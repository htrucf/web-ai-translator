"""FastAPI routes cho dịch .docx.

Mount qua `app.include_router(office_router)`. Endpoint shape song hành với
`/api/pdf-translate/*` để frontend tái dùng pattern poll-status + download.
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import current_username, is_admin as _auth_is_admin
from app.user_paths import (
    safe_username,
    user_dir as _user_dir,
    user_job_dir,
    find_job_path,
    legacy_jobs_dir,
    ensure_user_dirs,
)
from app.database import get_job_owner, upsert_job
from app.utils.safe_io import atomic_write_json, is_valid_job_id
from app.utils.browser_guard import require_no_browser_running
from app.config import settings
from app.rate_limit import upload_limit, translate_limit


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/office-translate", tags=["office-translate"])

WORKSPACE = os.path.abspath(settings.WORKSPACE_DIR)
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ── Ownership helpers ──────────────────────────────────────────

def _owner_or_401(request: Request) -> str:
    user = current_username(request)
    if not user:
        raise HTTPException(401, "Chưa đăng nhập")
    return user


def _is_admin(user: str) -> bool:
    return _auth_is_admin(user)


def _check_owner(job_id: str, owner: str) -> None:
    if not is_valid_job_id(job_id):
        raise HTTPException(400, "job_id không hợp lệ")
    db_owner = get_job_owner(job_id)
    if db_owner and db_owner != owner and not _is_admin(owner):
        raise HTTPException(403, "Không có quyền truy cập job này")


def _resolve_job_dir(job_id: str, owner: str, must_exist: bool = True) -> str:
    if not is_valid_job_id(job_id):
        raise HTTPException(400, "job_id không hợp lệ")
    p = find_job_path(WORKSPACE, job_id, owner, allow_legacy=_is_admin(owner))
    if p:
        return p
    if must_exist:
        raise HTTPException(404, "Không tìm thấy job")
    return user_job_dir(WORKSPACE, owner, job_id)


def _user_work_dir(owner: str) -> str:
    return _user_dir(WORKSPACE, owner)


def _detect_kind(filename: str) -> str | None:
    low = (filename or "").lower()
    if low.endswith(".docx"):
        return "docx"
    return None


def _media_type(kind: str) -> str:
    return {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(kind, "application/octet-stream")


# ── Pipeline manager (subprocess per job) ──────────────────────

class OfficePipelineManager:
    """Run each office job as its own subprocess so the Playwright event loop
    stays isolated. Mirrors PdfPipelineManager but stripped down — no retries,
    no AccountPool hook (office files are short)."""

    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def stop_job(self, job_id: str) -> None:
        with self._lock:
            entry = self._jobs.pop(job_id, None)
        if not entry:
            return
        proc = entry.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        work_dir = entry.get("work_dir")
        if not work_dir:
            return
        progress_file = os.path.join(work_dir, "jobs", job_id, "progress.json")
        if not os.path.exists(progress_file):
            return
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
            status = progress.get("status", "")
            if (not status.startswith("done")
                    and not status.startswith("error")
                    and status != "starting"):
                progress["status"] = "cancelled"
                atomic_write_json(progress_file, progress)
        except Exception:
            pass

    def is_job_running(self, job_id: str) -> bool:
        with self._lock:
            entry = self._jobs.get(job_id)
            if not entry:
                return False
            proc = entry.get("proc")
            return proc is not None and proc.poll() is None

    def start(self, job_id: str, file_path: str, kind: str, work_dir: str) -> None:
        with self._lock:
            existing = self._jobs.get(job_id)
        if existing and existing.get("proc") and existing["proc"].poll() is None:
            self.stop_job(job_id)

        thread = threading.Thread(
            target=self._run,
            args=(job_id, file_path, kind, work_dir),
            daemon=True,
        )
        thread.start()

    def _run(self, job_id: str, file_path: str, kind: str, work_dir: str) -> None:
        abs_workspace = os.path.abspath(work_dir).replace("\\", "/")
        abs_file = os.path.abspath(file_path).replace("\\", "/")
        backend = BACKEND_DIR.replace("\\", "/")

        job_dir = os.path.join(work_dir, "jobs", job_id)
        os.makedirs(job_dir, exist_ok=True)

        safe_kind = "docx"
        script_path = os.path.join(job_dir, "run_office_pipeline.py")
        script_content = f'''
import asyncio, sys, os, json, traceback

sys.path.insert(0, r"{backend}")
os.chdir(r"{backend}")
from app.office.pipeline import OfficeTranslationPipeline
from app.utils.safe_io import atomic_write_json


async def run_once():
    pipeline = OfficeTranslationPipeline(work_dir=r"{abs_workspace}")
    await pipeline.run(
        file_path=r"{abs_file}",
        job_id="{job_id}",
        kind="{safe_kind}",
    )


loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
try:
    loop.run_until_complete(run_once())
except (KeyboardInterrupt, SystemExit):
    pass
except Exception as e:
    pf = os.path.join(r"{abs_workspace}", "jobs", "{job_id}", "progress.json")
    progress = {{}}
    if os.path.exists(pf):
        try:
            with open(pf, "r", encoding="utf-8") as f:
                progress = json.load(f)
        except Exception:
            pass
    err = str(e)[:200] or type(e).__name__
    progress["status"] = "error: " + err
    try:
        atomic_write_json(pf, progress)
    except Exception:
        pass
    print(traceback.format_exc())
finally:
    try:
        if not loop.is_closed():
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
    except Exception:
        pass
'''
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_content)

        log_path = os.path.join(job_dir, "pipeline.log")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        with open(log_path, "a", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=BACKEND_DIR,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            with self._lock:
                self._jobs[job_id] = {"proc": proc, "work_dir": work_dir}
            proc.wait()

        with self._lock:
            entry = self._jobs.get(job_id)
            if entry and entry.get("proc") is proc:
                self._jobs.pop(job_id, None)

        logger.info("[OfficePipelineManager] job %s finished (exit=%s)",
                    job_id, proc.returncode)


_manager = OfficePipelineManager()


# ── Helpers ────────────────────────────────────────────────────

def _find_existing_office_job(filename: str, owner: str) -> dict | None:
    """Return a previously translated job that matches `filename`, or None.

    Matches caller's jobs first; admin additionally sees the legacy global
    `workspace/jobs/`.
    """
    norm = (filename or "").strip().lower()
    if not norm:
        return None

    roots: list[str] = []
    user_root = os.path.join(WORKSPACE, "users", safe_username(owner), "jobs")
    if os.path.isdir(user_root):
        roots.append(user_root)
    if _is_admin(owner):
        legacy = legacy_jobs_dir(WORKSPACE)
        if os.path.isdir(legacy):
            roots.append(legacy)

    seen: set[str] = set()
    for root in roots:
        for name in os.listdir(root):
            if not name.startswith("docx_") or name in seen:
                continue
            seen.add(name)
            job_dir = os.path.join(root, name)
            if not os.path.isdir(job_dir):
                continue
            pf = os.path.join(job_dir, "progress.json")
            if not os.path.exists(pf):
                continue
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    progress = json.load(f)
            except Exception:
                continue
            if (progress.get("source_filename") or "").strip().lower() != norm:
                continue
            kind = progress.get("kind") or "docx"
            translated_path = os.path.join(job_dir, "output", f"translated.{kind}")
            if not os.path.exists(translated_path):
                continue
            return {
                "job_id": name,
                "kind": kind,
                "filename": progress.get("source_filename", ""),
                "status": progress.get("status", "unknown"),
            }
    return None


# ── Endpoints ──────────────────────────────────────────────────

class StartRequest(BaseModel):
    job_id: str
    force: bool = False


@router.post("/upload")
@upload_limit
async def upload_and_translate(
    request: Request,
    file: UploadFile = File(...),
    owner: str = Depends(_owner_or_401),
):
    """Upload a .docx and start in-place translation."""
    if not file.filename:
        raise HTTPException(400, "Tên file không hợp lệ")
    kind = _detect_kind(file.filename)
    if not kind:
        raise HTTPException(400, "Chỉ chấp nhận .docx")

    require_no_browser_running()

    base_name = os.path.splitext(file.filename)[0]
    safe_base = re.sub(r"[^\w\-.]", "_", base_name)[:50]
    job_id = f"{kind}_{safe_base}"

    db_owner = get_job_owner(job_id)
    if db_owner and db_owner != owner and not _is_admin(owner):
        raise HTTPException(403, "Job ID này thuộc người dùng khác")

    existing = _find_existing_office_job(file.filename, owner)
    if existing:
        eid = existing["job_id"]
        return {
            "job_id": eid,
            "status": "already_done",
            "kind": existing["kind"],
            "filename": existing["filename"],
            "original_url": f"/api/office-translate/{eid}/original",
            "translated_url": f"/api/office-translate/{eid}/translated",
            "preview_url": f"/api/office-translate/{eid}/preview",
        }

    ensure_user_dirs(WORKSPACE, owner)
    job_dir = user_job_dir(WORKSPACE, owner, job_id)
    os.makedirs(job_dir, exist_ok=True)

    original_path = os.path.join(job_dir, f"original.{kind}")
    content = await file.read()
    with open(original_path, "wb") as f:
        f.write(content)

    progress = {
        "status": "pending",
        "kind": kind,
        "source_filename": file.filename,
        "source_type": "office",
    }
    atomic_write_json(os.path.join(job_dir, "progress.json"), progress)
    upsert_job(job_id, source_type="office", title=file.filename,
               status="pending", username=owner)

    _manager.start(job_id, original_path, kind, _user_work_dir(owner))

    return {
        "job_id": job_id,
        "kind": kind,
        "status": "started",
        "filename": file.filename,
        "original_url": f"/api/office-translate/{job_id}/original",
    }


@router.post("/start")
@translate_limit
async def start_translation(
    request: Request,
    req: StartRequest,
    owner: str = Depends(_owner_or_401),
):
    """Re-translate an existing office job."""
    _check_owner(req.job_id, owner)
    require_no_browser_running()
    job_dir = _resolve_job_dir(req.job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Không tìm thấy job")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    kind = progress.get("kind")
    if kind != "docx":
        raise HTTPException(400, "Job không phải office")

    original_path = os.path.join(job_dir, f"original.{kind}")
    if not os.path.exists(original_path):
        raise HTTPException(404, "Không tìm thấy file gốc")

    if req.force:
        for k in ("translated_chunks", "block_count", "total_chunks",
                  "applied_blocks", "has_preview", "preview_error"):
            progress.pop(k, None)
        progress["status"] = "pending"
        out_dir = os.path.join(job_dir, "output")
        for fn in (f"translated.{kind}", "preview.pdf"):
            p = os.path.join(out_dir, fn)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        atomic_write_json(progress_file, progress)
    else:
        if not progress.get("status", "").startswith("translating"):
            progress["status"] = "pending"
            atomic_write_json(progress_file, progress)

    _manager.start(req.job_id, original_path, kind, _user_work_dir(owner))
    return {"job_id": req.job_id, "status": "started", "kind": kind}


@router.get("/{job_id}/status")
async def job_status(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner, must_exist=False)
    progress_file = os.path.join(job_dir, "progress.json")

    result: dict = {"job_id": job_id, "status": "unknown", "source_type": "office"}

    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
        result["status"] = progress.get("status", "pending")
        result["kind"] = progress.get("kind")
        result["filename"] = progress.get("source_filename", "")
        result["block_count"] = progress.get("block_count", 0)
        result["total_chunks"] = progress.get("total_chunks", 0)
        result["applied_blocks"] = progress.get("applied_blocks", 0)
        result["has_preview"] = bool(progress.get("has_preview", False))
        if progress.get("preview_error"):
            result["preview_error"] = progress["preview_error"]

        m = re.match(r"translating (\d+)/(\d+)", result["status"])
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            result["current_chunk"] = cur
            result["total_chunks"] = total
            result["progress_percent"] = round(cur / total * 100) if total > 0 else 0
        elif result["status"] in ("done", "done_with_warnings"):
            result["progress_percent"] = 100

    kind = result.get("kind")
    if kind:
        original = os.path.join(job_dir, f"original.{kind}")
        translated = os.path.join(job_dir, "output", f"translated.{kind}")
        preview = os.path.join(job_dir, "output", "preview.pdf")
        if os.path.exists(original):
            result["original_url"] = f"/api/office-translate/{job_id}/original"
        if os.path.exists(translated):
            result["translated_url"] = f"/api/office-translate/{job_id}/translated"
        if os.path.exists(preview):
            result["preview_url"] = f"/api/office-translate/{job_id}/preview"

    return result


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    _manager.stop_job(job_id)
    return {"status": "cancelled"}


@router.get("/{job_id}/original")
async def serve_original(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    kind = progress.get("kind", "docx")
    path = os.path.join(job_dir, f"original.{kind}")
    if not os.path.exists(path):
        raise HTTPException(404, "File gốc không tồn tại")
    filename = progress.get("source_filename") or f"original.{kind}"
    return FileResponse(path, media_type=_media_type(kind), filename=filename)


@router.get("/{job_id}/translated")
async def serve_translated(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    kind = progress.get("kind", "docx")
    path = os.path.join(job_dir, "output", f"translated.{kind}")
    if not os.path.exists(path):
        raise HTTPException(404, "File dịch chưa có")
    source_name = progress.get("source_filename") or f"translated.{kind}"
    base, ext = os.path.splitext(source_name)
    if not ext:
        ext = "." + kind
    download_name = f"{base}_vi{ext}"
    return FileResponse(path, media_type=_media_type(kind), filename=download_name)


@router.get("/{job_id}/preview")
async def serve_preview(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    path = os.path.join(job_dir, "output", "preview.pdf")
    if not os.path.exists(path):
        raise HTTPException(404, "Preview PDF chưa được tạo (cần LibreOffice)")
    return FileResponse(path, media_type="application/pdf")


@router.get("/jobs")
async def list_jobs(owner: str = Depends(_owner_or_401)):
    """List office jobs visible to the caller (caller's own + admin sees legacy)."""
    roots: list[str] = []
    user_root = os.path.join(WORKSPACE, "users", safe_username(owner), "jobs")
    if os.path.isdir(user_root):
        roots.append(user_root)
    if _is_admin(owner):
        legacy = legacy_jobs_dir(WORKSPACE)
        if os.path.isdir(legacy):
            roots.append(legacy)

    out: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        for name in os.listdir(root):
            if not name.startswith("docx_") or name in seen:
                continue
            seen.add(name)
            job_dir = os.path.join(root, name)
            if not os.path.isdir(job_dir):
                continue

            kind_guess = "docx"
            entry = {
                "job_id": name,
                "kind": kind_guess,
                "source_type": "office",
                "has_translated": os.path.exists(
                    os.path.join(job_dir, "output", f"translated.{kind_guess}")
                ),
                "has_preview": os.path.exists(
                    os.path.join(job_dir, "output", "preview.pdf")
                ),
                "status": "unknown",
                "progress_percent": 0,
            }

            progress_file = os.path.join(job_dir, "progress.json")
            if os.path.exists(progress_file):
                try:
                    with open(progress_file, "r", encoding="utf-8") as f:
                        progress = json.load(f)
                except Exception:
                    progress = {}
                entry["kind"] = progress.get("kind", kind_guess)
                entry["status"] = progress.get("status", "pending")
                entry["filename"] = progress.get("source_filename", "")
                entry["block_count"] = progress.get("block_count", 0)
                entry["total_chunks"] = progress.get("total_chunks", 0)

                m = re.match(r"translating (\d+)/(\d+)", entry["status"])
                if m:
                    cur, total = int(m.group(1)), int(m.group(2))
                    entry["progress_percent"] = round(cur / total * 100) if total > 0 else 0
                elif entry["status"] in ("done", "done_with_warnings"):
                    entry["progress_percent"] = 100

            out.append(entry)

    return {"jobs": out}
