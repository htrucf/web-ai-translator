"""Job recovery on backend startup.

A backend (or worker) restart can leave jobs in a half-finished state:
  - status="starting" / "translating X/Y" but no live worker is running them.
  - subprocess died after writing partial chunks but before flushing status.

Recovery strategy:
  1. Scan all per-user job dirs (and legacy `workspace/jobs/`) for
     `progress.json` rows whose status is non-terminal.
  2. Cross-check against Celery's "active" tasks via the inspect API.
  3. Anything non-terminal AND not active is marked ``interrupted`` so the
     frontend can offer a "Resume" button.

Idempotent — safe to run on every cold start.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Iterable

from app.utils.safe_io import atomic_write_json

logger = logging.getLogger(__name__)

NON_TERMINAL_PREFIXES = ("starting", "translating", "extracting", "compiling", "resuming")
TERMINAL_PREFIXES = ("done", "error", "cancelled", "interrupted", "compile_error")


def _iter_progress_files(workspace: str) -> Iterable[tuple[str, str]]:
    """Yield (job_id, progress_path) pairs across all known job dirs."""
    legacy = os.path.join(workspace, "jobs")
    if os.path.isdir(legacy):
        for jid in os.listdir(legacy):
            p = os.path.join(legacy, jid, "progress.json")
            if os.path.exists(p):
                yield jid, p

    users_root = os.path.join(workspace, "users")
    if os.path.isdir(users_root):
        for user in os.listdir(users_root):
            jobs_root = os.path.join(users_root, user, "jobs")
            if not os.path.isdir(jobs_root):
                continue
            for jid in os.listdir(jobs_root):
                p = os.path.join(jobs_root, jid, "progress.json")
                if os.path.exists(p):
                    yield jid, p


def _active_celery_jobs() -> set[str]:
    """Job IDs currently being processed by a Celery worker.

    Returns an empty set if Celery / broker isn't reachable — then we play it
    safe and just mark everything non-terminal as interrupted.
    """
    try:
        from app.celery_app import celery_app
        insp = celery_app.control.inspect(timeout=2.0)
        active = insp.active() or {}
        out: set[str] = set()
        for tasks in active.values():
            for t in tasks:
                args = t.get("args") or []
                if args and isinstance(args[0], str):
                    out.add(args[0])
        return out
    except Exception as e:
        logger.warning("celery inspect failed: %s", e)
        return set()


def recover_interrupted_jobs(workspace: str) -> dict:
    """Mark stale in-progress jobs as `interrupted`. Returns a small report."""
    active = _active_celery_jobs()
    interrupted: list[str] = []
    scanned = 0
    for job_id, path in _iter_progress_files(workspace):
        scanned += 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                progress = json.load(f)
        except Exception:
            continue

        status = (progress.get("status") or "").strip()
        if not status:
            continue
        if status.startswith(TERMINAL_PREFIXES):
            continue
        if not status.startswith(NON_TERMINAL_PREFIXES):
            continue
        if job_id in active:
            continue

        progress["status"] = "interrupted"
        progress["interrupted_at"] = _utcnow()
        try:
            atomic_write_json(path, progress)
            interrupted.append(job_id)
            logger.info("Marked job %s as interrupted (was: %s)", job_id, status)
        except Exception as e:
            logger.warning("Could not mark %s interrupted: %s", job_id, e)

    return {
        "scanned": scanned,
        "active": len(active),
        "interrupted": len(interrupted),
        "interrupted_jobs": interrupted,
    }


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
