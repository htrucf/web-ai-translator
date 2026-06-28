"""Celery application factory.

Workers consume from two queues:
  - ``translate`` — long-running translation jobs (LaTeX or PDF)
  - ``default``   — short housekeeping tasks (session purge, cache warmup…)

Each worker runs with concurrency=1 because a Playwright Chrome instance is
expensive (~500MB RAM + needs its own user-data dir). Horizontal scale comes
from replicas in docker-compose, not from threads inside one worker.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")

celery_app = Celery(
    "web_ai_translator",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["app.tasks"],
)

celery_app.conf.update(
    # ── Routing ─────────────────────────────────────────────────────────────
    task_default_queue="default",
    task_routes={
        "app.tasks.translate_latex_job": {"queue": "translate"},
        "app.tasks.translate_pdf_job": {"queue": "translate"},
    },
    # ── Reliability ─────────────────────────────────────────────────────────
    # Acks-late so a worker crash mid-job sends the task back to the broker
    # instead of dropping it on the floor. Combined with `reject_on_worker_lost`
    # this gives at-least-once semantics — tasks must be idempotent (they are:
    # progress.json + DB upserts).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,         # one task at a time → fair distribution
    # ── Timeouts ───────────────────────────────────────────────────────────
    task_soft_time_limit=3 * 60 * 60,     # 3h soft (long PDFs)
    task_time_limit=4 * 60 * 60,          # 4h hard kill
    # ── Serialization ──────────────────────────────────────────────────────
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # ── Result backend retention ───────────────────────────────────────────
    result_expires=60 * 60 * 24 * 7,      # 7 days
    # ── Beat schedule ──────────────────────────────────────────────────────
    beat_schedule={
        "purge-expired-sessions": {
            "task": "app.tasks.purge_expired_sessions_task",
            "schedule": crontab(minute=0, hour="*"),   # hourly
        },
        "warm-arxiv-cache": {
            "task": "app.tasks.warm_arxiv_cache_task",
            "schedule": crontab(minute="*/30"),         # every 30 min
            "args": (),
        },
    },
)
