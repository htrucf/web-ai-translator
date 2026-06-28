"""Unified job dispatcher — Celery in production, subprocess fallback in dev.

Exposes the same interface the existing routes already use against
``PipelineManager`` so we don't have to touch every endpoint:

    start(job_id, ...)
    stop_job(job_id)
    is_job_running(job_id)
    running_jobs   (property)
    is_running     (property)

The backing implementation is chosen at process start:
  - ``DISPATCH_MODE=celery`` (default in Docker) → enqueue Celery tasks
  - ``DISPATCH_MODE=subprocess``                  → legacy subprocess path
  - ``auto``                                      → try Celery, fall back if
                                                    the broker is unreachable

Mapping ``job_id → task_id`` is kept in Redis so any API replica can cancel /
inspect any worker's job.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DISPATCH_MODE = os.getenv("DISPATCH_MODE", "auto").lower()
_TASK_MAP_KEY = "dispatch:task_map"   # Redis hash: job_id → task_id


# ── Celery-backed dispatcher ────────────────────────────────────────────────

class CeleryDispatcher:
    """Enqueues tasks via Celery; tracks task_id per job_id in Redis."""

    def __init__(self):
        from app.celery_app import celery_app
        from app.cache import _r as redis_client, _REDIS_OK
        self.celery = celery_app
        self.redis = redis_client if _REDIS_OK else None

    # ── Job map helpers ────────────────────────────────────────────────────

    def _put_task_id(self, job_id: str, task_id: str) -> None:
        if self.redis:
            try:
                self.redis.hset(_TASK_MAP_KEY, job_id, task_id)
            except Exception as e:
                logger.warning("redis hset failed: %s", e)

    def _get_task_id(self, job_id: str) -> Optional[str]:
        if not self.redis:
            return None
        try:
            return self.redis.hget(_TASK_MAP_KEY, job_id)
        except Exception:
            return None

    def _drop_task_id(self, job_id: str) -> None:
        if self.redis:
            try:
                self.redis.hdel(_TASK_MAP_KEY, job_id)
            except Exception:
                pass

    # ── Public API ─────────────────────────────────────────────────────────

    def start_latex(self, job_id: str, tex_path: str, source_dir: str, work_dir: str) -> None:
        # Cancel any running task with the same job_id (re-translate flow)
        self.stop_job(job_id)

        from app.tasks import translate_latex_job
        result = translate_latex_job.apply_async(
            args=[job_id, tex_path, source_dir, work_dir],
            queue="translate",
            task_id=f"latex:{job_id}",
        )
        self._put_task_id(job_id, result.id)
        logger.info("Dispatched LaTeX job %s → task %s", job_id, result.id)

    def start_pdf(self, job_id: str, pdf_path: str, work_dir: str, options: dict | None = None) -> None:
        self.stop_job(job_id)

        from app.tasks import translate_pdf_job
        result = translate_pdf_job.apply_async(
            args=[job_id, pdf_path, work_dir, options or {}],
            queue="translate",
            task_id=f"pdf:{job_id}",
        )
        self._put_task_id(job_id, result.id)
        logger.info("Dispatched PDF job %s → task %s", job_id, result.id)

    def stop_job(self, job_id: str) -> None:
        task_id = self._get_task_id(job_id)
        # Also try the deterministic task_ids we set above
        for tid in (task_id, f"latex:{job_id}", f"pdf:{job_id}"):
            if not tid:
                continue
            try:
                self.celery.control.revoke(tid, terminate=True, signal="SIGTERM")
            except Exception as e:
                logger.warning("revoke %s failed: %s", tid, e)
        self._drop_task_id(job_id)
        self._mark_cancelled(job_id)

    def is_job_running(self, job_id: str) -> bool:
        return job_id in self.running_jobs

    @property
    def running_jobs(self) -> list[str]:
        # Cross-reference the job map with Celery's active tasks; only jobs
        # that exist in both lists are truly running.
        try:
            insp = self.celery.control.inspect(timeout=1.0)
            active = insp.active() or {}
            active_ids: set[str] = set()
            for tasks in active.values():
                for t in tasks:
                    args = t.get("args") or []
                    if args and isinstance(args[0], str):
                        active_ids.add(args[0])
            return sorted(active_ids)
        except Exception:
            # Inspect can fail under heavy load — fall back to the map
            if not self.redis:
                return []
            try:
                return [k for k in (self.redis.hkeys(_TASK_MAP_KEY) or [])]
            except Exception:
                return []

    @property
    def is_running(self) -> bool:
        return len(self.running_jobs) > 0

    # ── Helpers ────────────────────────────────────────────────────────────

    def _mark_cancelled(self, job_id: str) -> None:
        from app.utils.safe_io import atomic_write_json
        from app.user_paths import find_job_path
        from app.config import settings

        path = find_job_path(settings.WORKSPACE_DIR, job_id, owner="", allow_legacy=True)
        if not path:
            return
        progress_file = os.path.join(path, "progress.json")
        if not os.path.exists(progress_file):
            return
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
            status = (progress.get("status") or "").strip()
            if not (status.startswith("done") or status.startswith("error") or status == "starting"):
                progress["status"] = "cancelled"
                atomic_write_json(progress_file, progress)
        except Exception as e:
            logger.warning("mark_cancelled failed for %s: %s", job_id, e)


# ── Subprocess fallback (delegates to the existing PipelineManager classes) ──

class SubprocessDispatcher:
    """Wraps the legacy PipelineManager / PdfPipelineManager so the API
    surface matches CeleryDispatcher."""

    def __init__(self, latex_mgr, pdf_mgr):
        self.latex = latex_mgr
        self.pdf = pdf_mgr

    def start_latex(self, job_id, tex_path, source_dir, work_dir):
        self.latex.start(job_id, tex_path, source_dir, work_dir)

    def start_pdf(self, job_id, pdf_path, work_dir, options=None):
        opts = options or {}
        self.pdf.start(
            job_id,
            pdf_path=pdf_path,
            mode=opts.get("mode", "standard"),
            agentic=bool(opts.get("agentic")),
            work_dir=work_dir,
            num_tabs=max(1, min(6, int(opts.get("num_tabs", 2) or 2))),
            models=opts.get("models"),
            judge_backend=opts.get("judge_backend", "web"),
        )

    def stop_job(self, job_id):
        if self.latex.is_job_running(job_id):
            self.latex.stop_job(job_id)
        if self.pdf.is_job_running(job_id):
            self.pdf.stop_job(job_id)

    def is_job_running(self, job_id):
        return self.latex.is_job_running(job_id) or self.pdf.is_job_running(job_id)

    @property
    def running_jobs(self):
        return list({*self.latex.running_jobs, *self.pdf.running_jobs})

    @property
    def is_running(self):
        return self.latex.is_running or self.pdf.is_running


# ── Factory ─────────────────────────────────────────────────────────────────

_dispatcher = None
_dispatcher_lock = threading.Lock()


def get_dispatcher(latex_fallback=None, pdf_fallback=None):
    """Lazy singleton. Caller passes the legacy managers so we can fall back
    gracefully if Celery's broker is unreachable."""
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher

    with _dispatcher_lock:
        if _dispatcher is not None:
            return _dispatcher

        want_celery = DISPATCH_MODE in ("celery", "auto")
        if want_celery:
            try:
                d = CeleryDispatcher()
                # Quick ping: does the broker answer?
                try:
                    d.celery.control.inspect(timeout=1.0).ping()
                except Exception:
                    if DISPATCH_MODE == "celery":
                        # Caller explicitly asked for celery — log loudly but use it
                        logger.warning("Celery broker not responding; using it anyway")
                    else:
                        raise RuntimeError("celery broker unreachable")
                _dispatcher = d
                logger.info("Dispatcher: Celery (broker=%s)", os.getenv("CELERY_BROKER_URL", "?"))
                return _dispatcher
            except Exception as e:
                if DISPATCH_MODE == "celery":
                    raise
                logger.warning("Celery unavailable (%s) — falling back to subprocess", e)

        if latex_fallback is None or pdf_fallback is None:
            raise RuntimeError("Subprocess dispatcher requested but fallback managers were not provided")
        _dispatcher = SubprocessDispatcher(latex_fallback, pdf_fallback)
        logger.info("Dispatcher: subprocess fallback")
        return _dispatcher
