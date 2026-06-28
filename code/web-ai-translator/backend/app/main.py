import asyncio
import os
import json
import re
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Depends, WebSocket, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.services.latex_processor import (
    extract_source,
    extract_source_zip,
    save_single_tex,
    _find_main_tex,
)
from app.services.pipeline import TranslationPipeline
from app.text.converter import text_ext as _text_ext, convert_to_latex as _text_convert_to_latex
from app.text.html_converter import html_ext as _html_ext, convert_html_to_latex as _html_convert_to_latex
from app.config import SUPPORTED_AI_BACKENDS, SUPPORTED_TARGET_BROWSERS, settings
from app.pdf.routes import router as pdf_router
from app.office.routes import router as office_router
from app.api.history import router as history_router
from app.database import (
    init_db,
    migrate_existing_jobs,
    sync_job_to_db,
    get_jobs_for_user,
    get_job_owner,
    set_job_owner,
    upsert_job,
    purge_expired_sessions,
)
from app.auth import (
    auth_middleware,
    login as auth_login,
    logout as auth_logout,
    validate_token,
    current_username,
    ADMIN_USERNAME,
    IDLE_TIMEOUT,
    is_admin,
    _extract_token,
    register_user as auth_register,
    get_security_question,
    reset_password as auth_reset_password,
)
from app.user_paths import (
    safe_username,
    user_dir as _user_dir,
    user_job_dir,
    find_job_path,
    legacy_jobs_dir,
    ensure_user_dirs,
)
from app.utils.safe_io import (
    is_valid_job_id,
    atomic_write_json,
)
from app.utils.browser_guard import require_no_browser_running

# ── New architecture modules ────────────────────────────────────────────────
from app.logging_config import setup_logging, get_logger, bind_context
from app.metrics import setup_metrics, jobs_enqueued_total
from app.rate_limit import setup_rate_limit, translate_limit, auth_limit, upload_limit
from app.job_recovery import recover_interrupted_jobs
from app.ws import stream_job_events
from app.dispatcher import get_dispatcher
from app import cache as cache_mod

setup_logging()
log = get_logger("app.main")


def _ensure_job_id(job_id: str) -> str:
    """Reject job IDs that could escape the workspace via path traversal."""
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="job_id không hợp lệ")
    return job_id


app = FastAPI(
    title="Web AI Translator",
    description="AI Agent tự động truy xuất và dịch thuật tài liệu học thuật",
)

# CORS: never combine `*` with credentials — browsers reject it and Starlette
# would echo the request origin instead, which lets ANY site make credentialed
# calls. Read explicit origins from settings.CORS_ORIGINS (comma-separated).
_cors_origins = [
    o.strip()
    for o in settings.CORS_ORIGINS.split(",")
    if o.strip() and o.strip() != "*"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus + slowapi must be installed BEFORE the auth middleware so the
# /metrics endpoint and rate-limit responses are reachable without auth, and
# so that 429s aren't masked by the auth layer.
setup_metrics(app)
setup_rate_limit(app)

app.middleware("http")(auth_middleware)

# Mount routes
app.include_router(pdf_router)
app.include_router(office_router)
app.include_router(history_router)


# ── WebSocket: live job progress ────────────────────────────────────────────

@app.websocket("/ws/jobs/{job_id}")
async def ws_job_progress(websocket: WebSocket, job_id: str):
    """Stream progress events for a single job.

    Workers publish to ``job:{job_id}`` on Redis; this endpoint fans them out
    to whichever browser tab has the job open. Falls back to a polling
    in-memory bus if Redis is unreachable (see app.ws).
    """
    if not is_valid_job_id(job_id):
        await websocket.close(code=1008)
        return
    await stream_job_events(websocket, job_id)


@app.on_event("startup")
async def startup():
    """Init DB, drop expired sessions, migrate existing jobs, recover stale runs."""
    workspace = os.path.abspath(settings.WORKSPACE_DIR)
    init_db()
    # Anything older than (now - idle_timeout) is unreachable anyway; clearing
    # it here keeps the sessions table from growing forever.
    try:
        removed = purge_expired_sessions(time.time() - IDLE_TIMEOUT)
        if removed:
            log.info("session_purge_startup", removed=removed)
    except Exception as e:
        log.warning("session_purge_failed", error=str(e))
    migrate_existing_jobs(workspace, admin_username=ADMIN_USERNAME)

    # Mark jobs that were mid-flight when the previous backend crashed.
    # The frontend uses status="interrupted" to offer a Resume button.
    try:
        report = recover_interrupted_jobs(workspace)
        if report.get("interrupted"):
            log.info(
                "job_recovery_done",
                scanned=report["scanned"],
                active=report["active"],
                interrupted=report["interrupted"],
            )
    except Exception as e:
        log.warning("job_recovery_failed", error=str(e))

    # Initialize the unified dispatcher with both managers as subprocess
    # fallbacks. In production (DISPATCH_MODE=celery) the broker is contacted
    # and the fallbacks are ignored; in dev they handle everything.
    try:
        from app.pdf.routes import _manager as _pdf_manager
        get_dispatcher(latex_fallback=pipeline_manager, pdf_fallback=_pdf_manager)
    except Exception as e:
        log.warning("dispatcher_init_failed", error=str(e))

WORKSPACE = settings.WORKSPACE_DIR
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Ownership helpers ─────────────────────────────────────────────────────────

def get_owner(request: Request) -> str:
    """FastAPI dependency: resolve current user, raise 401 if missing.

    The auth_middleware already rejects unauthenticated requests for /api/*,
    so this is mostly a convenience. We still re-check to guard against
    middleware bypass and to fetch the actual username (not just validity).
    """
    user = current_username(request)
    if not user:
        raise HTTPException(status_code=401, detail="Chưa đăng nhập")
    return user


# `is_admin` is imported from app.auth — env-var admin OR DB-flagged admin.
# Re-exported here so existing call sites that read `app.main.is_admin` keep
# working. The local def used to duplicate the env-var check; the auth-module
# version also covers the DB-flagged "first registered user" admin.


def resolve_owned_job_dir(workspace: str, job_id: str, owner: str) -> str:
    """Locate a job dir for the current user.

    Raises 400 if job_id format is invalid (path traversal guard).
    Raises 404 if job folder doesn't exist anywhere we're allowed to read.
    Raises 403 if the job exists but belongs to someone else (non-admin).
    """
    _ensure_job_id(job_id)
    db_owner = get_job_owner(job_id)
    if db_owner and db_owner != owner and not is_admin(owner):
        raise HTTPException(status_code=403, detail="Không có quyền truy cập job này")
    path = find_job_path(workspace, job_id, owner, allow_legacy=is_admin(owner))
    if not path:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return path


# ── Auth endpoints ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    security_question: str
    security_answer: str


class ForgotPasswordRequest(BaseModel):
    username: str
    security_answer: str
    new_password: str


@app.post("/api/auth/login")
@auth_limit
async def api_login(request: Request, req: LoginRequest):
    token = auth_login(req.username, req.password)
    return {"token": token}


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    token = _extract_token(request)
    if token:
        auth_logout(token)
    return {"status": "logged_out"}


@app.post("/api/auth/register")
@auth_limit
async def api_register(request: Request, req: RegisterRequest):
    """Register a new user account."""
    auth_register(req.username, req.password, req.security_question, req.security_answer)
    return {"status": "registered", "username": req.username.strip()}


@app.get("/api/auth/security-question")
async def api_security_question(username: str):
    """Return the security question for a username (used by forgot-password flow)."""
    question = get_security_question(username)
    return {"security_question": question}


@app.post("/api/auth/forgot-password")
@auth_limit
async def api_forgot_password(request: Request, req: ForgotPasswordRequest):
    """Reset password using the security answer."""
    auth_reset_password(req.username, req.security_answer, req.new_password)
    return {"status": "password_reset"}


@app.get("/api/auth/me")
async def api_me(request: Request):
    """Return current user info if session is valid."""
    from app.auth import IDLE_TIMEOUT
    user = current_username(request)
    if not user:
        raise HTTPException(status_code=401, detail="Chưa đăng nhập")
    return {
        "username": user,
        "is_admin": is_admin(user),
        "idle_timeout": IDLE_TIMEOUT,
    }


# ── /health (public) ──────────────────────────────────────────────────────────

# ============================================================
# Pipeline Manager: nhieu job chay song song, khong kill job cu
# ============================================================
class PipelineManager:
    """Quan ly pipeline dich LaTeX: nhieu job co the chay dong thoi.

    Each job is associated with a `work_dir` — the *parent* of `jobs/`, i.e. the
    per-user workspace `{WORKSPACE}/users/{safe_username}`. The pipeline
    subprocess builds its own paths as `{work_dir}/jobs/{job_id}/...`, so per-user
    isolation comes for free as long as we pass the right work_dir.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # job_id -> {"proc": Popen, "work_dir": str}
        self._jobs: dict[str, dict] = {}

    def _job_dir(self, work_dir: str, job_id: str) -> str:
        return os.path.join(work_dir, "jobs", job_id)

    def stop_job(self, job_id: str):
        """Dung 1 job cu the. Danh dau progress la 'cancelled'."""
        with self._lock:
            entry = self._jobs.pop(job_id, None)

        if not entry:
            return
        proc = entry.get("proc")
        work_dir = entry.get("work_dir")

        if proc and proc.poll() is None:
            print(f"[PipelineManager] Stopping job: {job_id}")
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if work_dir:
            self._mark_cancelled(work_dir, job_id)

    def _mark_cancelled(self, work_dir: str, job_id: str):
        """Danh dau progress.json cua job la 'cancelled'."""
        try:
            progress_file = os.path.join(self._job_dir(work_dir, job_id), "progress.json")
            if os.path.exists(progress_file):
                with open(progress_file, "r", encoding="utf-8") as f:
                    progress = json.load(f)
                status = progress.get("status", "")
                if (not status.startswith("done")
                        and not status.startswith("error")
                        and status != "starting"):
                    progress["status"] = "cancelled"
                    atomic_write_json(progress_file, progress)
                    print(f"[PipelineManager] Marked job {job_id} as cancelled")
        except Exception as e:
            print(f"[PipelineManager] Warning: could not mark {job_id} as cancelled: {e}")

    def start(self, job_id: str, tex_path: str, source_dir: str, work_dir: str):
        """Bat dau job moi. Neu cung job_id dang chay thi dung truoc."""
        # Stop same job if re-translating
        with self._lock:
            existing = self._jobs.get(job_id)
        if existing and existing.get("proc") and existing["proc"].poll() is None:
            self.stop_job(job_id)

        thread = threading.Thread(
            target=self._run,
            args=(job_id, tex_path, source_dir, work_dir),
            daemon=True,
        )
        thread.start()

    def _run(self, job_id: str, tex_path: str, source_dir: str, work_dir: str):
        """Chay pipeline trong subprocess."""
        abs_workspace = os.path.abspath(work_dir).replace("\\", "/")
        abs_tex = os.path.abspath(tex_path).replace("\\", "/")
        abs_source = os.path.abspath(source_dir).replace("\\", "/")
        backend = BACKEND_DIR.replace("\\", "/")

        job_dir = self._job_dir(work_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        script_path = os.path.join(job_dir, "run_pipeline.py")
        script_content = f'''
import asyncio, sys, os, json

sys.path.insert(0, r"{backend}")
os.chdir(r"{backend}")
from app.services.pipeline import TranslationPipeline
from app.utils.safe_io import atomic_write_json

async def main():
    pipeline = TranslationPipeline(work_dir=r"{abs_workspace}")
    try:
        await pipeline.run(
            tex_path=r"{abs_tex}",
            job_id="{job_id}",
            source_dir=r"{abs_source}",
        )
    except Exception as e:
        job_dir = os.path.join(r"{abs_workspace}", "jobs", "{job_id}")
        pf = os.path.join(job_dir, "progress.json")
        progress = {{}}
        if os.path.exists(pf):
            with open(pf, "r", encoding="utf-8") as f:
                progress = json.load(f)
        progress["status"] = f"error: {{e}}"
        atomic_write_json(pf, progress)
        raise

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
try:
    loop.run_until_complete(main())
except KeyboardInterrupt:
    pass  # Graceful shutdown — don't treat as error
finally:
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending and not loop.is_closed():
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    except Exception:
        pass
    if not loop.is_closed():
        loop.close()
'''
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_content)

        log_path = os.path.join(job_dir, "pipeline.log")
        print(f"[PipelineManager] Starting job {job_id}, log: {log_path}")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        with open(log_path, "w", encoding="utf-8") as log_file:
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

        if proc.returncode != 0:
            print(f"[PipelineManager] Job {job_id} failed (exit code {proc.returncode})")
            progress_file = os.path.join(job_dir, "progress.json")
            progress = {}
            if os.path.exists(progress_file):
                with open(progress_file, "r", encoding="utf-8") as f:
                    progress = json.load(f)
            current_status = progress.get("status", "")
            if (not current_status.startswith("error")
                    and current_status != "starting"
                    and current_status != "cancelled"):
                try:
                    with open(log_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                        error_msg = lines[-1].strip() if lines else "Unknown error"
                except Exception:
                    error_msg = f"Exit code {proc.returncode}"
                progress["status"] = f"error: {error_msg}"
                atomic_write_json(progress_file, progress)
        else:
            print(f"[PipelineManager] Job {job_id} completed!")
            try:
                progress_file = os.path.join(job_dir, "progress.json")
                if os.path.exists(progress_file):
                    with open(progress_file, "r", encoding="utf-8") as f:
                        _prog = json.load(f)
                    sync_job_to_db(job_id, _prog, os.path.abspath(WORKSPACE))
            except Exception as _e:
                print(f"[PipelineManager] DB sync failed: {_e}")

    @property
    def running_jobs(self) -> list[str]:
        with self._lock:
            return [jid for jid, e in self._jobs.items()
                    if e.get("proc") and e["proc"].poll() is None]

    def is_job_running(self, job_id: str) -> bool:
        with self._lock:
            entry = self._jobs.get(job_id)
            if not entry:
                return False
            proc = entry.get("proc")
            return proc is not None and proc.poll() is None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return any(e.get("proc") and e["proc"].poll() is None
                       for e in self._jobs.values())


pipeline_manager = PipelineManager()


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/api/health/setup")
async def setup_status():
    """Public — frontend hits this on first load to pick login vs first-time setup.

    needs_setup is True when there are no registered users AND no env-var admin
    is configured: in that state the launcher should send the user to
    /api/auth/register before anything else. Once the first user exists, the
    DB-flagged admin promotion (see register_user) takes over.
    """
    from app.database import count_users
    try:
        users = count_users()
    except Exception:
        # DB not initialized yet — pretend no users so the UI can still render
        users = 0
    has_builtin_admin = bool(ADMIN_USERNAME)
    return {
        "needs_setup": users == 0 and not has_builtin_admin,
        "has_builtin_admin": has_builtin_admin,
        "user_count": users,
    }


# ── Translator mode settings ──────────────────────────────────────────────────

@app.get("/api/settings/translator-mode")
async def get_translator_mode():
    """Tra ve che do hien tai cua translator."""
    return {
        "mode": settings.TRANSLATOR_MODE,
        "cdp_url": settings.CDP_URL,
        "ai_backend": settings.AI_BACKEND,
        "target_browser": settings.TARGET_BROWSER,
        "supported_target_browsers": list(SUPPORTED_TARGET_BROWSERS),
    }


@app.post("/api/settings/translator-mode")
async def set_translator_mode(body: dict):
    """Doi che do translator (new_browser | cdp). Luu vao file, giu sau khi restart."""
    mode = body.get("mode", "")
    if mode not in ("new_browser", "cdp"):
        raise HTTPException(400, "mode phai la 'new_browser' hoac 'cdp'")
    settings.set_translator_mode(mode)
    return {"mode": settings.TRANSLATOR_MODE}


@app.post("/api/settings/ai-backend")
async def set_ai_backend(body: dict):
    """Doi AI backend. Luu vao file, giu sau khi restart."""
    backend = body.get("backend", "")
    if backend not in SUPPORTED_AI_BACKENDS:
        raise HTTPException(
            400,
            "backend phai la mot trong: " + ", ".join(SUPPORTED_AI_BACKENDS),
        )
    settings.set_ai_backend(backend)
    return {"ai_backend": settings.AI_BACKEND}


@app.post("/api/settings/target-browser")
async def set_target_browser(body: dict):
    """Đổi trình duyệt dùng khi mở browser mới. Lưu vào file, giữ sau restart."""
    browser = (body.get("browser") or "").lower()
    if browser not in SUPPORTED_TARGET_BROWSERS:
        raise HTTPException(
            400,
            "browser phải là một trong: " + ", ".join(SUPPORTED_TARGET_BROWSERS),
        )
    settings.set_target_browser(browser)
    return {"target_browser": settings.TARGET_BROWSER}


@app.get("/api/settings/cdp-status")
async def get_cdp_status():
    """Kiem tra Chrome co dang chay voi remote debugging port khong."""
    import socket
    from urllib.parse import urlparse
    parsed = urlparse(settings.CDP_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port or 9222
    available = False
    try:
        with socket.create_connection((host, port), timeout=2):
            available = True
    except Exception:
        pass
    return {
        "available": available,
        "url": settings.CDP_URL,
        "current_mode": settings.TRANSLATOR_MODE,
    }


@app.get("/api/settings/vlm-status")
async def get_vlm_status():
    """Kiểm tra VLM navigation (Ollama + VLM model) có sẵn sàng không."""
    from app.services.vision_nav import (
        _check_ollama_available,
        _check_model_available,
        OLLAMA_URL as vlm_url,
        DEFAULT_MODEL as vlm_model,
    )
    ollama_ok = await _check_ollama_available()
    model_ok = await _check_model_available(vlm_model) if ollama_ok else False
    return {
        "available": ollama_ok and model_ok,
        "ollama_running": ollama_ok,
        "model_installed": model_ok,
        "model": vlm_model,
        "ollama_url": vlm_url,
        "description": (
            "Agentic Web Navigation: VLM chụp screenshot → tự tìm UI elements "
            "khi CSS selectors thất bại. Cần: Ollama + VLM model (vd: llava:7b)."
        ),
        "setup_hint": (
            f"ollama pull {vlm_model}" if ollama_ok and not model_ok
            else ("Cài Ollama tại https://ollama.com" if not ollama_ok else "")
        ),
    }


@app.get("/api/settings/scheduler")
async def get_scheduler_status():
    """Trả về scheduling strategy hiện tại + danh sách strategies + per-account history."""
    from app.pools import get_account_pool
    from app.pools.account_history import get_account_history
    from app.pools.schedulers import list_strategies

    pool = get_account_pool()
    history = get_account_history()

    accounts_info = []
    for email in pool.accounts:
        st = history.get(email)
        state = pool._state(email)
        accounts_info.append({
            "email": email,
            "state": state,
            "success": st.success,
            "fail": st.fail,
            "cooldowns": st.cooldowns,
            "avg_latency": round(st.avg_latency, 2),
            "recent_success_rate": round(st.recent_success_rate(), 3),
            "last_used_ts": st.last_used_ts,
        })

    return {
        "current": pool.scheduler_name(),
        "strategies": list_strategies(),
        "accounts": accounts_info,
    }


class _SchedulerSetReq(BaseModel):
    strategy: str


@app.put("/api/settings/scheduler")
async def set_scheduler(req: _SchedulerSetReq, request: Request):
    """Đổi scheduling strategy runtime. Yêu cầu quyền admin."""
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Cần quyền admin")
    from app.pools import get_account_pool
    from app.pools.schedulers import list_strategies

    if req.strategy not in list_strategies():
        raise HTTPException(status_code=400, detail=f"Không nhận diện strategy: {req.strategy}")

    pool = get_account_pool()
    resolved = pool.set_scheduler(req.strategy)
    return {"current": resolved}


@app.post("/api/restart")
async def api_restart(owner: str = Depends(get_owner)):
    """Trigger uvicorn --reload by touching main.py (requires --reload flag).

    Admin-only — restarting the backend kills every in-flight translation
    pipeline subprocess for every user, so this is effectively a DoS vector
    if any logged-in user can call it.
    """
    if not is_admin(owner):
        raise HTTPException(status_code=403, detail="Chỉ admin mới được restart server")
    import pathlib
    pathlib.Path(__file__).touch()
    return {"status": "reloading"}


@app.post("/api/cancel")
async def cancel_current_job(
    job_id: str | None = None,
    owner: str = Depends(get_owner),
):
    """Cancel a running translation job. Pass job_id to cancel a specific job."""
    if job_id:
        _ensure_job_id(job_id)
        # Ownership guard — admin can cancel anyone's
        db_owner = get_job_owner(job_id)
        if db_owner and db_owner != owner and not is_admin(owner):
            raise HTTPException(status_code=403, detail="Không có quyền hủy job này")
        if pipeline_manager.is_job_running(job_id):
            pipeline_manager.stop_job(job_id)
        return {"status": "cancelled", "job_id": job_id}
    # No job_id: cancel all running LaTeX jobs the caller owns (admin: all)
    running = pipeline_manager.running_jobs
    stopped = []
    for jid in running:
        db_owner = get_job_owner(jid)
        if db_owner == owner or is_admin(owner) or db_owner is None and is_admin(owner):
            pipeline_manager.stop_job(jid)
            stopped.append(jid)
    return {"status": "cancelled", "stopped": stopped}


@app.get("/api/pipeline/status")
async def api_pipeline_status(owner: str = Depends(get_owner)):
    """Tra ve trang thai tat ca pipeline dang chay (filter theo owner)."""
    running = pipeline_manager.running_jobs
    # Filter to caller's jobs (admin: all)
    if not is_admin(owner):
        running = [jid for jid in running if get_job_owner(jid) == owner]
    if not running:
        return {"running": False, "jobs": []}

    jobs = []
    for job_id in running:
        job_info: dict = {"job_id": job_id, "arxiv_id": job_id.replace("_", "/")}
        path = find_job_path(WORKSPACE, job_id, owner, allow_legacy=is_admin(owner))
        if not path:
            continue
        progress_file = os.path.join(path, "progress.json")
        if os.path.exists(progress_file):
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
            job_info["status"] = progress.get("status", "")
            job_info["translated_chunks"] = len(progress.get("translated_chunks", {}))
        jobs.append(job_info)

    if not jobs:
        return {"running": False, "jobs": []}
    # Backward-compat: expose first running job at top level
    return {"running": True, "jobs": jobs, "job_id": jobs[0]["job_id"]}


# --- Danh sach tat ca jobs ---
@app.get("/api/jobs")
async def api_list_jobs(owner: str = Depends(get_owner)):
    """Tra ve danh sach cac job thuoc nguoi dung hien tai.

    Admin sees jobs whose owner matches AND legacy unowned jobs.
    Regular users only see jobs they own.
    """
    # Build list of (job_id, job_dir) tuples for the caller
    candidates: list[tuple[str, str]] = []
    safe = safe_username(owner)
    user_jobs_root = os.path.join(WORKSPACE, "users", safe, "jobs")
    if os.path.isdir(user_jobs_root):
        for jid in os.listdir(user_jobs_root):
            jp = os.path.join(user_jobs_root, jid)
            if os.path.isdir(jp):
                candidates.append((jid, jp))

    # Admin also sees legacy jobs (workspace/jobs/...)
    if is_admin(owner):
        legacy_root = legacy_jobs_dir(WORKSPACE)
        if os.path.isdir(legacy_root):
            already = {jid for jid, _ in candidates}
            for jid in os.listdir(legacy_root):
                if jid in already:
                    continue
                jp = os.path.join(legacy_root, jid)
                if os.path.isdir(jp):
                    candidates.append((jid, jp))

    if not candidates:
        return {"jobs": []}

    jobs = []
    for job_id, job_dir in candidates:

        # Determine source type from progress.json or job_id prefix
        source_type = "latex"
        progress_file = os.path.join(job_dir, "progress.json")
        if os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8") as f:
                    _p = json.load(f)
                st = _p.get("source_type", "")
                if st in ("pdf_only", "pdf"):
                    source_type = "pdf"
            except Exception:
                pass
        if job_id.startswith("pdf_"):
            source_type = "pdf"

        job_info = {
            "job_id": job_id,
            "arxiv_id": job_id.replace("_", "/") if source_type == "latex" else job_id,
            "source_type": source_type,
            "status": "unknown",
            "progress_percent": 0,
            "has_original_pdf": os.path.exists(os.path.join(job_dir, "original.pdf")),
            "has_translated_pdf": os.path.exists(os.path.join(job_dir, "output", "translated.pdf")),
            "created_at": os.path.getctime(job_dir),
            "updated_at": os.path.getmtime(job_dir),
        }

        # Doc progress.json
        progress_file = os.path.join(job_dir, "progress.json")
        if os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8") as f:
                    progress = json.load(f)
                status = progress.get("status", "unknown")
                job_info["status"] = status
                for key in (
                    "title", "original_filename", "page_count", "total_chunks",
                    "current_chunk", "duration_seconds", "num_tabs",
                    "model_preference", "models", "judge_backend",
                    "phase_timeline",
                ):
                    if progress.get(key) is not None:
                        job_info[key] = progress.get(key)
                glossary_terms = (progress.get("glossary") or {}).get("terms")
                if isinstance(glossary_terms, list):
                    job_info["glossary_count"] = len(glossary_terms)
                eval_loop = progress.get("eval_loop") or {}
                if eval_loop:
                    job_info["eval_loop"] = {
                        "passed_chunks": len(eval_loop.get("passed") or []),
                        "flagged_chunks": len(eval_loop.get("flagged") or []),
                        "total_translations": eval_loop.get("total_translations"),
                        "total_judge_calls": eval_loop.get("total_judge_calls"),
                        "duration_seconds": eval_loop.get("duration_seconds"),
                    }
                if progress.get("started_at"):
                    job_info["started_at"] = progress.get("started_at")

                # Tinh progress %
                translated = progress.get("translated_chunks", {})
                num_translated = len(translated)

                # Tim total chunks tu status "translating X/Y"
                m = re.match(r"translating (\d+)/(\d+)", status)
                if m:
                    total = int(m.group(2))
                    current = int(m.group(1))
                    job_info["total_chunks"] = total
                    job_info["current_chunk"] = current
                    job_info["progress_percent"] = round(current / total * 100) if total > 0 else 0
                elif status in ("done", "done_with_warnings") or job_info["has_translated_pdf"]:
                    job_info["progress_percent"] = 100
                    job_info["status"] = status if status in ("done", "done_with_warnings") else "done"
                    # Them validation info neu co
                    validation = progress.get("validation")
                    if not validation and job_info["has_translated_pdf"]:
                        # Auto-validate jobs chua co validation data
                        from app.services.pipeline import TranslationPipeline
                        translated_pdf = os.path.join(job_dir, "output", "translated.pdf")
                        original_pdf = os.path.join(job_dir, "original.pdf")
                        validation = TranslationPipeline._validate_pdf(translated_pdf, original_pdf)
                        # Luu validation vao progress.json
                        progress["validation"] = validation
                        if validation["status"] == "warning":
                            progress["status"] = "done_with_warnings"
                            job_info["status"] = "done_with_warnings"
                        atomic_write_json(progress_file, progress)
                    if validation:
                        job_info["validation"] = validation
                    quality = progress.get("quality")
                    if quality:
                        job_info["quality_score"] = quality.get("score", 0)
                        job_info["quality_issues"] = quality.get("issue_count", 0)
                elif status == "cancelled":
                    # Uoc luong progress tu so chunk da dich
                    # Can doc file .tex goc de biet total, nhung co the lay tu translated_chunks
                    if num_translated > 0:
                        # Uoc tinh: dung so chunk da dich / tong (khong biet tong chinh xac)
                        # Tim total tu cac input_chunks keys
                        total_est = num_translated
                        for key in progress:
                            if key.startswith("input_chunks:"):
                                total_est += len(progress[key])
                        job_info["progress_percent"] = min(95, round(num_translated / max(total_est, 1) * 100))
                        job_info["translated_chunks_count"] = num_translated
                elif status.startswith("error") or status.startswith("compile_error"):
                    job_info["progress_percent"] = round(num_translated / max(num_translated + 1, 1) * 100)
            except Exception:
                pass

        # Build PDF URLs for pdf jobs
        if source_type == "pdf":
            if job_info["has_original_pdf"]:
                job_info["original_pdf_url"] = f"/api/pdf-translate/{job_id}/original"
            if job_info["has_translated_pdf"]:
                job_info["translated_pdf_url"] = f"/api/pdf-translate/{job_id}/translated"

        # Include title for PDF jobs (stored in progress.json)
        if source_type == "pdf" and os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8") as f:
                    _pdata = json.load(f)
                saved_title = _pdata.get("title", "").strip()
                if saved_title:
                    job_info["title"] = saved_title
                else:
                    # Backfill: extract title from original PDF if not saved
                    orig_pdf = os.path.join(job_dir, "original.pdf")
                    if os.path.exists(orig_pdf):
                        try:
                            from app.pdf.processor import get_pdf_info
                            pdf_info = get_pdf_info(orig_pdf)
                            extracted_title = pdf_info.get("title", "").strip()
                            if extracted_title:
                                job_info["title"] = extracted_title
                                # Persist so we don't re-extract next time
                                _pdata["title"] = extracted_title
                                with open(progress_file, "w", encoding="utf-8") as f:
                                    json.dump(_pdata, f, ensure_ascii=False, indent=2)
                        except Exception:
                            pass
            except Exception:
                pass

        jobs.append(job_info)

    # Sap xep: dang chay truoc, roi done, roi cancelled, roi error
    status_order = {"starting": 0, "translating": 1, "done_with_warnings": 2, "done": 3, "cancelled": 4, "error": 5}
    def sort_key(j):
        s = j["status"]
        for prefix, order in status_order.items():
            if s.startswith(prefix):
                return order
        return 5
    jobs.sort(key=sort_key)

    return {"jobs": jobs}


# --- Upload LaTeX source thủ công (.tex / .tar.gz / .zip) ---
_LATEX_EXTS = (".tex", ".tar.gz", ".tgz", ".zip")


def _latex_ext(filename: str) -> str | None:
    lower = filename.lower()
    if lower.endswith(".tar.gz"):
        return "tar.gz"
    if lower.endswith(".tgz"):
        return "tar.gz"
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(".tex"):
        return "tex"
    return None


def _latex_job_id_from_filename(filename: str) -> str:
    base = filename
    for suf in (".tar.gz", ".tgz", ".zip", ".tex"):
        if base.lower().endswith(suf):
            base = base[: -len(suf)]
            break
    slug = re.sub(r"[^\w\-.]", "_", base)[:50] or "upload"
    return f"tex_{slug}"


@app.post("/api/translate/upload")
@upload_limit
async def api_translate_upload(
    request: Request,
    file: UploadFile = File(...),
    force: bool = Form(False),
    owner: str = Depends(get_owner),
):
    """Upload .tex / .tar.gz / .tgz / .zip và bắt đầu dịch LaTeX.

    Tái sử dụng pipeline LaTeX hiện có (cùng dispatcher / pipeline_manager).
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")

    ext = _latex_ext(file.filename)
    if ext is None:
        raise HTTPException(
            status_code=400,
            detail="Chỉ chấp nhận .tex, .tar.gz, .tgz, .zip",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File rỗng")

    require_no_browser_running()

    job_id = _latex_job_id_from_filename(file.filename)

    db_owner = get_job_owner(job_id)
    if db_owner and db_owner != owner and not is_admin(owner):
        raise HTTPException(status_code=403, detail="Job ID này thuộc người dùng khác")

    ensure_user_dirs(WORKSPACE, owner)
    work_dir = _user_dir(WORKSPACE, owner)
    job_dir = user_job_dir(WORKSPACE, owner, job_id)
    os.makedirs(job_dir, exist_ok=True)

    translated_pdf = os.path.join(job_dir, "output", "translated.pdf")
    if os.path.exists(translated_pdf) and not force:
        return {
            "job_id": job_id,
            "status": "already_done",
            "original_pdf_url": f"/api/pdf/{job_id}/original",
            "translated_pdf_url": f"/api/pdf/{job_id}/translated",
        }

    if force:
        import shutil as _shutil, time as _time
        _ts = int(_time.time())
        output_dir = os.path.join(job_dir, "output")
        if os.path.exists(output_dir):
            os.rename(output_dir, os.path.join(job_dir, f"output_v{_ts}"))
        progress_file_old = os.path.join(job_dir, "progress.json")
        if os.path.exists(progress_file_old):
            os.rename(progress_file_old, os.path.join(job_dir, f"progress_v{_ts}.json"))
        extract_dir_old = os.path.join(job_dir, "source_extracted")
        if os.path.exists(extract_dir_old):
            _shutil.rmtree(extract_dir_old)

    extract_root = os.path.join(job_dir, "source_extracted")
    try:
        if ext == "tar.gz":
            archive_path = os.path.join(job_dir, "uploaded.tar.gz")
            with open(archive_path, "wb") as f:
                f.write(content)
            tex_path = extract_source(archive_path, extract_root)
        elif ext == "zip":
            archive_path = os.path.join(job_dir, "uploaded.zip")
            with open(archive_path, "wb") as f:
                f.write(content)
            tex_path = extract_source_zip(archive_path, extract_root)
        else:  # tex
            tex_path = save_single_tex(content, extract_root, filename="main.tex")
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=f"Archive không an toàn: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không giải nén được: {e}")

    source_dir = os.path.dirname(tex_path)

    progress_file = os.path.join(job_dir, "progress.json")
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump({
            "status": "starting",
            "translated_chunks": {},
            "source_type": "latex",
            "uploaded_filename": file.filename,
        }, f)

    upsert_job(
        job_id,
        source_type="latex",
        arxiv_id=None,
        status="starting",
        username=owner,
    )

    try:
        dispatcher = get_dispatcher(latex_fallback=pipeline_manager)
        dispatcher.start_latex(job_id, tex_path, source_dir, work_dir)
    except Exception as e:
        log.warning("dispatcher_start_failed_falling_back", job_id=job_id, error=str(e))
        pipeline_manager.start(job_id, tex_path, source_dir, work_dir)

    jobs_enqueued_total.labels(source_type="latex").inc()

    return {
        "job_id": job_id,
        "status": "translating",
        "uploaded_filename": file.filename,
    }


# --- Resume / re-translate LaTeX job (job_id-based) ---
class LatexStartRequest(BaseModel):
    job_id: str
    force: bool = False  # True = dịch lại từ đầu (giữ tối đa 2 version cũ)
    resume: bool = False  # True = tiếp tục từ progress.json


@app.post("/api/translate/start")
@translate_limit
async def api_translate_start(
    request: Request,
    req: LatexStartRequest,
    owner: str = Depends(get_owner),
):
    """Bắt đầu/dịch lại/tiếp tục dịch LaTeX job đã có (đã upload trước đó)."""
    _ensure_job_id(req.job_id)
    job_id = req.job_id

    require_no_browser_running()

    db_owner = get_job_owner(job_id)
    if db_owner and db_owner != owner and not is_admin(owner):
        raise HTTPException(status_code=403, detail="Job này thuộc người dùng khác")

    work_dir = _user_dir(WORKSPACE, owner)
    job_dir = find_job_path(WORKSPACE, job_id, owner, allow_legacy=is_admin(owner))
    if not job_dir or not os.path.isdir(job_dir):
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    if is_admin(owner) and job_dir.startswith(legacy_jobs_dir(WORKSPACE)):
        work_dir = WORKSPACE

    source_root = os.path.join(job_dir, "source_extracted")
    if not os.path.isdir(source_root):
        raise HTTPException(
            status_code=400,
            detail="Source LaTeX không còn — cần upload lại file gốc",
        )
    try:
        tex_path = _find_main_tex(source_root)
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail="Không tìm thấy file .tex trong source_extracted")
    source_dir = os.path.dirname(tex_path)

    if req.force:
        import shutil as _shutil, time as _time
        _ts = int(_time.time())
        output_dir = os.path.join(job_dir, "output")
        if os.path.exists(output_dir):
            os.rename(output_dir, os.path.join(job_dir, f"output_v{_ts}"))
        progress_file_old = os.path.join(job_dir, "progress.json")
        if os.path.exists(progress_file_old):
            os.rename(progress_file_old, os.path.join(job_dir, f"progress_v{_ts}.json"))
        _versions = sorted([
            d for d in os.listdir(job_dir)
            if d.startswith("output_v") and os.path.isdir(os.path.join(job_dir, d))
        ])
        for _old in _versions[:-2]:
            _shutil.rmtree(os.path.join(job_dir, _old), ignore_errors=True)

    if req.resume:
        progress_file = os.path.join(job_dir, "progress.json")
        if os.path.exists(progress_file):
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
            progress["status"] = "resuming"
            atomic_write_json(progress_file, progress)

    if not req.resume:
        progress_file = os.path.join(job_dir, "progress.json")
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump({"status": "starting", "translated_chunks": {}, "source_type": "latex"}, f)

    upsert_job(job_id, source_type="latex", arxiv_id=None, status="starting", username=owner)

    try:
        dispatcher = get_dispatcher(latex_fallback=pipeline_manager)
        dispatcher.start_latex(job_id, tex_path, source_dir, work_dir)
    except Exception as e:
        log.warning("dispatcher_start_failed_falling_back", job_id=job_id, error=str(e))
        pipeline_manager.start(job_id, tex_path, source_dir, work_dir)
    jobs_enqueued_total.labels(source_type="latex").inc()

    return {
        "job_id": job_id,
        "status": "translating",
        "original_pdf_url": f"/api/pdf/{job_id}/original",
    }


# --- Upload .txt / .md ---
def _text_job_id_from_filename(filename: str) -> str:
    base = filename
    for suf in (".markdown", ".md", ".txt"):
        if base.lower().endswith(suf):
            base = base[: -len(suf)]
            break
    slug = re.sub(r"[^\w\-.]", "_", base)[:50] or "upload"
    return f"text_{slug}"


@app.post("/api/translate/upload-text")
@upload_limit
async def api_translate_upload_text(
    request: Request,
    file: UploadFile = File(...),
    force: bool = Form(False),
    title: str = Form(""),
    owner: str = Depends(get_owner),
):
    """Upload .txt / .md / .markdown — convert sang LaTeX rồi dùng pipeline LaTeX.

    Output là PDF đã dịch (giống luồng .tex upload).
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")

    ext = _text_ext(file.filename)
    if ext is None:
        raise HTTPException(
            status_code=400,
            detail="Chỉ chấp nhận .txt, .md, .markdown",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File rỗng")

    require_no_browser_running()

    job_id = _text_job_id_from_filename(file.filename)

    db_owner = get_job_owner(job_id)
    if db_owner and db_owner != owner and not is_admin(owner):
        raise HTTPException(status_code=403, detail="Job ID này thuộc người dùng khác")

    ensure_user_dirs(WORKSPACE, owner)
    work_dir = _user_dir(WORKSPACE, owner)
    job_dir = user_job_dir(WORKSPACE, owner, job_id)
    os.makedirs(job_dir, exist_ok=True)

    translated_pdf = os.path.join(job_dir, "output", "translated.pdf")
    if os.path.exists(translated_pdf) and not force:
        return {
            "job_id": job_id,
            "status": "already_done",
            "translated_pdf_url": f"/api/pdf/{job_id}/translated",
        }

    if force:
        import shutil as _shutil, time as _time
        _ts = int(_time.time())
        output_dir = os.path.join(job_dir, "output")
        if os.path.exists(output_dir):
            os.rename(output_dir, os.path.join(job_dir, f"output_v{_ts}"))
        progress_file_old = os.path.join(job_dir, "progress.json")
        if os.path.exists(progress_file_old):
            os.rename(progress_file_old, os.path.join(job_dir, f"progress_v{_ts}.json"))
        extract_dir_old = os.path.join(job_dir, "source_extracted")
        if os.path.exists(extract_dir_old):
            _shutil.rmtree(extract_dir_old)

    derived_title = title.strip() or os.path.splitext(file.filename)[0]
    try:
        latex_doc = _text_convert_to_latex(content, ext, title=derived_title)
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"File không decode được: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không convert được sang LaTeX: {e}")

    source_dir = os.path.join(job_dir, "source_extracted", "source")
    os.makedirs(source_dir, exist_ok=True)
    tex_path = os.path.join(source_dir, "main.tex")
    with open(tex_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(latex_doc)

    raw_path = os.path.join(job_dir, f"original{'' if ext == 'txt' else '.md'}.txt") if ext == "txt" \
        else os.path.join(job_dir, "original.md")
    with open(raw_path, "wb") as f:
        f.write(content)

    progress_file = os.path.join(job_dir, "progress.json")
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump({
            "status": "starting",
            "translated_chunks": {},
            "source_type": "latex",
            "uploaded_filename": file.filename,
            "uploaded_kind": ext,
            "title": derived_title,
        }, f)

    upsert_job(
        job_id,
        source_type="latex",
        arxiv_id=None,
        status="starting",
        username=owner,
    )

    try:
        dispatcher = get_dispatcher(latex_fallback=pipeline_manager)
        dispatcher.start_latex(job_id, tex_path, source_dir, work_dir)
    except Exception as e:
        log.warning("dispatcher_start_failed_falling_back", job_id=job_id, error=str(e))
        pipeline_manager.start(job_id, tex_path, source_dir, work_dir)

    jobs_enqueued_total.labels(source_type="latex").inc()

    return {
        "job_id": job_id,
        "status": "translating",
        "uploaded_filename": file.filename,
        "uploaded_kind": ext,
    }


# --- Upload .html / .htm ---
def _html_job_id_from_filename(filename: str) -> str:
    base = filename
    for suf in (".html", ".htm"):
        if base.lower().endswith(suf):
            base = base[: -len(suf)]
            break
    slug = re.sub(r"[^\w\-.]", "_", base)[:50] or "upload"
    return f"html_{slug}"


@app.post("/api/translate/upload-html")
@upload_limit
async def api_translate_upload_html(
    request: Request,
    file: UploadFile = File(...),
    force: bool = Form(False),
    title: str = Form(""),
    owner: str = Depends(get_owner),
):
    """Upload .html / .htm — extract text qua BeautifulSoup, convert sang LaTeX, dịch ra PDF."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")

    ext = _html_ext(file.filename)
    if ext is None:
        raise HTTPException(status_code=400, detail="Chỉ chấp nhận .html, .htm")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File rỗng")

    require_no_browser_running()

    job_id = _html_job_id_from_filename(file.filename)

    db_owner = get_job_owner(job_id)
    if db_owner and db_owner != owner and not is_admin(owner):
        raise HTTPException(status_code=403, detail="Job ID này thuộc người dùng khác")

    ensure_user_dirs(WORKSPACE, owner)
    work_dir = _user_dir(WORKSPACE, owner)
    job_dir = user_job_dir(WORKSPACE, owner, job_id)
    os.makedirs(job_dir, exist_ok=True)

    translated_pdf = os.path.join(job_dir, "output", "translated.pdf")
    if os.path.exists(translated_pdf) and not force:
        return {
            "job_id": job_id,
            "status": "already_done",
            "translated_pdf_url": f"/api/pdf/{job_id}/translated",
        }

    if force:
        import shutil as _shutil, time as _time
        _ts = int(_time.time())
        output_dir = os.path.join(job_dir, "output")
        if os.path.exists(output_dir):
            os.rename(output_dir, os.path.join(job_dir, f"output_v{_ts}"))
        progress_file_old = os.path.join(job_dir, "progress.json")
        if os.path.exists(progress_file_old):
            os.rename(progress_file_old, os.path.join(job_dir, f"progress_v{_ts}.json"))
        extract_dir_old = os.path.join(job_dir, "source_extracted")
        if os.path.exists(extract_dir_old):
            _shutil.rmtree(extract_dir_old)

    try:
        latex_doc, derived_title = _html_convert_to_latex(content, title=title.strip())
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không convert được HTML: {e}")

    source_dir = os.path.join(job_dir, "source_extracted", "source")
    os.makedirs(source_dir, exist_ok=True)
    tex_path = os.path.join(source_dir, "main.tex")
    with open(tex_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(latex_doc)

    raw_path = os.path.join(job_dir, "original.html")
    with open(raw_path, "wb") as f:
        f.write(content)

    progress_file = os.path.join(job_dir, "progress.json")
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump({
            "status": "starting",
            "translated_chunks": {},
            "source_type": "latex",
            "uploaded_filename": file.filename,
            "uploaded_kind": "html",
            "title": derived_title,
        }, f)

    upsert_job(
        job_id,
        source_type="latex",
        arxiv_id=None,
        status="starting",
        username=owner,
    )

    try:
        dispatcher = get_dispatcher(latex_fallback=pipeline_manager)
        dispatcher.start_latex(job_id, tex_path, source_dir, work_dir)
    except Exception as e:
        log.warning("dispatcher_start_failed_falling_back", job_id=job_id, error=str(e))
        pipeline_manager.start(job_id, tex_path, source_dir, work_dir)

    jobs_enqueued_total.labels(source_type="latex").inc()

    return {
        "job_id": job_id,
        "status": "translating",
        "uploaded_filename": file.filename,
        "uploaded_kind": "html",
        "title": derived_title,
    }


# --- Unified upload dispatcher — detect ext → route ---
def _detect_upload_kind(filename: str) -> str | None:
    """Return one of: 'pdf', 'latex', 'text', 'html', 'docx', or None."""
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith(".docx"):
        return "docx"
    if lower.endswith((".tex", ".tar.gz", ".tgz", ".zip")):
        return "latex"
    if lower.endswith((".txt", ".md", ".markdown")):
        return "text"
    if lower.endswith((".html", ".htm")):
        return "html"
    return None


@app.get("/api/translate/supported-formats")
async def api_supported_formats():
    """Liệt kê file extension được hỗ trợ — frontend dùng làm whitelist file picker."""
    return {
        "formats": [
            {"kind": "pdf", "exts": [".pdf"], "endpoint": "/api/pdf-translate/upload",
             "description": "PDF digital (có text layer, không phải scan)"},
            {"kind": "latex", "exts": [".tex", ".tar.gz", ".tgz", ".zip"],
             "endpoint": "/api/translate/upload",
             "description": "LaTeX source (file đơn .tex hoặc archive tar.gz/zip — vd Overleaf export)"},
            {"kind": "text", "exts": [".txt", ".md", ".markdown"],
             "endpoint": "/api/translate/upload-text",
             "description": "Plain text / Markdown — convert tự động sang LaTeX rồi dịch"},
            {"kind": "html", "exts": [".html", ".htm"],
             "endpoint": "/api/translate/upload-html",
             "description": "HTML — extract text qua BeautifulSoup rồi dịch (mất CSS)"},
            {"kind": "docx", "exts": [".docx"],
             "endpoint": "/api/office-translate/upload",
             "description": "Word .docx — dịch in-place, giữ nguyên format; preview qua LibreOffice"},
        ],
        "max_size_mb": 50,
    }


# --- Quality-estimation (COMETKiwi) model management ---
# UI dùng để hỏi "chưa tải, tải xuống?" rồi hiện thanh tiến độ 0-100% trước khi
# cho chọn judge backend cometkiwi-xl. Xem app/pdf/qe_manager.py.

@app.get("/api/quality/qe-status")
async def api_qe_status(backend: str = "cometkiwi-xl",
                        owner: str = Depends(get_owner)):
    """Model QE đã sẵn sàng chưa (gói + weights)? Kèm tiến độ tải nếu đang tải."""
    from app.pdf.qe_manager import get_status
    return get_status(backend)


@app.post("/api/quality/qe-download")
async def api_qe_download(backend: str = "cometkiwi-xl",
                          owner: str = Depends(get_owner)):
    """Bắt đầu tải weights model QE ở thread nền (idempotent)."""
    from app.pdf.qe_manager import start_download
    return start_download(backend)


@app.get("/api/quality/qe-download-status")
async def api_qe_download_status(backend: str = "cometkiwi-xl",
                                 owner: str = Depends(get_owner)):
    """Poll tiến độ tải: {state, percent 0-100, message}."""
    from app.pdf.qe_manager import get_download_status
    return get_download_status(backend)


@app.post("/api/documents/upload")
@upload_limit
async def api_documents_upload(
    request: Request,
    file: UploadFile = File(...),
    force: bool = Form(False),
    title: str = Form(""),
    mode: str = Form("standard"),
    agentic: bool = Form(False),
    num_tabs: int = Form(2),
    models: str = Form(""),
    judge_backend: str = Form("web"),
    owner: str = Depends(get_owner),
):
    """Unified upload — auto-detect file type rồi route sang handler tương ứng.

    Trả về JSON giống endpoint cụ thể, kèm `kind` để frontend biết job thuộc luồng nào.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")

    kind = _detect_upload_kind(file.filename)
    if kind is None:
        raise HTTPException(
            status_code=400,
            detail="Định dạng không hỗ trợ. Xem /api/translate/supported-formats.",
        )

    if kind == "latex":
        resp = await api_translate_upload(request, file=file, force=force, owner=owner)
    elif kind == "text":
        resp = await api_translate_upload_text(request, file=file, force=force, title=title, owner=owner)
    elif kind == "html":
        resp = await api_translate_upload_html(request, file=file, force=force, title=title, owner=owner)
    elif kind == "pdf":
        # Reuse PDF route's handler — import lazily to avoid circular import on startup
        from app.pdf.routes import upload_and_translate as _pdf_upload
        resp = await _pdf_upload(request, file=file, mode=mode, agentic=agentic,
                                 num_tabs=num_tabs, models=models,
                                 judge_backend=judge_backend, owner=owner)
    elif kind == "docx":
        from app.office.routes import upload_and_translate as _office_upload
        resp = await _office_upload(request, file=file, owner=owner)
    else:  # unreachable
        raise HTTPException(status_code=500, detail="Internal routing error")

    if isinstance(resp, dict):
        resp.setdefault("kind", kind)
    return resp


# --- Trạng thái job ---
@app.get("/api/job/{job_id}")
async def api_job_status(job_id: str, owner: str = Depends(get_owner)):
    job_dir = resolve_owned_job_dir(WORKSPACE, job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")

    result = {
        "job_id": job_id,
        "status": "pending",
    }

    # PDF gốc
    original_pdf = os.path.join(job_dir, "original.pdf")
    if os.path.exists(original_pdf):
        result["original_pdf_url"] = f"/api/pdf/{job_id}/original"

    # Tiến trình dịch
    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
        status = progress.get("status", "pending")
        result["status"] = status
        # Thêm thông tin tiến trình chi tiết
        translated_chunks = progress.get("translated_chunks", {})
        result["translated_chunks"] = len(translated_chunks)
        # Parse total từ status "translating X/Y"
        m = re.match(r"translating (\d+)/(\d+)", status)
        if m:
            result["current_chunk"] = int(m.group(1))
            result["total_chunks"] = int(m.group(2))

    # PDF đã dịch
    translated_pdf = os.path.join(job_dir, "output", "translated.pdf")
    if os.path.exists(translated_pdf):
        result["translated_pdf_url"] = f"/api/pdf/{job_id}/translated"
        # Preserve done_with_warnings; otherwise mark as done
        if result["status"] not in ("done_with_warnings",):
            result["status"] = "done"

    return result


# --- Glossary CRUD (LaTeX jobs) ---
@app.get("/api/job/{job_id}/glossary")
async def api_get_latex_glossary(job_id: str, owner: str = Depends(get_owner)):
    """Get the user-maintained glossary for a LaTeX job."""
    from app.pdf.glossary import normalize_locked
    job_dir = resolve_owned_job_dir(WORKSPACE, job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    g = progress.get("glossary") or {}
    return {
        "job_id": job_id,
        "terms": g.get("terms") or {},
        "enabled": g.get("enabled", True),
        "locked": normalize_locked(g.get("locked")),
        "count": len(g.get("terms") or {}),
    }


class LatexGlossaryUpdate(BaseModel):
    terms: dict[str, str] | None = None
    enabled: bool | None = None
    locked: list[str] | None = None


@app.put("/api/job/{job_id}/glossary")
async def api_put_latex_glossary(
    job_id: str,
    body: LatexGlossaryUpdate,
    owner: str = Depends(get_owner),
):
    """Update the LaTeX job glossary. Locked keys (lowercase EN) survive
    Gemini-driven term discovery and are flagged as inviolable in the prompt."""
    from app.pdf.glossary import normalize_locked
    from app.utils.safe_io import atomic_write_json
    job_dir = resolve_owned_job_dir(WORKSPACE, job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    g = progress.get("glossary") or {"terms": {}, "enabled": True}
    if body.terms is not None:
        g["terms"] = body.terms
    if body.enabled is not None:
        g["enabled"] = body.enabled
    if body.locked is not None:
        g["locked"] = normalize_locked(body.locked)
    progress["glossary"] = g
    atomic_write_json(progress_file, progress)
    return {
        "job_id": job_id,
        "terms": g.get("terms", {}),
        "enabled": g.get("enabled", True),
        "locked": g.get("locked", []),
        "count": len(g.get("terms", {})),
    }


# --- LLM Judge (LaTeX jobs) ---
@app.get("/api/judge/models")
async def api_judge_models():
    """Return available Ollama models for LLM-as-Judge."""
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
        return {"ollama_running": ollama_running, "ollama_url": OLLAMA_URL,
                "models": models, "default_model": "qwen2.5:7b"}
    except ImportError:
        return {"ollama_running": False, "models": [], "default_model": "qwen2.5:7b"}


class JudgeRequest(BaseModel):
    model: str = "qwen2.5:7b"
    max_segments: int = 10
    low_score_threshold: float = 0.70


@app.post("/api/job/{job_id}/judge")
async def api_run_judge(
    job_id: str,
    req: JudgeRequest,
    owner: str = Depends(get_owner),
):
    """Run LLM-as-Judge on low-quality segments of a LaTeX translation job."""
    job_dir = resolve_owned_job_dir(WORKSPACE, job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)

    if progress.get("status") not in ("done", "done_with_warnings"):
        raise HTTPException(400, "Job must be completed before running LLM judge")

    pairs = []

    # input_chunks + translated_chunks (LaTeX progress format)
    input_chunks = progress.get("input_chunks", {})
    translated_chunks = progress.get("translated_chunks", {})
    for key in sorted(input_chunks.keys(), key=lambda k: int(k) if k.isdigit() else 0):
        src = (input_chunks.get(key) or "").strip()
        mt = (translated_chunks.get(key) or "").strip()
        if src and mt and len(src) >= 20:
            idx = int(key) if key.isdigit() else len(pairs)
            pairs.append({"index": idx, "src": src, "mt": mt, "score_pct": 50})

    if not pairs:
        raise HTTPException(404, "No translation pairs found. Complete a translation job first.")

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
        judged = [r for r in results if r.get("llm_result")]
        avg_score = round(sum(r["llm_result"].get("mqm_score", r["llm_result"]["score"]) for r in judged) / len(judged)) if judged else None
        error_counts: dict = {}
        for r in judged:
            for e in (r["llm_result"].get("errors") or []):
                cat = e.get("category", "other")
                error_counts[cat] = error_counts.get(cat, 0) + 1

        judge_cache = {"model": req.model, "num_judged": len(judged),
                       "avg_score": avg_score, "error_counts": error_counts, "results": results}
        progress["llm_judge"] = judge_cache
        # Atomic write to avoid race condition with running pipeline
        atomic_write_json(progress_file, progress)

        return {"job_id": job_id, **judge_cache}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"LLM Judge failed: {e}")


@app.get("/api/job/{job_id}/judge")
async def api_get_judge(job_id: str, owner: str = Depends(get_owner)):
    """Get cached LLM Judge report."""
    job_dir = resolve_owned_job_dir(WORKSPACE, job_id, owner)
    progress_file = os.path.join(job_dir, "progress.json")
    if not os.path.exists(progress_file):
        raise HTTPException(404, "Job not found")
    with open(progress_file, "r", encoding="utf-8") as f:
        progress = json.load(f)
    judge = progress.get("llm_judge")
    if not judge:
        raise HTTPException(404, "No LLM Judge report. Run POST /api/job/{job_id}/judge first.")
    return {"job_id": job_id, **judge}


# --- Serve PDF ---
@app.get("/api/pdf/{job_id}/original")
async def serve_original_pdf(job_id: str, owner: str = Depends(get_owner)):
    job_dir = resolve_owned_job_dir(WORKSPACE, job_id, owner)
    pdf_path = os.path.join(job_dir, "original.pdf")
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="PDF gốc không tìm thấy")
    return FileResponse(pdf_path, media_type="application/pdf")


@app.get("/api/pdf/{job_id}/translated")
async def serve_translated_pdf(job_id: str, owner: str = Depends(get_owner)):
    job_dir = resolve_owned_job_dir(WORKSPACE, job_id, owner)
    pdf_path = os.path.join(job_dir, "output", "translated.pdf")
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="PDF dịch không tìm thấy")
    return FileResponse(pdf_path, media_type="application/pdf")


# ── Serve React frontend (built dist/) ────────────────────────────
# os.path.abspath(__file__) resolves symlinks/relative paths correctly
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))  # .../backend/app
_FRONTEND_DIST = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", "frontend", "dist"))

_index_html = os.path.join(_FRONTEND_DIST, "index.html")

if os.path.isdir(_FRONTEND_DIST):
    # Serve /assets/* and other static files directly
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_FRONTEND_DIST, "assets")),
        name="assets",
    )

    @app.get("/", include_in_schema=False)
    async def spa_root():
        return FileResponse(_index_html)

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        p = os.path.join(_FRONTEND_DIST, "favicon.svg")
        return FileResponse(p) if os.path.exists(p) else FileResponse(_index_html)

    # SPA catch-all: any non-API, non-asset path → index.html
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # Let /api/* and /assets/* fall through to their own handlers
        if full_path.startswith("api/") or full_path.startswith("assets/"):
            raise HTTPException(status_code=404)
        return FileResponse(_index_html)
