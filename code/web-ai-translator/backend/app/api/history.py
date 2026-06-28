"""History API — view and edit translation history stored in SQLite."""

import json
import os
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.database import (
    get_job, get_jobs, get_chunks, get_jobs_for_user, get_job_owner,
    update_chunk_translation, update_job_notes,
    sync_job_to_db,
)
from app.config import settings
from app.auth import current_username, ADMIN_USERNAME, is_admin as _auth_is_admin
from app.user_paths import find_job_path
from app.utils.safe_io import atomic_write_json

router = APIRouter(prefix="/api/history")
WORKSPACE = settings.WORKSPACE_DIR


def _owner_or_401(request: Request) -> str:
    user = current_username(request)
    if not user:
        raise HTTPException(status_code=401, detail="Chưa đăng nhập")
    return user


def _is_admin(user: str) -> bool:
    # Delegates to app.auth.is_admin (env-var admin OR DB-flagged first-user).
    return _auth_is_admin(user)


def _check_owner(job_id: str, owner: str) -> None:
    """Raise 403 if `owner` cannot access this job. 400 if job_id format is invalid."""
    from app.utils.safe_io import is_valid_job_id
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="job_id không hợp lệ")
    db_owner = get_job_owner(job_id)
    if db_owner and db_owner != owner and not _is_admin(owner):
        raise HTTPException(status_code=403, detail="Không có quyền truy cập job này")


def _resolve_job_dir(job_id: str, owner: str) -> str:
    """Per-user job folder, with admin legacy fallback. Raises 404 if missing."""
    from app.utils.safe_io import is_valid_job_id
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="job_id không hợp lệ")
    p = find_job_path(WORKSPACE, job_id, owner, allow_legacy=_is_admin(owner))
    if not p:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return p


# ── List all jobs ──────────────────────────────────────────────
@router.get("")
async def history_list(
    limit: int = 100,
    offset: int = 0,
    owner: str = Depends(_owner_or_401),
):
    jobs = get_jobs_for_user(
        owner,
        include_unowned=_is_admin(owner),
        limit=limit,
        offset=offset,
    )
    return {"jobs": jobs, "total": len(jobs)}


# ── Single job metadata ────────────────────────────────────────
@router.get("/{job_id}")
async def history_job(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# ── Chunks list ───────────────────────────────────────────────
@router.get("/{job_id}/chunks")
async def history_chunks(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    chunks = get_chunks(job_id)
    return {"job_id": job_id, "chunks": chunks, "total": len(chunks)}


# ── Edit chunk translation ─────────────────────────────────────
class ChunkEditRequest(BaseModel):
    mt_latex: str
    edit_note: str = ""


def _validate_chunk_key(chunk_key: str) -> str:
    """Reject chunk keys that could escape progress.json with arbitrary nested keys.

    Two valid shapes (matching writer logic in pipeline.py):
      - "<digits>"                   (top-level translated chunk)
      - "input:<rel>:<digits>"       (nested input file chunk)

    `<rel>` is a tex-relative path so we forbid '..', leading slash, and
    backslash to keep it inside the source tree.
    """
    if not isinstance(chunk_key, str) or not (1 <= len(chunk_key) <= 256):
        raise HTTPException(400, "chunk_key không hợp lệ")
    if chunk_key.startswith("input:"):
        body = chunk_key[len("input:"):]
        parts = body.rsplit(":", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            raise HTTPException(400, "chunk_key không hợp lệ")
        rel = parts[0]
        if (not rel) or rel.startswith("/") or rel.startswith("\\") or "\\" in rel \
                or ".." in rel.replace("\\", "/").split("/"):
            raise HTTPException(400, "chunk_key chứa đường dẫn không an toàn")
        return chunk_key
    if not chunk_key.isdigit():
        raise HTTPException(400, "chunk_key không hợp lệ")
    return chunk_key


@router.put("/{job_id}/chunks/{chunk_key:path}")
async def edit_chunk(
    job_id: str,
    chunk_key: str,
    req: ChunkEditRequest,
    owner: str = Depends(_owner_or_401),
):
    _check_owner(job_id, owner)
    _validate_chunk_key(chunk_key)
    ok = update_chunk_translation(job_id, chunk_key, req.mt_latex, req.edit_note)
    if not ok:
        raise HTTPException(404, f"Chunk '{chunk_key}' not found for job '{job_id}'")

    # Also update progress.json so pipeline can recompile
    _update_progress_chunk(job_id, chunk_key, req.mt_latex, owner)
    return {"status": "ok", "chunk_key": chunk_key}


# ── Re-translate a single chunk with a user hint ───────────────
class HintRetranslateRequest(BaseModel):
    hint: str
    persist: bool = True  # save to DB + progress.json/chunk file


def _resolve_chunk_original(job_dir: str, chunk_key: str, source_type: str) -> str:
    """Resolve the original (source) text for a chunk.

    LaTeX:  re-derive from source_extracted/<main>.tex (or input file).
    PDF:    read chunks/chunk_XXX_original.txt.

    Raises HTTPException(400) if the source can't be located.
    """
    if source_type == "pdf_only":
        if not chunk_key.isdigit():
            raise HTTPException(400, "PDF chunk_key phải là số")
        idx = int(chunk_key)
        path = os.path.join(job_dir, "chunks", f"chunk_{idx:03d}_original.txt")
        if not os.path.exists(path):
            raise HTTPException(400, f"Không tìm thấy file gốc cho chunk {chunk_key}")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    # LaTeX path
    from app.services.latex_processor import split_into_chunks
    extract_dir = os.path.join(job_dir, "source_extracted")
    if not os.path.isdir(extract_dir):
        raise HTTPException(400, "Source LaTeX không tìm thấy")
    import glob as _glob

    if chunk_key.startswith("input:"):
        body = chunk_key[len("input:"):]
        rel, idx_str = body.rsplit(":", 1)
        if not idx_str.isdigit():
            raise HTTPException(400, "chunk_key không hợp lệ")
        idx = int(idx_str)
        # Find the input file
        candidates = _glob.glob(os.path.join(extract_dir, "**", rel), recursive=True)
        if not candidates:
            # rel may already be relative to source dir without subdirs
            candidates = [os.path.join(extract_dir, rel)] if os.path.exists(os.path.join(extract_dir, rel)) else []
        if not candidates:
            raise HTTPException(400, f"Không tìm thấy input file: {rel}")
        with open(candidates[0], "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        chunks = split_into_chunks(content)
    else:
        if not chunk_key.isdigit():
            raise HTTPException(400, "chunk_key không hợp lệ")
        idx = int(chunk_key)
        tex_files = _glob.glob(os.path.join(extract_dir, "**", "*.tex"), recursive=True)
        if not tex_files:
            raise HTTPException(400, "Không tìm thấy file .tex")
        main_tex = tex_files[0]
        for tf in tex_files:
            with open(tf, "r", encoding="utf-8", errors="ignore") as f:
                if "\\begin{document}" in f.read():
                    main_tex = tf
                    break
        with open(main_tex, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        # Match the body-only chunking the pipeline uses
        from app.services.pipeline import TranslationPipeline
        pipeline = TranslationPipeline(work_dir=WORKSPACE)
        _, body = pipeline._split_preamble_body(content)
        chunks = split_into_chunks(body)

    if idx < 0 or idx >= len(chunks):
        raise HTTPException(400, f"chunk index {idx} ngoài phạm vi (0..{len(chunks) - 1})")
    return chunks[idx]


def _persist_chunk_translation(
    job_id: str, chunk_key: str, new_mt: str, owner: str, source_type: str
) -> None:
    """Persist a regenerated translation to DB + progress/chunks file."""
    update_chunk_translation(job_id, chunk_key, new_mt, edit_note="hint-refined")

    job_dir = find_job_path(WORKSPACE, job_id, owner, allow_legacy=_is_admin(owner))
    if not job_dir:
        return

    if source_type == "pdf_only":
        if chunk_key.isdigit():
            idx = int(chunk_key)
            chunks_dir = os.path.join(job_dir, "chunks")
            os.makedirs(chunks_dir, exist_ok=True)
            path = os.path.join(chunks_dir, f"chunk_{idx:03d}_translated.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_mt)
        return

    _update_progress_chunk(job_id, chunk_key, new_mt, owner)


@router.post("/{job_id}/chunks/{chunk_key:path}/retranslate")
async def retranslate_chunk_with_hint(
    job_id: str,
    chunk_key: str,
    req: HintRetranslateRequest,
    owner: str = Depends(_owner_or_401),
):
    """Re-translate a single chunk steered by a user-provided hint.

    Spins up an ad-hoc Gemini session (Playwright), sends a refinement prompt,
    persists the new translation to DB and to the source-of-truth file
    (progress.json for LaTeX, chunks/chunk_XXX_translated.txt for PDF).

    The user must trigger Recompile (LaTeX) or PDF rebuild separately to see
    the change in the rendered output — this endpoint only updates the chunk.
    """
    _check_owner(job_id, owner)
    _validate_chunk_key(chunk_key)

    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    hint = (req.hint or "").strip()
    if not hint:
        raise HTTPException(400, "Gợi ý không được để trống")
    if len(hint) > 2000:
        raise HTTPException(400, "Gợi ý quá dài (tối đa 2000 ký tự)")

    # Fetch current MT from DB
    chunks = get_chunks(job_id)
    chunk_row = next((c for c in chunks if c["chunk_key"] == chunk_key), None)
    if not chunk_row:
        raise HTTPException(404, f"Chunk '{chunk_key}' không tồn tại")
    prev_mt = chunk_row.get("mt_latex") or ""
    if not prev_mt:
        raise HTTPException(400, "Chunk chưa có bản dịch hiện tại")

    source_type = job.get("source_type") or "latex"
    job_dir = _resolve_job_dir(job_id, owner)
    original = _resolve_chunk_original(job_dir, chunk_key, source_type)
    is_latex = source_type != "pdf_only"

    # Run Gemini refinement (ad-hoc Playwright session)
    from app.services.translator import WebAITranslator
    translator = WebAITranslator()
    try:
        _, page = await translator.launch_browser()
        try:
            await translator.start_new_chat(page)
        except Exception:
            pass
        try:
            new_mt = await translator.refine_with_hint(
                page, original=original, prev_translation=prev_mt,
                hint=hint, is_latex=is_latex,
            )
        finally:
            try:
                await translator.cleanup()
            except Exception:
                pass
    except Exception as e:
        raise HTTPException(500, f"Gemini refine thất bại: {e}")

    if not new_mt or not new_mt.strip():
        raise HTTPException(500, "Gemini trả về bản dịch rỗng")

    if req.persist:
        _persist_chunk_translation(job_id, chunk_key, new_mt, owner, source_type)

    return {
        "status": "ok",
        "chunk_key": chunk_key,
        "translation": new_mt,
        "persisted": req.persist,
    }


# ── Update job notes ───────────────────────────────────────────
class NotesRequest(BaseModel):
    notes: str


@router.put("/{job_id}/notes")
async def update_notes(
    job_id: str,
    req: NotesRequest,
    owner: str = Depends(_owner_or_401),
):
    _check_owner(job_id, owner)
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    update_job_notes(job_id, req.notes)
    return {"status": "ok"}


# ── Recompile after edits ──────────────────────────────────────
@router.post("/{job_id}/recompile")
async def recompile(job_id: str, owner: str = Depends(_owner_or_401)):
    """Recompile PDF from edited chunks (LaTeX jobs only)."""
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    if progress.get("source_type") == "pdf_only":
        raise HTTPException(400, "Recompile chỉ hỗ trợ LaTeX jobs")

    # Find .tex file
    extract_dir = os.path.join(job_dir, "source_extracted")
    if not os.path.isdir(extract_dir):
        raise HTTPException(400, "Source LaTeX không tìm thấy")

    from app.services.latex_processor import extract_source, compile_to_pdf
    import glob as _glob
    tex_files = _glob.glob(os.path.join(extract_dir, "**", "*.tex"), recursive=True)
    if not tex_files:
        raise HTTPException(400, "Không tìm thấy file .tex")

    # Find main tex (same logic as pipeline)
    main_tex = tex_files[0]
    for tf in tex_files:
        with open(tf, "r", encoding="utf-8", errors="ignore") as f:
            if "\\begin{document}" in f.read():
                main_tex = tf
                break

    # Rebuild .tex from progress.json translated_chunks (which includes edits)
    from app.services.pipeline import TranslationPipeline
    from app.services.latex_processor import split_into_chunks

    with open(main_tex, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    pipeline = TranslationPipeline(work_dir=WORKSPACE)
    preamble, body = pipeline._split_preamble_body(content)
    chunks = split_into_chunks(body)
    translated_chunks = progress.get("translated_chunks", {})
    translated_body = "".join(translated_chunks.get(str(i), chunks[i]) for i in range(len(chunks)))

    translated_preamble = pipeline._add_vietnamese_support(preamble)
    translated_body = pipeline._ensure_justified(translated_body)
    translated_body = pipeline._fix_latex_structure(translated_body)
    translated_content = translated_preamble + "\n" + translated_body

    output_dir = os.path.join(job_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    # Copy support files
    source_dir = os.path.dirname(main_tex)
    pipeline._copy_support_files(source_dir, output_dir, main_tex)
    pipeline._save_translated_input_files(job_id, pipeline._find_input_files(content, source_dir), output_dir)

    translated_tex = os.path.join(output_dir, "translated.tex")
    with open(translated_tex, "w", encoding="utf-8") as f:
        f.write(translated_content)

    try:
        pdf_path = compile_to_pdf(translated_tex, output_dir)
    except RuntimeError as e:
        raise HTTPException(500, f"Compile lỗi: {e}")

    progress["status"] = "done"
    atomic_write_json(progress_file, progress)

    # Sync to DB
    sync_job_to_db(job_id, progress, WORKSPACE)

    return {
        "status": "ok",
        "pdf_url": f"/api/pdf/{job_id}/translated",
    }


# ── Sync a job manually ───────────────────────────────────────
@router.post("/{job_id}/sync")
async def sync_job(job_id: str, owner: str = Depends(_owner_or_401)):
    """Force sync progress.json -> DB (useful for old jobs)."""
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "progress.json not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    sync_job_to_db(job_id, progress, WORKSPACE)
    return {"status": "synced"}


# ── Internal helper ───────────────────────────────────────────
def _update_progress_chunk(job_id: str, chunk_key: str, mt_latex: str, owner: str):
    """Write edited chunk back to progress.json."""
    job_dir = find_job_path(WORKSPACE, job_id, owner, allow_legacy=_is_admin(owner))
    if not job_dir:
        return
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        return
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    if chunk_key.startswith("input:"):
        # "input:{rel_path}:{idx}"
        parts = chunk_key[len("input:"):].rsplit(":", 1)
        if len(parts) == 2:
            input_rel, idx = parts
            tc = progress.setdefault(f"input_chunks:{input_rel}", {})
            tc[idx] = mt_latex
    else:
        progress.setdefault("translated_chunks", {})[chunk_key] = mt_latex

    atomic_write_json(progress_file, progress)
