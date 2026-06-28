"""Redis cache helpers.

Used for:
  - Term cache (lookup of `en_term → vi_term` across jobs, hot in memory)
  - arXiv metadata cache (search results, paper info)
  - Per-job progress snapshots (so the WebSocket fan-out doesn't hit disk)
  - Rate-limiter buckets (slowapi backend)

Falls back to an in-memory dict if Redis is unreachable so tests / local dev
don't break when the user forgets to start docker-compose.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("CACHE_REDIS_URL", os.getenv("REDIS_URL", "redis://localhost:6379/3"))
DEFAULT_TTL = int(os.getenv("CACHE_DEFAULT_TTL", "3600"))

try:
    import redis
    _r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
    _r.ping()
    _REDIS_OK = True
    logger.info("Cache: connected to %s", REDIS_URL)
except Exception as e:
    _r = None
    _REDIS_OK = False
    logger.warning("Cache: Redis unreachable (%s) — using in-memory fallback", e)


class _MemoryCache:
    """Tiny TTL-aware dict used when Redis is down. Single-process only."""
    def __init__(self):
        self._d: dict[str, tuple[float, str]] = {}
    def get(self, k):
        v = self._d.get(k)
        if not v: return None
        exp, val = v
        if exp and exp < time.time():
            self._d.pop(k, None)
            return None
        return val
    def setex(self, k, ttl, val):
        self._d[k] = (time.time() + ttl, val)
    def set(self, k, val):
        self._d[k] = (0.0, val)
    def delete(self, *keys):
        for k in keys:
            self._d.pop(k, None)
    def keys(self, pattern):
        import fnmatch
        return [k for k in self._d.keys() if fnmatch.fnmatch(k, pattern)]
    def incr(self, k):
        cur = int(self._d.get(k, (0.0, "0"))[1])
        self._d[k] = (0.0, str(cur + 1))
        return cur + 1
    def expire(self, k, ttl):
        if k in self._d:
            self._d[k] = (time.time() + ttl, self._d[k][1])


_memory = _MemoryCache()


def _client():
    return _r if _REDIS_OK else _memory


# ── Generic JSON cache ───────────────────────────────────────────────────────

def get_json(key: str) -> Any | None:
    raw = _client().get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def set_json(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    payload = json.dumps(value, ensure_ascii=False, default=str)
    if ttl > 0:
        _client().setex(key, ttl, payload)
    else:
        _client().set(key, payload)


def delete(*keys: str) -> None:
    if keys:
        _client().delete(*keys)


def keys(pattern: str) -> list[str]:
    return list(_client().keys(pattern))


# ── Domain-specific helpers ──────────────────────────────────────────────────

def cache_arxiv_search(query: str, results: list[dict], ttl: int = 600) -> None:
    set_json(f"arxiv:search:{query.lower().strip()}", results, ttl)


def get_arxiv_search(query: str) -> list[dict] | None:
    return get_json(f"arxiv:search:{query.lower().strip()}")


def cache_arxiv_paper(arxiv_id: str, info: dict, ttl: int = 86400) -> None:
    set_json(f"arxiv:paper:{arxiv_id}", info, ttl)


def get_arxiv_paper(arxiv_id: str) -> dict | None:
    return get_json(f"arxiv:paper:{arxiv_id}")


def cache_progress_snapshot(job_id: str, snapshot: dict, ttl: int = 60) -> None:
    """Hot cache of progress.json so polling/WS readers skip disk."""
    set_json(f"job:progress:{job_id}", snapshot, ttl)


def get_progress_snapshot(job_id: str) -> dict | None:
    return get_json(f"job:progress:{job_id}")


def cache_term(en_term: str, vi_term: str, ttl: int = 86400 * 7) -> None:
    set_json(f"term:{en_term.lower().strip()}", vi_term, ttl)


def get_term(en_term: str) -> str | None:
    val = get_json(f"term:{en_term.lower().strip()}")
    return val if isinstance(val, str) else None


def invalidate_job(job_id: str) -> None:
    delete(f"job:progress:{job_id}")


# ── Rate-limit primitive (used by slowapi backend) ───────────────────────────

def incr_with_ttl(key: str, ttl: int) -> int:
    """Atomic INCR; on first hit, sets the TTL."""
    if _REDIS_OK:
        pipe = _r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        cnt, _ = pipe.execute()
        return int(cnt)
    cnt = _memory.incr(key)
    _memory.expire(key, ttl)
    return cnt
