"""Separate API routes for PDF-only translation.

Mount this router in main.py:
    from app.pdf.routes import router as pdf_router
    app.include_router(pdf_router)

Or run standalone for testing:
    uvicorn app.pdf.routes:app --port 8001 --reload
"""

import logging
import os
import sys
import json
import re
import subprocess
import threading
import time
from fastapi import FastAPI, APIRouter, UploadFile, File, Form, HTTPException, Depends, Request

logger = logging.getLogger(__name__)
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.pdf.processor import (
    get_pdf_info,
    extract_text_blocks,
    split_blocks_into_chunks,
    parse_translated_chunk,
    rebuild_pdf,
)
from app.auth import current_username, ADMIN_USERNAME, is_admin as _auth_is_admin
from app.user_paths import (
    safe_username,
    user_dir as _user_dir,
    user_job_dir,
    find_job_path,
    legacy_jobs_dir,
    ensure_user_dirs,
)
from app.database import get_job_owner, upsert_job
from app.utils.safe_io import atomic_write_json
from app.utils.browser_guard import require_no_browser_running
from app.config import settings
from app.rate_limit import upload_limit, translate_limit
from app.pdf.model_preference import (
    expand_model_execution_order,
    model_preference_advice,
    parse_model_preference,
)

# ── Router (mounted in main app or standalone) ───────────────────
router = APIRouter(prefix="/api/pdf-translate", tags=["pdf-translate"])

# Resolved via app.paths in production (OS user-data dir) and via env override
# or backend/workspace in dev. Tests monkeypatch this constant directly.
WORKSPACE = os.path.abspath(settings.WORKSPACE_DIR)
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ── Ownership helpers ────────────────────────────────────────────

def _owner_or_401(request: Request) -> str:
    user = current_username(request)
    if not user:
        raise HTTPException(status_code=401, detail="Chưa đăng nhập")
    return user


def _is_admin(user: str) -> bool:
    # Delegates to app.auth.is_admin, which covers env-var admin AND the
    # DB-flagged first-user admin promoted at registration time.
    return _auth_is_admin(user)


def _check_owner(job_id: str, owner: str) -> None:
    """Raise 403 if `owner` cannot access this job. 400 if job_id format is invalid."""
    from app.utils.safe_io import is_valid_job_id
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="job_id không hợp lệ")
    db_owner = get_job_owner(job_id)
    if db_owner and db_owner != owner and not _is_admin(owner):
        raise HTTPException(status_code=403, detail="Không có quyền truy cập job này")


def _resolve_job_dir(job_id: str, owner: str, must_exist: bool = True) -> str:
    """Find on-disk job folder. Raises 400 if job_id is invalid, 404 if not found."""
    from app.utils.safe_io import is_valid_job_id
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="job_id không hợp lệ")
    p = find_job_path(WORKSPACE, job_id, owner, allow_legacy=_is_admin(owner))
    if p:
        return p
    if must_exist:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return user_job_dir(WORKSPACE, owner, job_id)


def _user_work_dir(owner: str) -> str:
    """Return the work_dir to pass into the pipeline subprocess."""
    return _user_dir(WORKSPACE, owner)


# ── Pipeline manager (subprocess-based, concurrent jobs) ────
class PdfPipelineManager:
    """Manages PDF translation jobs in separate subprocesses.

    Multiple jobs can run concurrently. Starting a new job does NOT stop
    other running jobs. Use stop_job(job_id) to cancel a specific job.

    Each job tracks its own `work_dir` (the parent of `jobs/`, i.e. the
    per-user workspace). The pipeline subprocess builds `{work_dir}/jobs/{job_id}/`
    so per-user isolation comes for free.
    """

    def __init__(self):
        # job_id -> {"proc": Popen, "work_dir": str}
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _job_dir(self, work_dir: str, job_id: str) -> str:
        return os.path.join(work_dir, "jobs", job_id)

    def stop_job(self, job_id: str):
        """Stop a specific job and mark it as cancelled."""
        with self._lock:
            entry = self._jobs.pop(job_id, None)

        if not entry:
            return
        proc = entry.get("proc")
        work_dir = entry.get("work_dir")

        if proc and proc.poll() is None:
            print(f"[PdfPipelineManager] Stopping job: {job_id}")
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if not work_dir:
            return
        progress_file = os.path.join(self._job_dir(work_dir, job_id), "progress.json")
        if os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8") as f:
                    progress = json.load(f)
                status = progress.get("status", "")
                # Mark cancelled for any non-terminal status.
                # "starting" means the API handler just reset for a new run — don't overwrite.
                if (not status.startswith("done")
                        and not status.startswith("error")
                        and status != "starting"):
                    progress["status"] = "cancelled"
                    atomic_write_json(progress_file, progress)
                    print(f"[PdfPipelineManager] Marked job {job_id} as cancelled")
            except Exception:
                pass

    def is_job_running(self, job_id: str) -> bool:
        with self._lock:
            entry = self._jobs.get(job_id)
            if not entry:
                return False
            proc = entry.get("proc")
            return proc is not None and proc.poll() is None

    @property
    def running_jobs(self) -> list[str]:
        with self._lock:
            return [jid for jid, e in self._jobs.items()
                    if e.get("proc") and e["proc"].poll() is None]

    @property
    def is_running(self) -> bool:
        with self._lock:
            return any(e.get("proc") and e["proc"].poll() is None
                       for e in self._jobs.values())

    def start(self, job_id: str, pdf_path: str = "", mode: str = "standard",
              agentic: bool = False, work_dir: str | None = None,
              num_tabs: int = 2, models: list[str] | None = None,
              judge_backend: str | None = "web"):
        """Start a new job. If the same job is already running, stop it first.

        `work_dir` is the per-user workspace (parent of `jobs/`). Defaults to
        global WORKSPACE for backwards compat — but routes should always pass
        the per-user dir.
        """
        if work_dir is None:
            work_dir = WORKSPACE
        with self._lock:
            existing = self._jobs.get(job_id)
        if existing and existing.get("proc") and existing["proc"].poll() is None:
            self.stop_job(job_id)

        thread = threading.Thread(
            target=self._run,
            args=(job_id, pdf_path, mode, agentic, work_dir, num_tabs, models, judge_backend),
            daemon=True,
        )
        thread.start()

    def _run(self, job_id: str, pdf_path: str, mode: str = "standard",
             agentic: bool = False, work_dir: str = "", num_tabs: int = 2,
             models: list[str] | None = None, judge_backend: str | None = "web"):
        """Run the PDF pipeline in a subprocess with its own event loop."""
        if not work_dir:
            work_dir = WORKSPACE
        abs_workspace = os.path.abspath(work_dir).replace("\\", "/")
        abs_pdf = os.path.abspath(pdf_path).replace("\\", "/")
        backend = BACKEND_DIR.replace("\\", "/")

        job_dir = self._job_dir(work_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)

        script_path = os.path.join(job_dir, "run_pdf_pipeline.py")
        safe_mode = mode if mode in ("standard", "book") else "standard"
        if agentic:
            pipeline_import = "from app.pdf.agents import MultiAgentCoordinator as _Pipeline"
            safe_models = json.dumps(parse_model_preference(models))
            safe_judge_backend = json.dumps(judge_backend or "off")
            ctor_extra = f", num_tabs={int(num_tabs)}, models={safe_models}, judge_backend={safe_judge_backend}"
        else:
            pipeline_import = "from app.pdf.pipeline import PdfTranslationPipeline as _Pipeline"
            ctor_extra = ""
        script_content = f'''
import asyncio, sys, os, json, time, traceback

sys.path.insert(0, r"{backend}")
os.chdir(r"{backend}")
{pipeline_import}
from app.utils.safe_io import atomic_write_json

MAX_RETRIES = 2          # Auto-resume on crash (browser TargetClosedError etc.)
RETRY_DELAY = 5          # Short wait before retry — pipeline resumes from saved progress

async def run_once():
    pipeline = _Pipeline(work_dir=r"{abs_workspace}", mode="{safe_mode}"{ctor_extra})
    await pipeline.run(
        pdf_path=r"{abs_pdf}",
        job_id="{job_id}",
    )

def is_done():
    pf = os.path.join(r"{abs_workspace}", "jobs", "{job_id}", "progress.json")
    if os.path.exists(pf):
        with open(pf, "r", encoding="utf-8") as f:
            p = json.load(f)
        return p.get("status", "") in ("done", "done_with_warnings", "cancelled")
    return False

import signal
_should_exit = False

def _handle_sigterm(signum, frame):
    global _should_exit
    _should_exit = True

try:
    signal.signal(signal.SIGTERM, _handle_sigterm)
except Exception:
    pass

for attempt in range(MAX_RETRIES + 1):
    if is_done() or _should_exit:
        print(f"Job done or exit requested, stopping.")
        break

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _exit_cleanly = False
    try:
        loop.run_until_complete(run_once())
        _exit_cleanly = True
        break  # Success
    except (KeyboardInterrupt, SystemExit):
        print("Pipeline received stop signal, exiting cleanly.")
        _exit_cleanly = True
        break
    except Exception as e:
        tb = traceback.format_exc()
        print(f"Pipeline error (attempt {{attempt + 1}}/{{MAX_RETRIES + 1}}): {{e}}")
        print(f"Traceback:\\n{{tb}}")

        pf = os.path.join(r"{abs_workspace}", "jobs", "{job_id}", "progress.json")
        progress = {{}}
        if os.path.exists(pf):
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    progress = json.load(f)
            except Exception:
                pass

        # The inner pipeline already wrote progress["error_detail"] via
        # _record_error before re-raising — preserve it. If the exception
        # escaped before that (very early failure), synthesize a minimal one.
        if not progress.get("error_detail"):
            progress["error_detail"] = {{
                "type": type(e).__name__,
                "message": str(e)[:500],
                "phase": "wrapper",
                "chunk_idx_at_error": None,
                "traceback": tb[-4000:],
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }}

        # Track retry attempts so the UI can show "attempt 2/3" without
        # losing the underlying error_detail across retries.
        progress["error_detail"]["attempts_used"] = attempt + 1
        progress["error_detail"]["max_attempts"] = MAX_RETRIES + 1

        if attempt < MAX_RETRIES and not _should_exit:
            progress["status"] = f"retrying (attempt {{attempt + 2}}/{{MAX_RETRIES + 1}})"
            try:
                atomic_write_json(pf, progress)
            except Exception:
                pass
            print(f"Retrying in {{RETRY_DELAY}}s... (will resume from saved progress)")
            time.sleep(RETRY_DELAY)
        else:
            # Final failure — keep the structured error_detail (already saved
            # by the pipeline), just refine the status string so the UI
            # shows a useful summary without re-reading the JSON.
            det = progress["error_detail"]
            where = (f"chunk {{det['chunk_idx_at_error'] + 1}}"
                      if det.get("chunk_idx_at_error") is not None
                      else det.get("phase") or "unknown")
            progress["status"] = (
                f"error in {{where}}: {{det.get('type','Error')}}: "
                f"{{(det.get('message') or '')[:120]}}"
            )
            try:
                atomic_write_json(pf, progress)
            except Exception:
                pass
    finally:
        try:
            if not loop.is_closed():
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
        except Exception:
            pass
'''
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_content)

        log_path = os.path.join(job_dir, "pipeline.log")
        print(f"[PdfPipelineManager] Starting job {job_id}, log: {log_path}")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        with open(log_path, "a", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=BACKEND_DIR,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            with self._lock:
                self._jobs[job_id] = {"proc": proc, "work_dir": work_dir}

            proc.wait()

        with self._lock:
            entry = self._jobs.get(job_id)
            if entry and entry.get("proc") is proc:
                self._jobs.pop(job_id, None)

        print(f"[PdfPipelineManager] Job {job_id} finished (exit={proc.returncode})")

        # Sync final progress → DB. PDF subprocess flow has no other sync point
        # (Celery uses _watch_progress); needed for benchmark cols num_tabs/
        # duration_seconds + heuristic_score to be queryable from the jobs table.
        try:
            from app.database import sync_job_to_db
            pf_final = os.path.join(job_dir, "progress.json")
            if os.path.exists(pf_final):
                with open(pf_final, "r", encoding="utf-8") as f:
                    _final = json.load(f)
                sync_job_to_db(job_id, _final, os.path.abspath(work_dir))
        except Exception as _sync_err:
            print(f"[PdfPipelineManager] DB sync failed: {_sync_err}")


_manager = PdfPipelineManager()


def _clamp_tabs(n) -> int:
    """Giới hạn số tab song song vào [1, 6] (an toàn, đủ dải để benchmark)."""
    try:
        return max(1, min(6, int(n)))
    except (TypeError, ValueError):
        return 2


def _has_model_selection(value) -> bool:
    """Phân biệt user thật sự gửi thứ tự model với payload resume rỗng."""
    if value is None:
        return False
    if isinstance(value, str):
        s = value.strip()
        return bool(s) and s not in ("[]", "null")
    if isinstance(value, (list, tuple)):
        return any(str(item or "").strip() for item in value)
    return True


def _pdf_job_id_base(filename: str) -> str:
    base_name = os.path.splitext(filename)[0]
    slug = re.sub(r'[^\w\-.]', '_', base_name)[:50] or "upload"
    return f"pdf_{slug}"


def _unique_pdf_job_id(base_job_id: str, owner: str) -> str:
    """Upload mới phải là một lượt chạy sạch nếu base job đã tồn tại."""
    owner_jobs_dir = os.path.join(WORKSPACE, "users", safe_username(owner), "jobs")

    def exists(job_id: str) -> bool:
        if os.path.exists(os.path.join(owner_jobs_dir, job_id)):
            return True
        db_owner = get_job_owner(job_id)
        return bool(db_owner and db_owner != owner and not _is_admin(owner))

    if not exists(base_job_id):
        return base_job_id

    stamp = time.strftime("%Y%m%d_%H%M%S")
    for i in range(0, 100):
        suffix = stamp if i == 0 else f"{stamp}_{i}"
        candidate = f"{base_job_id}_{suffix}"
        if not exists(candidate):
            return candidate

    return f"{base_job_id}_{stamp}_{int(time.time() * 1000)}"


_PDF_TERMINAL_STATUSES = ("done", "done_with_warnings", "cancelled", "superseded")
_PDF_TERMINAL_PREFIXES = ("error", "compile_error")


def _is_pdf_terminal_status(status: str) -> bool:
    status = (status or "").strip()
    return status in _PDF_TERMINAL_STATUSES or status.startswith(_PDF_TERMINAL_PREFIXES)


def _same_pdf_document(
    job_id: str,
    progress: dict,
    *,
    base_job_id: str = "",
    title: str = "",
    page_count: int | None = None,
) -> bool:
    """Best-effort match for repeated runs of the same uploaded PDF."""
    if base_job_id:
        if job_id == base_job_id or job_id.startswith(f"{base_job_id}_"):
            return True
        if progress.get("base_job_id") == base_job_id:
            return True

    title_norm = (title or "").strip().lower()
    existing_title = str(progress.get("title") or "").strip().lower()
    try:
        existing_pages = int(progress.get("page_count") or 0)
        wanted_pages = int(page_count or 0)
    except (TypeError, ValueError):
        existing_pages = wanted_pages = 0

    return bool(title_norm and wanted_pages > 0
                and existing_title == title_norm
                and existing_pages == wanted_pages)


def _matching_pdf_jobs_for_document(
    owner: str,
    *,
    base_job_id: str = "",
    title: str = "",
    page_count: int | None = None,
    exclude_job_id: str = "",
) -> list[tuple[str, str, dict]]:
    owner_jobs_dir = os.path.join(WORKSPACE, "users", safe_username(owner), "jobs")
    if not os.path.isdir(owner_jobs_dir):
        return []

    matches: list[tuple[str, str, dict]] = []
    for name in os.listdir(owner_jobs_dir):
        if name == exclude_job_id or not name.startswith("pdf_"):
            continue
        job_dir = os.path.join(owner_jobs_dir, name)
        progress_file = os.path.join(job_dir, "progress.json")
        if not os.path.isdir(job_dir) or not os.path.exists(progress_file):
            continue
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
        except Exception:
            continue
        if _same_pdf_document(
            name,
            progress,
            base_job_id=base_job_id,
            title=title,
            page_count=page_count,
        ):
            matches.append((name, job_dir, progress))
    return matches


def _mark_pdf_job_superseded(job_id: str, job_dir: str, new_job_id: str) -> None:
    """Keep old logs/report data, but make it clear this run is no longer active."""
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        return
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
    except Exception:
        progress = {}

    status = str(progress.get("status") or "")
    if status.startswith("done") or status.startswith("error") or status == "compile_error":
        return

    progress.setdefault("previous_status", status or "unknown")
    progress["status"] = "superseded"
    progress["superseded_by"] = new_job_id
    progress["superseded_at"] = time.time()
    progress["superseded_reason"] = "newer_run_for_same_document"
    atomic_write_json(progress_file, progress)
    try:
        upsert_job(job_id, status="superseded")
    except Exception:
        pass


def _kill_orphan_pdf_process(job_id: str) -> None:
    """Stop a leftover run_pdf_pipeline process even if backend reloaded."""
    try:
        import psutil
    except Exception:
        psutil = None

    if psutil:
        needle = f"jobs/{job_id}/run_pdf_pipeline.py".lower()
        current_pid = os.getpid()
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                if proc.info.get("pid") == current_pid:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or []).lower().replace("\\", "/")
                if needle not in cmdline:
                    continue
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return

    # Dev Windows fallback: psutil may be missing in an old venv. Keep this
    # tightly scoped to our generated pipeline script path.
    if os.name != "nt" or not re.match(r"^[\w.\-]+$", job_id):
        return
    script_a = f"jobs/{job_id}/run_pdf_pipeline.py"
    script_b = f"jobs\\{job_id}\\run_pdf_pipeline.py"
    command = (
        "$current = $PID; "
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.ProcessId -ne $current -and ($_.CommandLine -like '*{script_a}*' -or $_.CommandLine -like '*{script_b}*') }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        logger.warning("fallback orphan cleanup failed for %s: %s", job_id, e)


def _supersede_older_pdf_jobs_for_document(
    *,
    owner: str,
    new_job_id: str,
    base_job_id: str = "",
    title: str = "",
    page_count: int | None = None,
) -> list[str]:
    """Only the newest run for one document should stay active.

    Old folders/logs remain in place so history and reports can still explain
    what happened, but non-terminal older runs are stopped and marked
    `superseded`.
    """
    stopped: list[str] = []
    matches = _matching_pdf_jobs_for_document(
        owner,
        base_job_id=base_job_id,
        title=title,
        page_count=page_count,
        exclude_job_id=new_job_id,
    )
    if not matches:
        return stopped

    try:
        from app.dispatcher import get_dispatcher
        dispatcher = get_dispatcher(pdf_fallback=_manager)
    except Exception:
        dispatcher = None

    for old_job_id, old_job_dir, progress in matches:
        if _is_pdf_terminal_status(str(progress.get("status") or "")):
            continue
        if dispatcher:
            try:
                dispatcher.stop_job(old_job_id)
            except Exception as e:
                logger.warning("stop old PDF job failed: %s", e)
        _kill_orphan_pdf_process(old_job_id)
        _mark_pdf_job_superseded(old_job_id, old_job_dir, new_job_id)
        stopped.append(old_job_id)

    return stopped


def _dispatch_pdf_start(job_id: str, pdf_path: str, work_dir: str,
                        mode: str = "standard", agentic: bool = False,
                        num_tabs: int = 2,
                        models: list[str] | None = None,
                        judge_backend: str | None = "web") -> None:
    """Send a PDF job to the unified dispatcher (Celery in prod, subprocess in dev).

    Falls back to the local subprocess manager if the dispatcher is unreachable
    so the dev experience stays the same when Redis/Celery aren't running.
    """
    num_tabs = _clamp_tabs(num_tabs)
    try:
        from app.dispatcher import get_dispatcher
        from app.metrics import jobs_enqueued_total
        dispatcher = get_dispatcher(pdf_fallback=_manager)
        dispatcher.start_pdf(
            job_id, pdf_path, work_dir,
            options={
                "mode": mode, "agentic": agentic, "num_tabs": num_tabs,
                "models": parse_model_preference(models),
                "judge_backend": judge_backend,
            },
        )
        jobs_enqueued_total.labels(source_type="pdf").inc()
    except Exception as e:
        logger.warning("dispatcher fallback: %s", e)
        _manager.start(job_id, pdf_path, mode=mode, agentic=agentic,
                       work_dir=work_dir, num_tabs=num_tabs,
                       models=parse_model_preference(models),
                       judge_backend=judge_backend)


def _dispatch_stop(job_id: str) -> None:
    """Cancel a job via dispatcher; fall back to legacy manager."""
    try:
        from app.dispatcher import get_dispatcher
        dispatcher = get_dispatcher(pdf_fallback=_manager)
        dispatcher.stop_job(job_id)
    except Exception:
        _manager.stop_job(job_id)


# ── Endpoints ───────────────────────────────────────────────────

class TranslatePdfRequest(BaseModel):
    job_id: str
    force: bool = False
    mode: str | None = None  # "standard" or "book"
    agentic: bool = False    # opt-in multi-agent pipeline (Planner → Glossary → Translator → Critic)
    num_tabs: int | None = None  # số tab/luồng dịch song song (1–6) — để benchmark hiệu năng
    models: list[str] | None = None  # thứ tự model do user chọn
    judge_backend: str | None = None


@router.get("/model-preference/advice")
async def get_model_preference_advice(
    models: str = "",
    owner: str = Depends(_owner_or_401),
):
    chosen = parse_model_preference(models)
    return model_preference_advice(WORKSPACE, owner, chosen)


def _find_existing_pdf_job(
    title: str,
    page_count: int,
    owner: str,
    exclude_job_id: str = "",
) -> dict | None:
    """Search the caller's existing PDF jobs for a matching paper.

    Admin also searches legacy `workspace/jobs/`. Returns job info dict if
    found, None otherwise.
    """
    if not title or page_count <= 0:
        return None

    title_norm = title.strip().lower()

    # Roots to scan
    roots: list[str] = []
    user_root = os.path.join(WORKSPACE, "users", safe_username(owner), "jobs")
    if os.path.isdir(user_root):
        roots.append(user_root)
    if _is_admin(owner):
        legacy = os.path.join(WORKSPACE, "jobs")
        if os.path.isdir(legacy):
            roots.append(legacy)

    seen: set[str] = set()
    for jobs_dir in roots:
        for name in os.listdir(jobs_dir):
            if not name.startswith("pdf_") or name == exclude_job_id or name in seen:
                continue
            seen.add(name)
            job_dir = os.path.join(jobs_dir, name)
            if not os.path.isdir(job_dir):
                continue

            progress_file = os.path.join(job_dir, "progress.json")
            if not os.path.exists(progress_file):
                continue

            try:
                with open(progress_file, "r", encoding="utf-8") as f:
                    progress = json.load(f)
            except Exception:
                continue

            existing_title = progress.get("title", "").strip().lower()
            existing_pages = progress.get("page_count", 0)

            if not existing_title or existing_pages <= 0:
                continue

            if existing_pages == page_count and existing_title == title_norm:
                status = progress.get("status", "unknown")
                has_translated = os.path.exists(
                    os.path.join(job_dir, "output", "translated.pdf")
                )
                return {
                    "job_id": name,
                    "status": status,
                    "has_translated_pdf": has_translated,
                    "title": progress.get("title", ""),
                    "page_count": existing_pages,
                }

    return None


# ── LLM Judge: models list (defined early — before any /{job_id}/... routes) ──
@router.get("/judge/models")
async def judge_list_models():
    """Return available Ollama models for LLM-as-Judge evaluation."""
    try:
        from app.pdf.llm_judge import list_available_models, OLLAMA_URL
        import httpx as _httpx
        ollama_running = False
        try:
            r = _httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
            ollama_running = r.is_success
        except Exception:
            pass
        models = list_available_models() if ollama_running else []
        return {
            "ollama_running": ollama_running,
            "ollama_url": OLLAMA_URL,
            "models": models,
            "default_model": "qwen2.5:32b",
        }
    except ImportError:
        return {"ollama_running": False, "models": [], "default_model": "qwen2.5:32b"}


@router.post("/upload")
@upload_limit
async def upload_and_translate(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("standard"),
    agentic: bool = Form(False),
    num_tabs: int = Form(2),
    models: str = Form(""),
    judge_backend: str = Form("web"),
    owner: str = Depends(_owner_or_401),
):
    """Upload a PDF and start translation.

    If a matching paper (same title + page count) was already translated,
    returns status="already_done" so the frontend can show a confirmation dialog.

    `agentic=true` opts into the multi-agent pipeline (Planner → Glossary →
    Translator → Critic). Default is the legacy single-pipeline path.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    require_no_browser_running()

    # Per-user job folder
    ensure_user_dirs(WORKSPACE, owner)
    base_job_id = _pdf_job_id_base(file.filename)
    job_id = _unique_pdf_job_id(base_job_id, owner)
    job_dir = user_job_dir(WORKSPACE, owner, job_id)
    os.makedirs(job_dir, exist_ok=True)

    pdf_path = os.path.join(job_dir, "original.pdf")
    content = await file.read()
    with open(pdf_path, "wb") as f:
        f.write(content)

    info = get_pdf_info(pdf_path)
    if not info["has_text"]:
        raise HTTPException(
            400,
            "PDF không chứa text (có thể là ảnh scan). "
            "Chỉ hỗ trợ PDF digital."
        )

    # Check for existing translation of the same paper (caller's jobs only)
    existing = _find_existing_pdf_job(
        info["title"], info["page_count"], owner, exclude_job_id=""
    )
    if existing and existing["has_translated_pdf"]:
        existing_id = existing["job_id"]
        return {
            "job_id": existing_id,
            "status": "already_done",
            "title": existing["title"],
            "pages": existing["page_count"],
            "original_pdf_url": f"/api/pdf-translate/{existing_id}/original",
            "translated_pdf_url": f"/api/pdf-translate/{existing_id}/translated",
        }

    chosen_models = parse_model_preference(models)
    execution_models = expand_model_execution_order(chosen_models)
    advice = model_preference_advice(WORKSPACE, owner, chosen_models)

    # Save fresh metadata in progress.json for this upload run. Do not reuse a
    # previous progress.json here: old cancelled/pause flags would stop the new
    # job before it really starts.
    progress_file = os.path.join(job_dir, "progress.json")
    progress = {
        "status": "pending",
        "title": info["title"],
        "page_count": info["page_count"],
        "total_chars": info["total_chars"],
        "source_type": "pdf_only",
        "mode": mode,
        "agentic": agentic,
        "num_tabs": _clamp_tabs(num_tabs),
        "model_preference": chosen_models,
        "models": execution_models,
        "model_preference_advice": advice,
        "judge_backend": judge_backend,
        "original_filename": file.filename,
        "run_label": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_job_id": base_job_id,
    }
    atomic_write_json(progress_file, progress)

    # Record ownership in DB
    upsert_job(job_id, source_type="pdf", title=info["title"],
               status="pending", username=owner)

    superseded_jobs = _supersede_older_pdf_jobs_for_document(
        owner=owner,
        new_job_id=job_id,
        base_job_id=base_job_id,
        title=info["title"],
        page_count=info["page_count"],
    )
    if superseded_jobs:
        progress["superseded_jobs"] = superseded_jobs
        atomic_write_json(progress_file, progress)

    _dispatch_pdf_start(job_id, pdf_path, _user_work_dir(owner),
                        mode=mode, agentic=agentic, num_tabs=num_tabs,
                        models=chosen_models, judge_backend=judge_backend)

    return {
        "job_id": job_id,
        "status": "started",
        "original_pdf_url": f"/api/pdf-translate/{job_id}/original",
        "pages": info["page_count"],
        "total_chars": info["total_chars"],
        "title": info["title"],
        "original_filename": file.filename,
        "mode": mode,
        "agentic": agentic,
        "model_preference": chosen_models,
        "models": execution_models,
        "model_preference_advice": advice,
        "judge_backend": judge_backend,
        "superseded_jobs": superseded_jobs,
    }


@router.post("/start")
@translate_limit
async def start_translation(
    request: Request,
    req: TranslatePdfRequest,
    owner: str = Depends(_owner_or_401),
):
    """Start or re-translate an existing PDF job."""
    _check_owner(req.job_id, owner)
    require_no_browser_running()
    job_dir = _resolve_job_dir(req.job_id, owner)
    pdf_path = os.path.join(job_dir, "original.pdf")

    if not os.path.exists(pdf_path):
        raise HTTPException(404, f"No PDF found for job {req.job_id}")

    progress_file = os.path.join(job_dir, "progress.json")

    existing_progress: dict = {}
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                existing_progress = json.load(f)
        except Exception:
            existing_progress = {}

    # Resolve runtime options. On resume, the UI may send only job_id; in that
    # case keep the exact model/tab/judge settings saved when the job started.
    agentic = req.agentic
    if not agentic and existing_progress.get("agentic"):
        agentic = True
    resolved_mode = req.mode or existing_progress.get("mode") or "standard"
    resolved_tabs = _clamp_tabs(
        req.num_tabs if req.num_tabs is not None else existing_progress.get("num_tabs", 2)
    )
    model_source = (
        req.models
        if _has_model_selection(req.models)
        else (existing_progress.get("model_preference") or existing_progress.get("models"))
    )
    chosen_models = parse_model_preference(model_source)
    execution_models = expand_model_execution_order(chosen_models)
    advice = model_preference_advice(WORKSPACE, owner, chosen_models)
    judge_backend = req.judge_backend or existing_progress.get("judge_backend") or "off"

    if req.force:
        # Hard reset: xoa progress va output cu
        if os.path.exists(progress_file):
            with open(progress_file, "r", encoding="utf-8") as f:
                old = json.load(f)
            # Giu lai metadata, xoa progress dich
            fresh = {
                k: old[k]
                for k in ("title", "page_count", "total_chars", "source_type", "mode", "original_filename")
                if k in old
            }
            fresh["status"] = "pending"
            fresh["agentic"] = agentic
            fresh["mode"] = resolved_mode
            fresh["num_tabs"] = resolved_tabs
            fresh["model_preference"] = chosen_models
            fresh["models"] = execution_models
            fresh["judge_backend"] = judge_backend
            fresh["model_preference_advice"] = advice
            with open(progress_file, "w", encoding="utf-8") as f:
                json.dump(fresh, f, ensure_ascii=False, indent=2)
        output_pdf = os.path.join(job_dir, "output", "translated.pdf")
        if os.path.exists(output_pdf):
            os.remove(output_pdf)
    elif os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
        status = progress.get("status", "")
        # Reset bat ky trang thai khong phai "dang dich" ve pending
        # (done, error, cancelled, retrying, resuming)
        if status not in ("translating",) and not status.startswith("translating"):
            progress["status"] = "pending"
        progress.pop("pause_requested", None)
        progress.pop("paused_at", None)
        progress["agentic"] = agentic
        progress["mode"] = resolved_mode
        progress["num_tabs"] = resolved_tabs
        progress["model_preference"] = chosen_models
        progress["models"] = execution_models
        progress["judge_backend"] = judge_backend
        progress["model_preference_advice"] = advice
        atomic_write_json(progress_file, progress)

    current_meta = existing_progress
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                current_meta = json.load(f)
        except Exception:
            current_meta = existing_progress
    superseded_jobs = _supersede_older_pdf_jobs_for_document(
        owner=owner,
        new_job_id=req.job_id,
        base_job_id=current_meta.get("base_job_id") or _pdf_job_id_base(current_meta.get("original_filename") or req.job_id),
        title=current_meta.get("title", ""),
        page_count=current_meta.get("page_count"),
    )
    if superseded_jobs:
        current_meta["superseded_jobs"] = superseded_jobs
        atomic_write_json(progress_file, current_meta)

    _dispatch_pdf_start(req.job_id, pdf_path, _user_work_dir(owner),
                        mode=resolved_mode, agentic=agentic, num_tabs=resolved_tabs,
                        models=chosen_models, judge_backend=judge_backend)

    response_meta = {}
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                response_meta = json.load(f)
        except Exception:
            response_meta = {}

    return {"job_id": req.job_id, "status": "started", "mode": resolved_mode,
            "agentic": agentic, "num_tabs": resolved_tabs,
            "model_preference": chosen_models, "models": execution_models,
            "model_preference_advice": advice, "judge_backend": judge_backend,
            "title": response_meta.get("title"),
            "original_filename": response_meta.get("original_filename"),
            "superseded_jobs": superseded_jobs}


@router.get("/{job_id}/status")
async def job_status(job_id: str, owner: str = Depends(_owner_or_401)):
    """Get current status of a PDF translation job."""
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner, must_exist=False)
    progress_file = os.path.join(job_dir, "progress.json")

    result = {"job_id": job_id, "status": "unknown", "source_type": "pdf_only"}

    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)

        result["status"] = progress.get("status", "pending")
        result["source_type"] = progress.get("source_type", "pdf_only")
        result["mode"] = progress.get("mode", "standard")
        result["agentic"] = bool(progress.get("agentic", False))
        result["num_tabs"] = progress.get("num_tabs")
        result["model_preference"] = progress.get("model_preference")
        result["models"] = progress.get("models")
        result["model_preference_advice"] = progress.get("model_preference_advice")
        if progress.get("duration_seconds") is not None:
            result["duration_seconds"] = progress.get("duration_seconds")
        if progress.get("original_filename"):
            result["original_filename"] = progress["original_filename"]
        if progress.get("title"):
            result["title"] = progress["title"]
        phase = progress.get("phase")
        if phase:
            result["phase"] = phase
        if progress.get("base_job_id"):
            result["base_job_id"] = progress["base_job_id"]
        if progress.get("run_label"):
            result["run_label"] = progress["run_label"]
        if progress.get("superseded_by"):
            result["superseded_by"] = progress["superseded_by"]
        if progress.get("superseded_jobs"):
            result["superseded_jobs"] = progress["superseded_jobs"]
        if progress.get("previous_status"):
            result["previous_status"] = progress["previous_status"]

        m = re.match(r"translating (\d+)/(\d+)", result["status"])
        if m:
            current, total = int(m.group(1)), int(m.group(2))
            result["current_chunk"] = current
            result["total_chunks"] = total
            result["progress_percent"] = round(current / total * 100) if total > 0 else 0

        if result["status"] in ("done", "done_with_warnings"):
            result["progress_percent"] = 100

        failed = progress.get("failed_chunks", [])
        if failed:
            result["failed_chunks"] = len(failed)

        glossary = progress.get("glossary", {})
        if glossary.get("terms"):
            result["glossary_count"] = len(glossary["terms"])
        if glossary.get("seed_terms") is not None:
            result["glossary_seed_count"] = len(glossary.get("seed_terms") or {})
        if glossary.get("document_terms") is not None:
            result["glossary_document_count"] = len(glossary.get("document_terms") or {})
        if glossary.get("extraction"):
            result["glossary_extraction"] = glossary.get("extraction")
        if glossary.get("locked"):
            result["glossary_locked_count"] = len(glossary["locked"])
        if glossary.get("awaiting_review") and not glossary.get("approved"):
            result["awaiting_glossary_review"] = True
        style_anchor = progress.get("style_anchor") or {}
        if style_anchor:
            result["style_anchor"] = {
                "en": style_anchor.get("en", ""),
                "vi": style_anchor.get("vi", ""),
                "source_model": style_anchor.get("source_model", ""),
                "approved": bool(style_anchor.get("approved", False)),
                "awaiting_review": bool(style_anchor.get("awaiting_review", False)),
            }
            if style_anchor.get("awaiting_review") and not style_anchor.get("approved"):
                result["awaiting_style_review"] = True

        validation = progress.get("validation")
        if validation:
            result["validation"] = validation

        quality = progress.get("quality")
        if quality:
            result["quality_score"] = quality.get("score", 0)
            result["quality_issues"] = quality.get("issue_count", 0)

        diagnostics = progress.get("diagnostics")
        if diagnostics and diagnostics.get("primary_cause"):
            result["diagnostic_cause"] = diagnostics["primary_cause"]
            result["diagnostic_cause_label"] = diagnostics.get("primary_cause_label", "")
            result["diagnostic_severity"] = diagnostics.get("overall_severity", "")
            result["diagnostic_summary"] = diagnostics.get("summary", "")

        # Structured error from a failed/retrying run. Surface it so the UI
        # can show *where* (phase + chunk) and *why* (type + message) instead
        # of just the opaque status string.
        error_detail = progress.get("error_detail")
        if error_detail:
            result["error_detail"] = {
                "type": error_detail.get("type"),
                "message": error_detail.get("message"),
                "phase": error_detail.get("phase"),
                "chunk_idx_at_error": error_detail.get("chunk_idx_at_error"),
                "timestamp": error_detail.get("timestamp"),
                "attempts_used": error_detail.get("attempts_used"),
                "max_attempts": error_detail.get("max_attempts"),
            }
            # Keep traceback out of the polling response (can be 4KB).
            # Frontend can fetch it via the dedicated endpoint if needed.
            result["error_detail"]["has_traceback"] = bool(error_detail.get("traceback"))

        # Resume hint — how many chunks are durable on disk and which one
        # was being worked on when the previous run died.
        translated_chunks = progress.get("translated_chunks") or {}
        if isinstance(translated_chunks, dict) and translated_chunks:
            result["translated_chunks_count"] = len(translated_chunks)
        total_chunks = progress.get("total_chunks")
        if total_chunks and "total_chunks" not in result:
            completed_chunks = len(translated_chunks) if isinstance(translated_chunks, dict) else 0
            result["current_chunk"] = completed_chunks
            result["total_chunks"] = int(total_chunks)
            result["progress_percent"] = round(completed_chunks / int(total_chunks) * 100) if int(total_chunks) > 0 else 0
        if "last_attempted_chunk_idx" in progress:
            result["last_attempted_chunk_idx"] = progress["last_attempted_chunk_idx"]
        # In-flight attempt counter — updated on every prompt send so the
        # UI can show real progress during retry/truncation loops.
        if "current_chunk_attempt" in progress:
            result["current_chunk_attempt"] = progress["current_chunk_attempt"]
        if progress.get("current_chunk_attempt_label"):
            result["current_chunk_attempt_label"] = progress["current_chunk_attempt_label"]

    original_pdf = os.path.join(job_dir, "original.pdf")
    translated_pdf = os.path.join(job_dir, "output", "translated.pdf")

    if os.path.exists(original_pdf):
        result["original_pdf_url"] = f"/api/pdf-translate/{job_id}/original"
    if os.path.exists(translated_pdf):
        result["translated_pdf_url"] = f"/api/pdf-translate/{job_id}/translated"

    return result


@router.get("/{job_id}/error-detail")
async def get_error_detail(job_id: str, owner: str = Depends(_owner_or_401)):
    """Full structured error from the last failed run, including traceback.

    Polling /status returns a trimmed summary (no traceback) for size; this
    endpoint serves the full payload on demand.
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner, must_exist=False)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "No progress data found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    detail = progress.get("error_detail")
    if not detail:
        return {"job_id": job_id, "error_detail": None}
    return {"job_id": job_id, "error_detail": detail}


@router.post("/{job_id}/pause")
async def pause_job(job_id: str, owner: str = Depends(_owner_or_401)):
    """Request a soft pause without marking the model as failed."""
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner, must_exist=False)
    progress_file = os.path.join(job_dir, "progress.json")
    progress = {}
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
        except Exception:
            progress = {}
    progress["pause_requested"] = True
    progress["paused_at"] = time.time()
    if not str(progress.get("status", "")).startswith("done"):
        progress["status"] = "pausing"
    atomic_write_json(progress_file, progress)
    return {"status": "pausing", "job_id": job_id}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, owner: str = Depends(_owner_or_401)):
    """Cancel a running PDF translation job."""
    _check_owner(job_id, owner)
    _dispatch_stop(job_id)
    return {"status": "cancelled"}


@router.post("/{job_id}/compile-partial")
async def compile_partial(job_id: str, owner: str = Depends(_owner_or_401)):
    """Compile a partial PDF from whatever chunks have been translated so far.

    Useful for long books — user can preview progress without waiting for
    the entire translation to finish.
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    pdf_path = os.path.join(job_dir, "original.pdf")
    progress_file = os.path.join(job_dir, "progress.json")

    if not os.path.exists(pdf_path):
        raise HTTPException(404, "Original PDF not found")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "No progress data found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    translated_chunks = progress.get("translated_chunks", {})
    if not translated_chunks:
        raise HTTPException(400, "No chunks translated yet")

    # Re-extract blocks and apply translated chunks
    all_blocks = extract_text_blocks(pdf_path)
    chunks = split_blocks_into_chunks(all_blocks)

    applied = 0
    for chunk_key, translated_text in translated_chunks.items():
        idx = int(chunk_key)
        if 0 <= idx < len(chunks):
            parse_translated_chunk(translated_text, chunks[idx])
            applied += 1

    # Build partial PDF
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "translated.pdf")
    rebuild_pdf(pdf_path, all_blocks, output_path)

    return {
        "job_id": job_id,
        "status": "compiled",
        "chunks_applied": applied,
        "total_chunks": len(chunks),
        "translated_pdf_url": f"/api/pdf-translate/{job_id}/translated",
    }


@router.get("/{job_id}/quality")
async def get_quality_report(job_id: str, owner: str = Depends(_owner_or_401)):
    """Get the full quality report for a completed job."""
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    quality = progress.get("quality")
    if not quality:
        raise HTTPException(404, "Quality report not available (job not completed?)")

    return {"job_id": job_id, **quality}


# ── ChrF + BERTScore evaluation (reference-based) ──────────────────

class EvaluationSegment(BaseModel):
    hypothesis: str   # machine-translated VI text
    reference: str    # human reference VI text


class EvaluationRequest(BaseModel):
    segments: list[EvaluationSegment]
    run_chrf: bool = True
    run_bertscore: bool = False   # optional, requires bert-score + transformers
    bertscore_model: str = "vinai/phobert-base"


@router.post("/{job_id}/evaluate")
async def run_evaluation(
    job_id: str,
    req: EvaluationRequest,
    owner: str = Depends(_owner_or_401),
):
    """Run ChrF++ and/or BERTScore-VI evaluation given reference translations.

    This endpoint computes reference-based metrics (ChrF++, BERTScore with PhoBERT)
    for a completed translation job. You must provide human reference translations.

    Metrics:
    - ChrF++ (Popović 2015): character n-gram F-score, validated for Vietnamese/
      isolating languages. No tokenizer needed. Sacrebleu backend if installed.
    - BERTScore-VI (Zhang 2020 + PhoBERT): semantic similarity using
      vinai/phobert-base. Requires: pip install bert-score transformers.

    Body:
        segments: list of {hypothesis, reference} pairs
        run_chrf: compute ChrF++ (default true, lightweight)
        run_bertscore: compute BERTScore-VI (default false, ~540MB model download)
        bertscore_model: PhoBERT model variant (default vinai/phobert-base)
    """
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    if not req.segments:
        raise HTTPException(400, "segments list is empty")

    hypotheses = [s.hypothesis for s in req.segments]
    references = [s.reference for s in req.segments]

    result = {"job_id": job_id, "num_segments": len(req.segments)}

    # ChrF++
    if req.run_chrf:
        from app.pdf.chrf_vi import compute_chrf
        chrf_report = compute_chrf(hypotheses, references)
        result["chrf"] = chrf_report.to_dict()

    # BERTScore with PhoBERT
    if req.run_bertscore:
        from app.pdf.bertscore_vi import compute_bertscore
        bs_report = compute_bertscore(
            hypotheses, references,
            model_name=req.bertscore_model,
        )
        result["bertscore"] = bs_report.to_dict()

    # Persist to progress.json
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
        progress["evaluation"] = result
        atomic_write_json(progress_file, progress)
    except Exception as e:
        logger.warning(f"Could not save evaluation to progress.json: {e}")

    return result


@router.get("/{job_id}/evaluate")
async def get_evaluation_report(job_id: str, owner: str = Depends(_owner_or_401)):
    """Get the cached evaluation report (ChrF++, BERTScore-VI) for a job."""
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    evaluation = progress.get("evaluation")
    if not evaluation:
        raise HTTPException(
            404,
            "Evaluation report not available. "
            "POST to /{job_id}/evaluate with reference translations first."
        )
    return evaluation


@router.get("/metrics/availability")
async def metrics_availability():
    """Check availability of all evaluation metrics and their dependencies."""
    from app.pdf.chrf_vi import _try_sacrebleu_chrf
    from app.pdf.bertscore_vi import is_available as bs_available, unavailable_reason as bs_reason

    # Check sacrebleu
    sacrebleu_ok = _try_sacrebleu_chrf(["test"], ["test"]) is not None

    return {
        "metrics": {
            "chrf_plus_plus": {
                "available": True,   # always available (pure-python fallback)
                "sacrebleu_backend": sacrebleu_ok,
                "note": "ChrF++ — character n-gram F-score (Popović 2015). "
                        "Install sacrebleu for standard backend: pip install sacrebleu",
            },
            "bertscore_phobert": {
                "available": bs_available(),
                "model": "vinai/phobert-base",
                "unavailable_reason": bs_reason(),
                "note": "BERTScore with PhoBERT (Nguyen 2020). "
                        "Install: pip install bert-score transformers",
            },
            "heuristic_quality": {
                "available": True,
                "note": "Heuristic checks (quality.py) — always available, no dependencies",
            },
            "reference_free_metrics": {
                "available": True,
                "note": "Coverage, Vietnamese ratio, math preservation, fluency (metrics.py)",
            },
        }
    }


@router.get("/{job_id}/glossary")
async def get_glossary(job_id: str, owner: str = Depends(_owner_or_401)):
    """Get the current glossary for a job."""
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    glossary_data = progress.get("glossary", {"terms": {}, "enabled": True})
    from app.pdf.glossary import normalize_locked
    locked = normalize_locked(glossary_data.get("locked"))
    return {
        "job_id": job_id,
        "terms": glossary_data.get("terms", {}),
        "enabled": glossary_data.get("enabled", True),
        "locked": locked,
        "fields": glossary_data.get("fields", {}),   # en → lĩnh vực
        "count": len(glossary_data.get("terms", {})),
    }


def _normalize_glossary_fields(fields: dict[str, str] | None) -> dict[str, str]:
    """en (any case) → lĩnh vực, lowercased keys, trimmed/capped values, blanks dropped."""
    if not fields:
        return {}
    out: dict[str, str] = {}
    for k, v in fields.items():
        key = (k or "").lower().strip()
        val = (str(v) if v is not None else "").strip()[:64]
        if key and val:
            out[key] = val
    return out


class GlossaryUpdate(BaseModel):
    terms: dict[str, str] | None = None
    enabled: bool | None = None
    locked: list[str] | None = None
    fields: dict[str, str] | None = None   # en → lĩnh vực


@router.put("/{job_id}/glossary")
async def update_glossary(
    job_id: str,
    body: GlossaryUpdate,
    owner: str = Depends(_owner_or_401),
):
    """Update the glossary for a job (user edits).

    `locked` is a list of EN keys (case-insensitive) that the user has marked
    as inviolable; the pipeline elevates them in the prompt and never overwrites
    their VI translation when discovering new terms.
    """
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    glossary_data = progress.get("glossary", {"terms": {}, "enabled": True})

    if body.terms is not None:
        glossary_data["terms"] = body.terms
    if body.enabled is not None:
        glossary_data["enabled"] = body.enabled
    if body.locked is not None:
        from app.pdf.glossary import normalize_locked
        glossary_data["locked"] = normalize_locked(body.locked)
    if body.fields is not None:
        glossary_data["fields"] = _normalize_glossary_fields(body.fields)

    progress["glossary"] = glossary_data
    atomic_write_json(progress_file, progress)

    return {
        "job_id": job_id,
        "terms": glossary_data["terms"],
        "enabled": glossary_data["enabled"],
        "locked": glossary_data.get("locked", []),
        "fields": glossary_data.get("fields", {}),
        "count": len(glossary_data["terms"]),
    }


class GlossaryApprove(BaseModel):
    terms: dict[str, str] | None = None
    enabled: bool | None = None
    locked: list[str] | None = None
    fields: dict[str, str] | None = None   # en → lĩnh vực


class StyleAnchorApprove(BaseModel):
    en: str | None = None
    vi: str | None = None


@router.post("/{job_id}/approve-glossary")
async def approve_glossary(
    job_id: str,
    body: GlossaryApprove,
    owner: str = Depends(_owner_or_401),
):
    """Approve the glossary and resume translation.

    Called when the user clicks "Bắt đầu dịch" after reviewing the auto-extracted
    glossary. Optionally accepts last-second edits to terms/enabled/locked
    (same shape as PUT /glossary). Marks the glossary as approved, clears the
    awaiting_review flag, and relaunches the pipeline subprocess.
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    pdf_path = os.path.join(job_dir, "original.pdf")

    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    if not os.path.exists(pdf_path):
        raise HTTPException(404, "Original PDF not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    glossary_data = progress.get("glossary", {"terms": {}, "enabled": True})

    if body.terms is not None:
        glossary_data["terms"] = body.terms
    if body.enabled is not None:
        glossary_data["enabled"] = body.enabled
    if body.locked is not None:
        from app.pdf.glossary import normalize_locked
        glossary_data["locked"] = normalize_locked(body.locked)
    if body.fields is not None:
        glossary_data["fields"] = _normalize_glossary_fields(body.fields)

    glossary_data["approved"] = True
    glossary_data["awaiting_review"] = False
    progress["glossary"] = glossary_data

    style_anchor = progress.get("style_anchor") or {}
    if style_anchor.get("awaiting_review") and not style_anchor.get("approved"):
        progress["status"] = "awaiting_style_review"
        progress["phase"] = "style_anchor_review"
        atomic_write_json(progress_file, progress)
        return {
            "job_id": job_id,
            "status": "awaiting_style_review",
            "approved": True,
            "term_count": len(glossary_data["terms"]),
            "locked_count": len(glossary_data.get("locked", [])),
            "style_anchor": {
                "en": style_anchor.get("en", ""),
                "vi": style_anchor.get("vi", ""),
                "source_model": style_anchor.get("source_model", ""),
            },
        }

    progress["status"] = "starting"
    atomic_write_json(progress_file, progress)

    mode = progress.get("mode", "standard")
    agentic = bool(progress.get("agentic", False))
    num_tabs = _clamp_tabs(progress.get("num_tabs", 2))
    models = parse_model_preference(progress.get("models"))
    superseded_jobs = _supersede_older_pdf_jobs_for_document(
        owner=owner,
        new_job_id=job_id,
        base_job_id=progress.get("base_job_id") or _pdf_job_id_base(progress.get("original_filename") or job_id),
        title=progress.get("title", ""),
        page_count=progress.get("page_count"),
    )
    if superseded_jobs:
        progress["superseded_jobs"] = superseded_jobs
        atomic_write_json(progress_file, progress)
    _dispatch_pdf_start(job_id, pdf_path, _user_work_dir(owner),
                        mode=mode, agentic=agentic, num_tabs=num_tabs,
                        models=models,
                        judge_backend=progress.get("judge_backend", "web"))

    return {
        "job_id": job_id,
        "status": "started",
        "approved": True,
        "term_count": len(glossary_data["terms"]),
        "locked_count": len(glossary_data.get("locked", [])),
        "superseded_jobs": superseded_jobs,
    }


@router.get("/{job_id}/style-anchor")
async def get_style_anchor(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner, must_exist=False)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    anchor = progress.get("style_anchor") or {}
    return {
        "job_id": job_id,
        "style_anchor": {
            "en": anchor.get("en", ""),
            "vi": anchor.get("vi", ""),
            "source_model": anchor.get("source_model", ""),
            "approved": bool(anchor.get("approved", False)),
            "awaiting_review": bool(anchor.get("awaiting_review", False)),
        },
    }


@router.post("/{job_id}/approve-style-anchor")
async def approve_style_anchor(
    job_id: str,
    body: StyleAnchorApprove,
    owner: str = Depends(_owner_or_401),
):
    """Duyệt mẫu văn phong rồi resume pipeline vào eval-loop."""
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    pdf_path = os.path.join(job_dir, "original.pdf")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    if not os.path.exists(pdf_path):
        raise HTTPException(404, "Original PDF not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    anchor = progress.get("style_anchor") or {}
    if body.en is not None:
        anchor["en"] = body.en.strip()
    if body.vi is not None:
        anchor["vi"] = body.vi.strip()
    if not anchor.get("en") or not anchor.get("vi"):
        raise HTTPException(400, "Mẫu văn phong chưa đủ nguồn và bản dịch")

    anchor["approved"] = True
    anchor["awaiting_review"] = False
    progress["style_anchor"] = anchor
    progress["status"] = "starting"
    progress["phase"] = "style_anchor_review"
    atomic_write_json(progress_file, progress)

    mode = progress.get("mode", "standard")
    agentic = bool(progress.get("agentic", False))
    num_tabs = _clamp_tabs(progress.get("num_tabs", 2))
    models = parse_model_preference(progress.get("models"))
    superseded_jobs = _supersede_older_pdf_jobs_for_document(
        owner=owner,
        new_job_id=job_id,
        base_job_id=progress.get("base_job_id") or _pdf_job_id_base(progress.get("original_filename") or job_id),
        title=progress.get("title", ""),
        page_count=progress.get("page_count"),
    )
    if superseded_jobs:
        progress["superseded_jobs"] = superseded_jobs
        atomic_write_json(progress_file, progress)
    _dispatch_pdf_start(job_id, pdf_path, _user_work_dir(owner),
                        mode=mode, agentic=agentic, num_tabs=num_tabs,
                        models=models,
                        judge_backend=progress.get("judge_backend", "web"))

    return {
        "job_id": job_id,
        "status": "started",
        "approved": True,
        "style_anchor": anchor,
        "superseded_jobs": superseded_jobs,
    }


@router.get("/{job_id}/chunk-map")
async def get_chunk_block_map(job_id: str, owner: str = Depends(_owner_or_401)):
    """Return the chunk → block bbox map for the PDF overlay.

    Used by the frontend PdfViewer to render clickable regions per text block,
    so users can deep-link from the original PDF straight into HistoryEditor at
    the corresponding chunk.
    """
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    cb_map = progress.get("chunk_block_map")
    if not cb_map:
        raise HTTPException(404, "chunk_block_map not built yet — finish translating once to populate it")

    return {"job_id": job_id, **cb_map}


# ── Glossary packs (predefined domain glossaries users can import) ───────

class ImportPacksRequest(BaseModel):
    pack_ids: list[str]


@router.get("/glossary-packs")
async def list_glossary_packs():
    """List all available domain packs (metadata only, no terms)."""
    from app.pdf.term_packs import list_packs
    return {"packs": list_packs()}


@router.get("/glossary-packs/{pack_id}")
async def get_glossary_pack(pack_id: str):
    """Return one pack's full content including its term map."""
    from app.pdf.term_packs import get_pack
    pack = get_pack(pack_id)
    if not pack:
        raise HTTPException(404, f"Glossary pack '{pack_id}' not found")
    return pack


@router.post("/{job_id}/import-packs")
async def import_glossary_packs(
    job_id: str,
    body: ImportPacksRequest,
    owner: str = Depends(_owner_or_401),
):
    """Merge selected domain packs into the job's glossary.

    First-wins: existing entries (Gemini-extracted, user-edited, locked) are
    never overwritten — packs only fill gaps. Returns counts so the UI can
    show "Đã thêm N thuật ngữ mới (M đã có)".
    """
    from app.pdf.term_packs import merge_packs_into_glossary
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    if not body.pack_ids:
        raise HTTPException(400, "pack_ids must be a non-empty list")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    glossary_data = progress.get("glossary", {"terms": {}, "enabled": True})
    existing_terms = glossary_data.get("terms", {}) or {}

    merged, added, skipped, missing = merge_packs_into_glossary(
        existing_terms, body.pack_ids,
    )

    glossary_data["terms"] = merged
    progress["glossary"] = glossary_data
    atomic_write_json(progress_file, progress)

    return {
        "job_id": job_id,
        "added": added,
        "skipped": skipped,
        "missing_packs": missing,
        "total": len(merged),
        "terms": merged,
    }


def _friendly_pdf_basename(job_id: str, job_dir: str) -> str:
    """Derive a download-friendly basename (no extension) for a PDF job.

    Priority: original_filename → title → job_id stripped of the "pdf_" prefix.
    Sanitized to be safe across OSes.
    """
    base = ""
    progress_file = os.path.join(job_dir, "progress.json")
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
            orig = progress.get("original_filename")
            if orig:
                base = os.path.splitext(orig)[0]
            elif progress.get("title"):
                base = progress["title"]
        except Exception:
            pass
    if not base:
        base = job_id[4:] if job_id.startswith("pdf_") else job_id
    # Strip path separators + control chars; keep spaces and dots
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", base).strip()
    return base or "document"


@router.get("/{job_id}/original")
async def serve_original(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    path = os.path.join(job_dir, "original.pdf")
    if not os.path.exists(path):
        raise HTTPException(404, "Original PDF not found")
    filename = f"{_friendly_pdf_basename(job_id, job_dir)}.pdf"
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.get("/{job_id}/translated")
async def serve_translated(job_id: str, owner: str = Depends(_owner_or_401)):
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    path = os.path.join(job_dir, "output", "translated.pdf")
    if not os.path.exists(path):
        raise HTTPException(404, "Translated PDF not found")
    filename = f"{_friendly_pdf_basename(job_id, job_dir)}_vi_translated.pdf"
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.get("/jobs")
async def list_jobs(owner: str = Depends(_owner_or_401)):
    """List PDF translation jobs visible to the caller.

    Each user sees their own jobs. Admin additionally sees legacy
    `workspace/jobs/` (pre-multi-user).
    """
    # Roots to scan
    roots: list[str] = []
    user_root = os.path.join(WORKSPACE, "users", safe_username(owner), "jobs")
    if os.path.isdir(user_root):
        roots.append(user_root)
    if _is_admin(owner):
        legacy = legacy_jobs_dir(WORKSPACE)
        if os.path.isdir(legacy):
            roots.append(legacy)

    jobs = []
    seen: set[str] = set()
    for jobs_root in roots:
        for name in os.listdir(jobs_root):
            if not name.startswith("pdf_") or name in seen:
                continue
            seen.add(name)
            job_dir = os.path.join(jobs_root, name)
            if not os.path.isdir(job_dir):
                continue

            job_info = {
                "job_id": name,
                "source_type": "pdf_only",
                "has_original_pdf": os.path.exists(os.path.join(job_dir, "original.pdf")),
                "has_translated_pdf": os.path.exists(os.path.join(job_dir, "output", "translated.pdf")),
                "status": "unknown",
                "progress_percent": 0,
            }

            progress_file = os.path.join(job_dir, "progress.json")
            if os.path.exists(progress_file):
                with open(progress_file, "r", encoding="utf-8") as f:
                    progress = json.load(f)
                job_info["status"] = progress.get("status", "pending")
                job_info["title"] = progress.get("title", "")
                job_info["page_count"] = progress.get("page_count", 0)
                if progress.get("original_filename"):
                    job_info["original_filename"] = progress["original_filename"]

                m = re.match(r"translating (\d+)/(\d+)", job_info["status"])
                if m:
                    current, total = int(m.group(1)), int(m.group(2))
                    job_info["progress_percent"] = round(current / total * 100) if total > 0 else 0
                elif job_info["status"] in ("done", "done_with_warnings"):
                    job_info["progress_percent"] = 100

                validation = progress.get("validation")
                if validation:
                    job_info["validation"] = validation

                quality = progress.get("quality")
                if quality:
                    job_info["quality_score"] = quality.get("score", 0)
                    job_info["quality_issues"] = quality.get("issue_count", 0)

            jobs.append(job_info)

    return {"jobs": jobs}


def _collect_judge_pairs(job_dir: str, progress: dict) -> list[dict]:
    """Gather (src, mt) pairs for any judge backend (Ollama / Gemini / LaBSE).

    Tries chunk files on disk first (PDF pipeline), falls back to
    progress.json input_chunks/translated_chunks (LaTeX pipeline).
    Returns: list of {"index", "src", "mt", "score_pct"} dicts.
    """
    pairs: list[dict] = []

    chunk_dir = os.path.join(job_dir, "chunks")
    if os.path.isdir(chunk_dir):
        import glob as _glob
        orig_files = sorted(_glob.glob(os.path.join(chunk_dir, "chunk_*_original.txt")))
        for orig_path in orig_files:
            trans_path = orig_path.replace("_original.txt", "_translated.txt")
            if not os.path.exists(trans_path):
                continue
            try:
                with open(orig_path, encoding="utf-8") as f:
                    src = f.read().strip()
                with open(trans_path, encoding="utf-8") as f:
                    mt = f.read().strip()
                if src and mt and len(src) >= 20:
                    pairs.append({
                        "index": len(pairs), "src": src, "mt": mt, "score_pct": 50,
                    })
            except Exception:
                continue

    if not pairs:
        input_chunks = progress.get("input_chunks", {})
        translated_chunks = progress.get("translated_chunks", {})
        for key in sorted(input_chunks.keys(),
                          key=lambda k: int(k) if k.isdigit() else 0):
            src = (input_chunks.get(key) or "").strip()
            mt = (translated_chunks.get(key) or "").strip()
            if src and mt and len(src) >= 20:
                pairs.append({
                    "index": int(key) if key.isdigit() else len(pairs),
                    "src": src, "mt": mt, "score_pct": 50,
                })

    return pairs


class JudgeRequest(BaseModel):
    model: str = "qwen2.5:32b"   # Default upgraded — Qwen 2.5 32B is the new VI baseline
    max_segments: int = 10       # Max segments to judge (LLM is slow)
    low_score_threshold: float = 0.70  # Quality threshold (0..1) — segments below are prioritised


class GeminiJudgeRequest(BaseModel):
    """Gemini-as-Judge — uses Playwright + the existing Gemini web session."""
    max_segments: int = 10
    low_score_threshold: float = 0.70
    new_session_every: int = 5   # Open new chat every N segments (avoid context bloat)


class WebJudgeRequest(BaseModel):
    """Cross-model web judge — driven by a web AI DIFFERENT from the translator.

    `judge_backend` None → auto-pick a backend ≠ translator (avoids self-judging
    bias). If supplied but equal to the translator, it is overridden.
    """
    judge_backend: str | None = None   # "chatgpt" | "deepseek" | "gemini" | None
    max_segments: int = 10
    low_score_threshold: float = 0.70
    new_session_every: int = 5


@router.post("/{job_id}/judge")
async def run_llm_judge(
    job_id: str,
    req: JudgeRequest,
    owner: str = Depends(_owner_or_401),
):
    """Run LLM-as-Judge on segments of a completed PDF job.

    Sends source/translation pairs to a local Ollama model for MQM-style
    error analysis. Returns per-segment verdict, errors, and improvement
    suggestions.
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    translated_pdf = os.path.join(job_dir, "output", "translated.pdf")

    # Accept: status is "done"/"done_with_warnings" OR translated PDF exists
    status = progress.get("status", "")
    is_done = status in ("done", "done_with_warnings") or os.path.exists(translated_pdf)
    if not is_done:
        raise HTTPException(400, f"Job must be completed before running LLM judge (current status: {status})")

    # ── Collect source/translation pairs ──────────────────────────
    pairs = _collect_judge_pairs(job_dir, progress)
    if not pairs:
        raise HTTPException(404, "No translation pairs found. Make sure the job completed successfully.")

    try:
        from app.pdf.llm_judge import judge_segments_batch, is_available
        if not is_available(req.model):
            raise HTTPException(503, f"Model '{req.model}' not available. Install Ollama and run: ollama pull {req.model}")

        results = judge_segments_batch(
            pairs=pairs,
            model=req.model,
            max_segments=req.max_segments,
            low_score_threshold=req.low_score_threshold * 100,
        )

        # Compute summary stats (use MQM-computed score, not LLM self-score)
        judged = [r for r in results if r.get("llm_result")]
        avg_score = round(sum(r["llm_result"].get("mqm_score", r["llm_result"]["score"]) for r in judged) / len(judged)) if judged else None
        error_counts = {}
        for r in judged:
            for e in (r["llm_result"].get("errors") or []):
                cat = e.get("category", "other")
                error_counts[cat] = error_counts.get(cat, 0) + 1

        # Cache results in progress.json (atomic write to avoid race with pipeline)
        judge_cache = {
            "model": req.model,
            "num_judged": len(judged),
            "avg_score": avg_score,
            "error_counts": error_counts,
            "results": results,
        }
        progress["llm_judge"] = judge_cache
        atomic_write_json(progress_file, progress)

        return {
            "job_id": job_id,
            "model": req.model,
            "num_judged": len(judged),
            "avg_score": avg_score,
            "error_counts": error_counts,
            "results": results,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[LLMJudge] Error in judge endpoint: {e}")
        raise HTTPException(500, f"LLM Judge failed: {e}")


@router.get("/{job_id}/judge")
async def get_llm_judge_report(job_id: str, owner: str = Depends(_owner_or_401)):
    """Get cached LLM Judge report for a job."""
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    judge = progress.get("llm_judge")
    if not judge:
        raise HTTPException(404, "No LLM Judge report available. Run POST /{job_id}/judge first.")

    return {"job_id": job_id, **judge}


# ── Gemini-as-Judge ────────────────────────────────────────────────
# Reuses Playwright + the user's Gemini Pro session — strongest available
# judge model. Runs as a separate cached field ("gemini_judge") so users
# can compare alongside the Ollama judge.

@router.post("/{job_id}/judge/gemini")
async def run_gemini_judge(
    job_id: str,
    req: GeminiJudgeRequest,
    owner: str = Depends(_owner_or_401),
):
    """Run Gemini-as-Judge on segments via Playwright.

    Slower than Ollama (each call goes through the browser) but uses a
    much stronger model. Recommended for ≤10 segments at a time.
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    translated_pdf = os.path.join(job_dir, "output", "translated.pdf")
    status = progress.get("status", "")
    is_done = status in ("done", "done_with_warnings") or os.path.exists(translated_pdf)
    if not is_done:
        raise HTTPException(400, f"Job must be completed before running Gemini judge (current status: {status})")

    pairs = _collect_judge_pairs(job_dir, progress)
    if not pairs:
        raise HTTPException(404, "No translation pairs found.")

    try:
        from app.pdf.gemini_judge import judge_segments_batch
        results = await judge_segments_batch(
            pairs=pairs,
            max_segments=req.max_segments,
            low_score_threshold=req.low_score_threshold * 100,
            new_session_every=req.new_session_every,
        )

        judged = [r for r in results if r.get("llm_result")]
        avg_score = (
            round(sum(r["llm_result"].get("mqm_score",
                                          r["llm_result"]["score"])
                      for r in judged) / len(judged))
            if judged else None
        )
        error_counts: dict[str, int] = {}
        for r in judged:
            for e in (r["llm_result"].get("errors") or []):
                cat = e.get("category", "other")
                error_counts[cat] = error_counts.get(cat, 0) + 1

        cache = {
            "model": "gemini-web",
            "num_judged": len(judged),
            "avg_score": avg_score,
            "error_counts": error_counts,
            "results": results,
        }
        progress["gemini_judge"] = cache
        atomic_write_json(progress_file, progress)

        return {"job_id": job_id, **cache}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[GeminiJudge] Error: {e}")
        raise HTTPException(500, f"Gemini Judge failed: {e}")


@router.get("/{job_id}/judge/gemini")
async def get_gemini_judge_report(job_id: str, owner: str = Depends(_owner_or_401)):
    """Get cached Gemini Judge report for a job."""
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    cache = progress.get("gemini_judge")
    if not cache:
        raise HTTPException(404, "No Gemini Judge report available. Run POST /{job_id}/judge/gemini first.")
    return {"job_id": job_id, **cache}


# ── Cross-model web judge ──────────────────────────────────────────
# Drives a web AI DIFFERENT from the translator (e.g. translate=Gemini →
# judge=ChatGPT/DeepSeek) via Playwright. Independent cross-model signal
# without Ollama — dampens the self-judging bias of gemini_judge.

@router.post("/{job_id}/judge/web")
async def run_web_judge(
    job_id: str,
    req: WebJudgeRequest,
    owner: str = Depends(_owner_or_401),
):
    """Run a cross-model web judge on segments via Playwright.

    The judge backend is forced to differ from the translation backend, so a
    Gemini-translated paper is graded by ChatGPT or DeepSeek. Slower than
    Ollama (each verdict goes through the browser) — keep ≤10 segments.
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    translated_pdf = os.path.join(job_dir, "output", "translated.pdf")
    status = progress.get("status", "")
    is_done = status in ("done", "done_with_warnings") or os.path.exists(translated_pdf)
    if not is_done:
        raise HTTPException(400, f"Job must be completed before running web judge (current status: {status})")

    pairs = _collect_judge_pairs(job_dir, progress)
    if not pairs:
        raise HTTPException(404, "No translation pairs found.")

    # Translator backend used for THIS job (fall back to global setting).
    translator_backend = progress.get("ai_backend") or settings.AI_BACKEND

    try:
        from app.pdf.web_judge import judge_segments_batch
        report = await judge_segments_batch(
            pairs=pairs,
            judge_backend=req.judge_backend,
            translator_backend=translator_backend,
            max_segments=req.max_segments,
            low_score_threshold=req.low_score_threshold * 100,
            new_session_every=req.new_session_every,
        )

        progress["web_judge"] = report
        atomic_write_json(progress_file, progress)

        return {"job_id": job_id, **report}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[WebJudge] Error: {e}")
        raise HTTPException(500, f"Web Judge failed: {e}")


@router.get("/{job_id}/judge/web")
async def get_web_judge_report(job_id: str, owner: str = Depends(_owner_or_401)):
    """Get cached cross-model web judge report for a job."""
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    cache = progress.get("web_judge")
    if not cache:
        raise HTTPException(404, "No web judge report available. Run POST /{job_id}/judge/web first.")
    return {"job_id": job_id, **cache}


@router.get("/{job_id}/diagnostics")
async def get_diagnostics(
    job_id: str,
    refresh: bool = False,
    owner: str = Depends(_owner_or_401),
):
    """Get or recompute auto-diagnostic report for a job.

    ?refresh=true forces recomputation even if a cached report exists.
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    cached = progress.get("diagnostics")
    if cached and not refresh:
        return {"job_id": job_id, **cached}

    # (Re)compute
    try:
        from app.pdf.diagnostics import run_diagnostics
        report = run_diagnostics(job_id, job_dir, progress)
        result = report.to_dict()
        progress["diagnostics"] = result
        atomic_write_json(progress_file, progress)
        return {"job_id": job_id, **result}
    except Exception as e:
        raise HTTPException(500, f"Diagnostics failed: {e}")


# ── Audit trail endpoints ────────────────────────────────────────

@router.get("/{job_id}/audit")
async def get_audit_log(
    job_id: str,
    limit: int = 500,
    offset: int = 0,
    event_type: str | None = None,
    phase: str | None = None,
    since_seq: int | None = None,
    owner: str = Depends(_owner_or_401),
):
    """Đọc audit log JSONL của job theo trang.

    Query params:
      - limit: số event tối đa trả về (default 500, max 5000)
      - offset: bỏ qua N event đầu tiên (sau khi filter)
      - event_type: lọc theo prefix (vd 'chunk.', 'scheduler.', 'judge.')
      - phase: lọc theo phase ('translating', 'rebuilding', ...)
      - since_seq: chỉ trả event có seq > since_seq (tail-friendly)

    Trả về `{events, total_filtered, total_in_file, has_more, env_snapshot}`.
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner, must_exist=False)
    audit_path = os.path.join(job_dir, "audit.jsonl")
    env_path = os.path.join(job_dir, "env_snapshot.json")

    if not os.path.exists(audit_path):
        return {
            "job_id": job_id,
            "events": [],
            "total_filtered": 0,
            "total_in_file": 0,
            "has_more": False,
            "env_snapshot": None,
        }

    # Cap limit to prevent OOM
    limit = max(1, min(limit, 5000))
    offset = max(0, offset)

    events = []
    total_in_file = 0
    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            for line in f:
                total_in_file += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_seq is not None and ev.get("seq", 0) <= since_seq:
                    continue
                if event_type and not ev.get("event_type", "").startswith(event_type):
                    continue
                if phase and ev.get("phase") != phase:
                    continue
                events.append(ev)
    except Exception as e:
        raise HTTPException(500, f"Could not read audit log: {e}")

    total_filtered = len(events)
    paged = events[offset: offset + limit]
    has_more = offset + limit < total_filtered

    env_snapshot = None
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                env_snapshot = json.load(f)
        except Exception:
            env_snapshot = None

    return {
        "job_id": job_id,
        "events": paged,
        "total_filtered": total_filtered,
        "total_in_file": total_in_file,
        "has_more": has_more,
        "env_snapshot": env_snapshot,
    }


@router.get("/{job_id}/audit/raw")
async def get_audit_raw_response(
    job_id: str,
    name: str,
    owner: str = Depends(_owner_or_401),
):
    """Lấy nội dung file prompt/response gốc trong audit_responses/.

    `name` là relative path trả về từ event (vd `audit_responses/chunk_001_attempt_1_prompt.txt`).
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner, must_exist=False)

    # Path-traversal guard: chỉ cho phép file trong audit_responses/
    safe_name = name.replace("\\", "/").lstrip("/")
    if ".." in safe_name.split("/") or not safe_name.startswith("audit_responses/"):
        raise HTTPException(400, "Invalid audit file name")

    full = os.path.join(job_dir, safe_name)
    if not os.path.isfile(full):
        raise HTTPException(404, "Audit file not found")

    # Defence in depth: realpath check
    real_dir = os.path.realpath(os.path.join(job_dir, "audit_responses"))
    real_full = os.path.realpath(full)
    if not real_full.startswith(real_dir + os.sep) and real_full != real_dir:
        raise HTTPException(400, "Path escapes audit_responses/")

    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(500, f"Could not read file: {e}")

    return {"job_id": job_id, "name": safe_name, "content": content,
            "size_bytes": os.path.getsize(full)}


@router.get("/{job_id}/audit/summary")
async def get_audit_summary(
    job_id: str,
    owner: str = Depends(_owner_or_401),
):
    """Tổng hợp nhanh audit log để hiển thị overview.

    Trả về:
      - phase_durations: thời gian từng phase
      - event_counts_by_type: dict prefix → count
      - error_events: list các event error.* / *_failed
      - chunk_latencies: dict scope → list[float]
      - first_ts, last_ts, total_duration_seconds
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner, must_exist=False)
    audit_path = os.path.join(job_dir, "audit.jsonl")
    if not os.path.exists(audit_path):
        raise HTTPException(404, "Audit log not found")

    from datetime import datetime
    event_counts: dict[str, int] = {}
    error_events: list[dict] = []
    chunk_latencies: dict[str, list[float]] = {"body": [], "input_file": [], "pdf": []}
    phase_first_ts: dict[str, str] = {}
    phase_last_ts: dict[str, str] = {}
    first_ts = last_ts = None

    def _ts(ev: dict) -> str:
        return ev.get("ts", "")

    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                et = ev.get("event_type", "")
                # Group by prefix for top-level counts
                prefix = et.split(".", 1)[0] if "." in et else et
                event_counts[prefix] = event_counts.get(prefix, 0) + 1

                ts = _ts(ev)
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

                ph = ev.get("phase") or "unknown"
                if ph not in phase_first_ts or ts < phase_first_ts[ph]:
                    phase_first_ts[ph] = ts
                if ph not in phase_last_ts or ts > phase_last_ts[ph]:
                    phase_last_ts[ph] = ts

                if et == "chunk.translate_done":
                    data = ev.get("data", {})
                    scope = data.get("scope", "body")
                    lat = data.get("latency_seconds")
                    if isinstance(lat, (int, float)):
                        chunk_latencies.setdefault(scope, []).append(float(lat))

                if et.startswith("error.") or et.endswith("_failed"):
                    error_events.append({
                        "ts": ts, "seq": ev.get("seq"),
                        "phase": ph, "event_type": et,
                        "data": ev.get("data", {}),
                    })
    except Exception as e:
        raise HTTPException(500, f"Could not summarize audit: {e}")

    def _diff_seconds(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        try:
            da = datetime.fromisoformat(a.replace("Z", "+00:00"))
            db = datetime.fromisoformat(b.replace("Z", "+00:00"))
            return round((db - da).total_seconds(), 3)
        except Exception:
            return 0.0

    phase_durations = {
        ph: _diff_seconds(phase_first_ts[ph], phase_last_ts[ph])
        for ph in phase_first_ts
    }

    chunk_summary = {}
    for scope, latencies in chunk_latencies.items():
        if not latencies:
            continue
        chunk_summary[scope] = {
            "count": len(latencies),
            "mean_seconds": round(sum(latencies) / len(latencies), 3),
            "min_seconds": round(min(latencies), 3),
            "max_seconds": round(max(latencies), 3),
        }

    return {
        "job_id": job_id,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "total_duration_seconds": _diff_seconds(first_ts or "", last_ts or ""),
        "phase_durations": phase_durations,
        "event_counts_by_prefix": event_counts,
        "chunk_latency_summary": chunk_summary,
        "error_events": error_events[-50:],   # cap tail
        "error_count": len(error_events),
    }


# ── Global glossary endpoints ────────────────────────────────────

class GlobalTermUpdate(BaseModel):
    en_term: str
    vi_term: str
    field: str | None = None   # lĩnh vực (free text); None = không đổi, "" = xóa


class GlobalTermsBatch(BaseModel):
    # Accept either an {en: vi} mapping or a list of {en_term, vi_term, field} objects.
    terms: dict[str, str] | list[GlobalTermUpdate]
    # Optional en → lĩnh vực, used only with the dict form of `terms`.
    fields: dict[str, str] | None = None


@router.get("/global-glossary")
async def get_global_glossary_endpoint(
    min_confidence: float = 0.5,
    min_frequency: int = 1,
    limit: int = 500,
    field: str | None = None,
):
    """Get the cross-document global glossary.

    `terms` stays a flat {en: vi} map (back-compat for pre-seeding / badges).
    `details` adds per-term metadata incl. `field` (lĩnh vực); `fields` is the
    distinct lĩnh-vực list for autocomplete. Optional `field` filters `details`.
    """
    try:
        from app.database import (
            get_global_glossary, get_global_terms, get_global_terms_stats,
        )
        terms = get_global_glossary(
            min_confidence=min_confidence,
            min_frequency=min_frequency,
            limit=limit,
        )
        details = get_global_terms(
            min_confidence=min_confidence,
            min_frequency=min_frequency,
            limit=limit,
            field=field,
        )
        stats = get_global_terms_stats()
        return {
            "terms": terms,
            "details": details,
            "fields": stats.get("fields", []),
            "stats": stats,
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to load global glossary: {e}")


@router.post("/global-glossary")
async def upsert_global_term_endpoint(body: GlobalTermUpdate):
    """Manually add or update a term in the global glossary (incl. lĩnh vực)."""
    try:
        from app.database import upsert_global_term
        upsert_global_term(body.en_term, body.vi_term, field=body.field)
        return {
            "status": "ok",
            "en_term": body.en_term.lower().strip(),
            "vi_term": body.vi_term.strip(),
            "field": (body.field or "").strip()[:64] or None,
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to upsert term: {e}")


@router.post("/global-glossary/batch")
async def upsert_global_terms_batch_endpoint(body: GlobalTermsBatch):
    """Bulk upsert: lets the UI promote many (or all) terms to the kho in one call.

    Returns per-term outcome so the UI can refresh its "already in kho" badges
    without a second round-trip.
    """
    if isinstance(body.terms, dict):
        field_map = _normalize_glossary_fields(body.fields)
        pairs = [(en, vi, field_map.get((en or "").lower().strip()))
                 for en, vi in body.terms.items()]
    else:
        pairs = [(t.en_term, t.vi_term, t.field) for t in body.terms]
    try:
        from app.database import upsert_global_term
        added: list[str] = []
        failed: list[dict] = []
        for en, vi, fld in pairs:
            en_clean = (en or "").strip()
            vi_clean = (vi or "").strip()
            if not en_clean or not vi_clean:
                failed.append({"en_term": en, "reason": "empty"})
                continue
            try:
                upsert_global_term(en_clean, vi_clean, field=fld)
                added.append(en_clean.lower())
            except Exception as e:
                failed.append({"en_term": en_clean, "reason": str(e)[:200]})
        return {
            "status": "ok",
            "added_count": len(added),
            "failed_count": len(failed),
            "added": added,
            "failed": failed,
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to upsert batch: {e}")


@router.delete("/global-glossary/{en_term}")
async def delete_global_term_endpoint(en_term: str):
    """Delete a term from the global glossary."""
    try:
        from app.database import delete_global_term
        deleted = delete_global_term(en_term)
        if not deleted:
            raise HTTPException(404, f"Term '{en_term}' not found in global glossary")
        return {"status": "deleted", "en_term": en_term.lower()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to delete term: {e}")


# ── Glossary v2 (3-layer: seed + document + discovered) ──────────

class GlossaryV2Update(BaseModel):
    terms: dict[str, str] | None = None   # user-provided terms → document layer
    enabled: bool | None = None


@router.get("/glossary/seed")
async def get_seed_glossary():
    """Return the built-in seed glossary (Math/CS/AI, ~300 terms).

    These are always active and never need to be extracted — they're shipped
    with the system. Useful for inspection or export.
    """
    from app.pdf.glossaries.seed import SEED_GLOSSARY, DNT_SET
    return {
        "count": len(SEED_GLOSSARY),
        "dnt_count": len(DNT_SET),
        "terms": SEED_GLOSSARY,
        "dnt_set": sorted(DNT_SET),
    }


@router.get("/{job_id}/glossary-v2")
async def get_glossary_v2(job_id: str, owner: str = Depends(_owner_or_401)):
    """Get the full 3-layer glossary state for a job.

    Returns seed stats + document-specific + discovered terms separately.
    The seed is not returned in full (use /glossary/seed for that).
    """
    from app.pdf.glossaries import GlossaryPipeline
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    gp = GlossaryPipeline.from_progress(progress)
    return {"job_id": job_id, **gp.to_api_dict()}


@router.put("/{job_id}/glossary-v2")
async def update_glossary_v2(
    job_id: str,
    body: GlossaryV2Update,
    owner: str = Depends(_owner_or_401),
):
    """Add or override terms in the document layer (highest priority).

    These user-provided terms override both seed and auto-discovered terms.
    Useful for correcting a term Gemini translated inconsistently.
    """
    from app.pdf.glossaries import GlossaryPipeline
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    gp = GlossaryPipeline.from_progress(progress)

    if body.terms:
        gp.add_user_terms(body.terms)
    if body.enabled is not None:
        gp.enabled = body.enabled

    gp.save_to_progress(progress)

    atomic_write_json(progress_file, progress)

    return {"job_id": job_id, "status": "updated", **gp.to_api_dict()}


# ── Multi-agent disagreement analysis ────────────────────────────────

class MultiAgentRequest(BaseModel):
    model: str = "qwen2.5:7b"
    max_chunks: int = 20
    run_synthesis: bool = True


@router.post("/{job_id}/multi-agent")
async def run_multi_agent(
    job_id: str,
    req: MultiAgentRequest,
    owner: str = Depends(_owner_or_401),
):
    """Run multi-agent translation comparison (Gemini vs Ollama).

    For each chunk:
    1. Reads existing Gemini translation (primary)
    2. Re-translates the same EN source with Ollama (secondary)
    3. Computes ChrF++ agreement between the two VI outputs:
       - >= 65: consensus → use Gemini
       - 40-65: mild disagreement → Ollama picks better one
       - < 40: strong disagreement → Ollama synthesizes best of both
    4. Returns per-chunk agreement scores + synthesized translations

    This quantifies translation uncertainty:
    chunks where two independent models disagree strongly are likely
    ambiguous or contain translation errors.

    Requires: Ollama running + model pulled
    """
    _check_owner(job_id, owner)
    job_dir = _resolve_job_dir(job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")

    from app.pdf.multi_agent import run_multi_agent_evaluation, is_available

    if not is_available(req.model):
        raise HTTPException(503, detail=(
            f"Ollama không chạy hoặc model '{req.model}' chưa pull. "
            f"Chạy: ollama pull {req.model}"
        ))

    logger.info(
        f"[Routes] Multi-agent job={job_id} model={req.model} synthesis={req.run_synthesis}"
    )
    report = run_multi_agent_evaluation(
        job_dir,
        arbiter_model=req.model,
        max_chunks=min(req.max_chunks, 30),
        run_synthesis=req.run_synthesis,
    )

    result = {**report.to_dict(), "interpretation": report.interpretation()}

    # Persist to progress.json
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
        progress["multi_agent"] = result
        atomic_write_json(progress_file, progress)
    except Exception as e:
        logger.warning(f"Could not save multi_agent to progress.json: {e}")

    return result


@router.get("/{job_id}/multi-agent")
async def get_multi_agent_report(job_id: str, owner: str = Depends(_owner_or_401)):
    """Get cached multi-agent analysis report."""
    _check_owner(job_id, owner)
    progress_file = os.path.join(_resolve_job_dir(job_id, owner), "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    report = progress.get("multi_agent")
    if not report:
        raise HTTPException(404, "Multi-agent report not available. Run POST first.")
    return report


# ── Standalone app for isolated testing ──────────────────────────
app = FastAPI(title="PDF-Only Translation API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
