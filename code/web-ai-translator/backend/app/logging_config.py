"""Structured logging with structlog.

JSON output by default (machine-parseable for Loki / ELK). Human-readable
console renderer kicks in when ``LOG_FORMAT=console`` is set, which is nicer
during local dev.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json").lower()


def setup_logging() -> None:
    """Configure both stdlib logging and structlog so every log line ends up
    with the same shape regardless of who emits it."""

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        timestamper,
    ]

    if LOG_FORMAT == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(LOG_LEVEL)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib → structlog so libraries (FastAPI, SQLAlchemy, Celery…)
    # produce uniformly-shaped lines.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(LOG_LEVEL)

    # Quiet down noisy libraries unless explicitly turned up
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(os.getenv(f"LOG_LEVEL_{noisy.upper().replace('.', '_')}", "WARNING"))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


# ── Helpers to bind request-scoped context (job_id, user, request_id) ───────

def bind_context(**kwargs) -> None:
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
