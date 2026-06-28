"""Per-user / per-IP rate limiting via slowapi.

Keyed on the authenticated user when available (so login + first request
under the same IP doesn't burn through the bucket twice), falling back to the
remote IP for anonymous endpoints.

Limits are environment-configurable so admins can dial them up/down without
touching code:

  RATE_LIMIT_TRANSLATE   default ``5/minute``    expensive — kicks off a job
  RATE_LIMIT_UPLOAD      default ``10/minute``   PDF uploads
  RATE_LIMIT_API         default ``120/minute``  everything else
  RATE_LIMIT_AUTH        default ``20/minute``   login / register
"""

from __future__ import annotations

import os
from typing import Callable

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from app.auth import current_username


REDIS_URL = os.getenv("REDIS_URL", "memory://")

# ── Limits ──────────────────────────────────────────────────────────────────
LIMIT_TRANSLATE = os.getenv("RATE_LIMIT_TRANSLATE", "5/minute")
LIMIT_UPLOAD = os.getenv("RATE_LIMIT_UPLOAD", "10/minute")
LIMIT_API = os.getenv("RATE_LIMIT_API", "120/minute")
LIMIT_AUTH = os.getenv("RATE_LIMIT_AUTH", "20/minute")


def _key(request: Request) -> str:
    """User-first key. Falls back to IP for unauthenticated requests."""
    try:
        user = current_username(request)
    except Exception:
        user = None
    return f"user:{user}" if user else f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=_key,
    storage_uri=REDIS_URL if REDIS_URL.startswith(("redis://", "rediss://")) else "memory://",
    default_limits=[LIMIT_API],
    strategy="moving-window",
)


def setup_rate_limit(app: FastAPI) -> None:
    """Install slowapi middleware + 429 handler."""
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)


# Convenient decorators re-exported so route files can write
#   @translate_limit
#   async def api_translate(...): ...
translate_limit = limiter.limit(LIMIT_TRANSLATE)
upload_limit = limiter.limit(LIMIT_UPLOAD)
auth_limit = limiter.limit(LIMIT_AUTH)
api_limit = limiter.limit(LIMIT_API)
