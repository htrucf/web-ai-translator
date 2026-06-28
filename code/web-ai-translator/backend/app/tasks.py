"""Celery tasks — long-running translation pipelines run here.

Each task is the queue-side wrapper around the existing pipeline classes
(``TranslationPipeline`` for LaTeX, ``PdfTranslationPipeline`` for PDFs).

Why wrap rather than rewrite the pipeline:
  - The pipeline is the part that actually works today; we should not touch it.
  - Celery only needs to provide the *execution context* — process isolation,
    retries on worker crash, progress reporting.

Progress reporting flow:
  1. Pipeline writes to ``progress.json`` (unchanged).
  2. Task wrapper polls / hooks into the progress writes and publishes events
     via ``app.ws.publish_event`` so connected WebSocket clients see updates.
  3. On completion / failure, the task syncs DB rows and emits a terminal
     event.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from celery import Task
from celery.utils.log import get_task_logger

from app.celery_app import celery_app
from app import cache, ws
from app.config import settings

logger = get_task_logger(__name__)


# ── Internals ────────────────────────────────────────────────────────────────

def _progress_file(work_dir: str, job_id: str) -> str:
    return os.path.join(work_dir, "jobs", job_id, "progress.json")


def _load_progress(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _emit_from_progress(job_id: str, progress: dict) -> None:
    """Translate progress.json shape into a WS event."""
    status = progress.get("status", "")
    translated = len(progress.get("translated_chunks") or {})

    current = translated
    total = 0
    if status.startswith("translating "):
        try:
            head = status.split(" ", 1)[1]
            current_str, total_str = head.split("/")
            current = int(current_str)
            total = int(total_str)
        except Exception:
            pass

    ws.emit_progress(
        job_id,
        status=status,
        current=current,
        total=total,
        translated_chunks=translated,
        quality_score=(progress.get("quality") or {}).get("score"),
    )
    cache.cache_progress_snapshot(job_id, progress, ttl=60)


async def _watch_progress(job_id: str, progress_path: str, stop: asyncio.Event) -> None:
    """Background watcher: emits a WS event whenever progress.json changes."""
    last_mtime = 0.0
    while not stop.is_set():
        try:
            if os.path.exists(progress_path):
                mtime = os.path.getmtime(progress_path)
                if mtime != last_mtime:
                    last_mtime = mtime
                    progress = _load_progress(progress_path)
                    if progress:
                        _emit_from_progress(job_id, progress)
        except Exception as e:
            logger.warning("progress watch error (%s): %s", job_id, e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


class _PipelineTaskBase(Task):
    """Shared Celery task config — autoretry on transient infra errors."""
    autoretry_for = (ConnectionError, TimeoutError)
    retry_kwargs = {"max_retries": 3, "countdown": 30}
    retry_backoff = True


# ── LaTeX translation task ───────────────────────────────────────────────────

@celery_app.task(
    name="app.tasks.translate_latex_job",
    base=_PipelineTaskBase,
    bind=True,
)
def translate_latex_job(self, job_id: str, tex_path: str, source_dir: str, work_dir: str) -> dict:
    from app.services.pipeline import TranslationPipeline
    from app.database import sync_job_to_db

    progress_path = _progress_file(work_dir, job_id)
    ws.emit_progress(job_id, status="starting", current=0, total=0)

    async def _run() -> dict:
        pipeline = TranslationPipeline(work_dir=work_dir)
        stop = asyncio.Event()
        watcher = asyncio.create_task(_watch_progress(job_id, progress_path, stop))

        try:
            await pipeline.run(tex_path=tex_path, job_id=job_id, source_dir=source_dir)
        finally:
            stop.set()
            try:
                await watcher
            except Exception:
                pass

        return _load_progress(progress_path)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            progress = loop.run_until_complete(_run())
        finally:
            loop.close()

        # Sync final state to DB + emit done
        try:
            sync_job_to_db(job_id, progress, os.path.abspath(settings.WORKSPACE_DIR))
        except Exception as e:
            logger.warning("DB sync failed for %s: %s", job_id, e)
        cache.invalidate_job(job_id)
        ws.emit_done(job_id, status=progress.get("status", "done"))
        return {"job_id": job_id, "status": progress.get("status", "done")}
    except Exception as e:
        logger.exception("LaTeX task failed: %s", job_id)
        ws.emit_error(job_id, error=str(e))
        # Re-raise so Celery records the failure
        raise


# ── PDF translation task ─────────────────────────────────────────────────────

@celery_app.task(
    name="app.tasks.translate_pdf_job",
    base=_PipelineTaskBase,
    bind=True,
)
def translate_pdf_job(self, job_id: str, pdf_path: str, work_dir: str, options: dict | None = None) -> dict:
    opts = options or {}
    mode = opts.get("mode", "standard")
    agentic = bool(opts.get("agentic"))
    num_tabs = max(1, min(6, int(opts.get("num_tabs", 2) or 2)))
    models = opts.get("models")
    judge_backend = opts.get("judge_backend", "web")

    progress_path = _progress_file(work_dir, job_id)
    ws.emit_progress(job_id, status="starting", current=0, total=0)

    async def _run() -> dict:
        # Multi-agent coordinator (Planner → Glossary → Translator → Critic)
        # has the same surface as the single-pipeline path.
        if agentic:
            from app.pdf.agents import MultiAgentCoordinator
            pipeline = MultiAgentCoordinator(
                work_dir=work_dir, mode=mode, num_tabs=num_tabs,
                models=models, judge_backend=judge_backend,
            )
        else:
            from app.pdf.pipeline import PdfTranslationPipeline
            pipeline = PdfTranslationPipeline(work_dir=work_dir, mode=mode)

        stop = asyncio.Event()
        watcher = asyncio.create_task(_watch_progress(job_id, progress_path, stop))

        try:
            await pipeline.run(pdf_path=pdf_path, job_id=job_id)
        finally:
            stop.set()
            try:
                await watcher
            except Exception:
                pass

        return _load_progress(progress_path)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            progress = loop.run_until_complete(_run())
        finally:
            loop.close()

        cache.invalidate_job(job_id)
        ws.emit_done(job_id, status=progress.get("status", "done"))
        return {"job_id": job_id, "status": progress.get("status", "done")}
    except Exception as e:
        logger.exception("PDF task failed: %s", job_id)
        ws.emit_error(job_id, error=str(e))
        raise


# ── Housekeeping tasks (run from beat) ──────────────────────────────────────

@celery_app.task(name="app.tasks.purge_expired_sessions_task")
def purge_expired_sessions_task() -> int:
    from app.database import purge_expired_sessions
    from app.auth import IDLE_TIMEOUT
    removed = purge_expired_sessions(time.time() - IDLE_TIMEOUT)
    if removed:
        logger.info("Purged %d expired session(s)", removed)
    return removed


@celery_app.task(name="app.tasks.warm_arxiv_cache_task")
def warm_arxiv_cache_task() -> int:
    """Refresh a few popular-search caches so the first user of the day
    doesn't pay the cold-start latency."""
    # Stub — production would track popular queries; for DATN this is enough
    # to demonstrate scheduled tasks via Celery beat.
    return 0
