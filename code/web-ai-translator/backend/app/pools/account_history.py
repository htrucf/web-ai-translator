"""Per-account history tracker for adaptive scheduling.

Records signals the schedulers need to make a decision:
  - success / fail counters (lifetime + rolling window)
  - last latency observed (seconds)
  - number of cooldowns triggered (lifetime)
  - last-used wall-clock time (for LRU and "time-since-last-use")

Backed by Redis when available; in-memory fallback for single-process dev.

Schedulers READ the history; pipelines/translator WRITE to it via
``record_outcome`` after each chunk and ``record_cooldown`` on soft-ban.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import Optional

logger = logging.getLogger(__name__)

# Rolling-window size for the recent success/fail signal. Older outcomes still
# count toward lifetime totals but the adaptive scheduler weights the window.
WINDOW = int(os.getenv("ACCOUNT_HISTORY_WINDOW", "20"))


@dataclass
class AccountStats:
    email: str
    success: int = 0
    fail: int = 0
    cooldowns: int = 0
    last_latency: float = 0.0
    avg_latency: float = 0.0
    last_used_ts: float = 0.0
    last_cooldown_ts: float = 0.0
    # Rolling window of recent outcomes (1 = success, 0 = fail). Bounded by WINDOW.
    recent: list[int] = field(default_factory=list)

    def recent_success_rate(self) -> float:
        if not self.recent:
            return 1.0  # optimistic prior — untried accounts get a fair shot
        return sum(self.recent) / len(self.recent)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AccountStats":
        d = dict(d)
        d.setdefault("recent", [])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class AccountHistory:
    """Thread-safe per-account stats store with Redis persistence."""

    _KEY_FMT = "account:{email}:stats"

    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._mem: dict[str, AccountStats] = {}
        self._lock = threading.Lock()

    # ── Read ───────────────────────────────────────────────────────────────

    def get(self, email: str) -> AccountStats:
        with self._lock:
            if self.redis is not None:
                try:
                    raw = self.redis.get(self._KEY_FMT.format(email=email))
                    if raw:
                        return AccountStats.from_dict(json.loads(raw))
                except Exception as e:
                    logger.debug("history get from redis failed: %s", e)
            return self._mem.get(email) or AccountStats(email=email)

    def get_all(self, emails: list[str]) -> dict[str, AccountStats]:
        return {e: self.get(e) for e in emails}

    # ── Write ──────────────────────────────────────────────────────────────

    def record_outcome(self, email: str, success: bool, latency: float) -> None:
        """Record one chunk/job outcome. Called after each translation unit."""
        with self._lock:
            stats = self._read_locked(email)
            if success:
                stats.success += 1
            else:
                stats.fail += 1
            stats.last_latency = float(latency)
            # EMA on latency for the adaptive scheduler
            if stats.avg_latency == 0.0:
                stats.avg_latency = float(latency)
            else:
                stats.avg_latency = 0.7 * stats.avg_latency + 0.3 * float(latency)
            stats.recent.append(1 if success else 0)
            if len(stats.recent) > WINDOW:
                stats.recent = stats.recent[-WINDOW:]
            stats.last_used_ts = time.time()
            self._write_locked(stats)

    def record_cooldown(self, email: str) -> None:
        with self._lock:
            stats = self._read_locked(email)
            stats.cooldowns += 1
            stats.last_cooldown_ts = time.time()
            # A cooldown is implicitly a fail signal for the recent window.
            stats.recent.append(0)
            if len(stats.recent) > WINDOW:
                stats.recent = stats.recent[-WINDOW:]
            self._write_locked(stats)

    def touch(self, email: str) -> None:
        """Mark account as 'used now' without recording an outcome (acquire-time)."""
        with self._lock:
            stats = self._read_locked(email)
            stats.last_used_ts = time.time()
            self._write_locked(stats)

    def reset(self, email: Optional[str] = None) -> None:
        with self._lock:
            if email is None:
                self._mem.clear()
                if self.redis is not None:
                    try:
                        for key in self.redis.keys("account:*:stats"):
                            self.redis.delete(key)
                    except Exception:
                        pass
            else:
                self._mem.pop(email, None)
                if self.redis is not None:
                    try:
                        self.redis.delete(self._KEY_FMT.format(email=email))
                    except Exception:
                        pass

    # ── Internals ──────────────────────────────────────────────────────────

    def _read_locked(self, email: str) -> AccountStats:
        if self.redis is not None:
            try:
                raw = self.redis.get(self._KEY_FMT.format(email=email))
                if raw:
                    return AccountStats.from_dict(json.loads(raw))
            except Exception:
                pass
        return self._mem.get(email) or AccountStats(email=email)

    def _write_locked(self, stats: AccountStats) -> None:
        self._mem[stats.email] = stats
        if self.redis is not None:
            try:
                self.redis.set(
                    self._KEY_FMT.format(email=stats.email),
                    json.dumps(stats.to_dict()),
                )
            except Exception as e:
                logger.debug("history write to redis failed: %s", e)


# ── Singleton ───────────────────────────────────────────────────────────────

_history: AccountHistory | None = None


def get_account_history() -> AccountHistory:
    global _history
    if _history is not None:
        return _history
    redis_client = None
    try:
        from app.cache import _r, _REDIS_OK
        if _REDIS_OK:
            redis_client = _r
    except Exception:
        pass
    _history = AccountHistory(redis_client=redis_client)
    return _history
