"""Gemini account pool with Redis-backed lease + cooldown.

Each account is a `{email, browser_profile_dir, status}` record. Workers
``acquire()`` an account at job start and ``release()`` it when finished. If
the worker detects a soft-ban (CAPTCHA, rate-limit), it calls ``cooldown()``
to mark the account unavailable for N minutes.

Failure modes:
  - No accounts available → ``acquire()`` blocks up to ``timeout`` seconds.
  - Worker crashes mid-job → lease TTL expires, account auto-returns to pool.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

from app.pools.account_history import get_account_history
from app.pools.schedulers import Scheduler, SchedulerContext, build_scheduler
from app.audit import log_event

logger = logging.getLogger(__name__)

# Lease lasts long enough for a typical job — workers extend via heartbeat
LEASE_TTL = int(os.getenv("ACCOUNT_LEASE_TTL", "7200"))   # 2h
COOLDOWN_SECONDS = int(os.getenv("ACCOUNT_COOLDOWN", "1800"))  # 30m


@dataclass
class GeminiAccount:
    email: str
    profile_dir: str             # path to Playwright user-data-dir
    proxy: Optional[str] = None  # optional bound proxy
    notes: str = ""


class AccountPool:
    def __init__(
        self,
        accounts: list[GeminiAccount],
        redis_client=None,
        scheduler: Optional[Scheduler] = None,
    ):
        self.accounts = {a.email: a for a in accounts}
        self.redis = redis_client
        self.scheduler: Scheduler = scheduler or build_scheduler()
        self.history = get_account_history()
        # Seed the "free" set on first construction
        if redis_client is not None and accounts:
            try:
                # NX so we don't clobber a running deployment
                redis_client.sadd("accounts:all", *self.accounts.keys())
                pipe = redis_client.pipeline()
                for email in self.accounts:
                    pipe.set(f"account:{email}:state", "free", nx=True)
                pipe.execute()
            except Exception as e:
                logger.warning("AccountPool seed failed: %s", e)

    # ── Strategy control ──────────────────────────────────────────────────

    def set_scheduler(self, name: str) -> str:
        """Swap the scheduler at runtime. Returns the resolved name."""
        prev = self.scheduler_name()
        self.scheduler = build_scheduler(name)
        logger.info("AccountPool: scheduler set to %s", self.scheduler.name)
        log_event(
            "scheduler.strategy_changed",
            previous=prev,
            current=self.scheduler.name,
        )
        return self.scheduler.name

    def scheduler_name(self) -> str:
        return getattr(self.scheduler, "name", "unknown")

    # ── Acquire / release ──────────────────────────────────────────────────

    def acquire(self, worker_id: str, timeout: float = 30.0) -> Optional[GeminiAccount]:
        """Lease an account chosen by the active scheduler.

        Returns None if no account becomes free within ``timeout`` seconds.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            free = [e for e in self.accounts if self._state(e) == "free"]
            if free:
                ctx = SchedulerContext.make(free, self.history.get_all(free))
                pick = self.scheduler.pick(ctx)
                if pick and self._try_lease(pick, worker_id):
                    self.history.touch(pick)
                    logger.info(
                        "AccountPool: leased %s to %s (strategy=%s)",
                        pick, worker_id, self.scheduler_name(),
                    )
                    log_event(
                        "scheduler.pick",
                        strategy=self.scheduler_name(),
                        chosen=pick,
                        candidates=free,
                        candidate_count=len(free),
                        worker_id=worker_id,
                    )
                    return self.accounts[pick]
            time.sleep(1.0)
        return None

    # ── Outcome reporting (used by translator/pipeline) ──────────────────

    def report_outcome(self, email: str, success: bool, latency: float) -> None:
        """Tell the history tracker how a chunk/job went on this account."""
        try:
            self.history.record_outcome(email, success=success, latency=float(latency))
        except Exception as e:
            logger.debug("report_outcome failed: %s", e)

    def release(self, email: str, worker_id: str) -> None:
        if self.redis is None:
            return
        try:
            pipe = self.redis.pipeline()
            pipe.delete(f"account:{email}:lease")
            pipe.set(f"account:{email}:state", "free")
            pipe.execute()
            logger.info("AccountPool: released %s from %s", email, worker_id)
        except Exception as e:
            logger.warning("release failed: %s", e)

    def heartbeat(self, email: str, worker_id: str) -> None:
        """Extend the lease — call periodically during long jobs."""
        if self.redis is None:
            return
        try:
            cur = self.redis.get(f"account:{email}:lease")
            if cur == worker_id:
                self.redis.expire(f"account:{email}:lease", LEASE_TTL)
        except Exception:
            pass

    def cooldown(self, email: str, reason: str = "rate_limit", seconds: int = COOLDOWN_SECONDS) -> None:
        # Record before mutating Redis so the history reflects the event even
        # if Redis is unavailable.
        try:
            self.history.record_cooldown(email)
        except Exception:
            pass
        log_event(
            "scheduler.cooldown",
            account_email=email,
            reason=reason,
            duration_seconds=seconds,
        )
        if self.redis is None:
            return
        try:
            pipe = self.redis.pipeline()
            pipe.set(f"account:{email}:state", "cooldown")
            pipe.setex(f"account:{email}:cooldown", seconds, reason)
            pipe.delete(f"account:{email}:lease")
            pipe.execute()
            logger.warning("AccountPool: cooled down %s for %ds (%s)", email, seconds, reason)
        except Exception as e:
            logger.warning("cooldown failed: %s", e)
            log_event("scheduler.cooldown_redis_failed",
                      account_email=email,
                      error=str(e)[:200])

    # ── Inspection (used by /api endpoints + Prometheus) ──────────────────

    def stats(self) -> dict:
        """Per-account state summary. Cheap enough for /metrics scraping."""
        if self.redis is None:
            return {"total": len(self.accounts), "free": len(self.accounts), "in_use": 0, "cooldown": 0}
        free = in_use = cd = 0
        for email in self.accounts:
            state = self._state(email)
            if state == "free":
                free += 1
            elif state == "cooldown":
                cd += 1
            else:
                in_use += 1
        return {"total": len(self.accounts), "free": free, "in_use": in_use, "cooldown": cd}

    # ── Internals ──────────────────────────────────────────────────────────

    def _try_lease(self, email: str, worker_id: str) -> bool:
        if self.redis is None:
            return True  # single-process: any acquire wins
        try:
            if self._state(email) != "free":
                return False
            # Atomic SET NX EX — first writer wins
            ok = self.redis.set(f"account:{email}:lease", worker_id, nx=True, ex=LEASE_TTL)
            if ok:
                self.redis.set(f"account:{email}:state", "in_use")
                return True
        except Exception as e:
            logger.warning("_try_lease error: %s", e)
        return False

    def _state(self, email: str) -> str:
        if self.redis is None:
            return "free"
        try:
            # Cooldown key auto-expires; if it's gone but state is still "cooldown",
            # promote back to free.
            if self.redis.get(f"account:{email}:state") == "cooldown":
                if not self.redis.exists(f"account:{email}:cooldown"):
                    self.redis.set(f"account:{email}:state", "free")
            return self.redis.get(f"account:{email}:state") or "free"
        except Exception:
            return "free"


# ── Singleton wiring ────────────────────────────────────────────────────────

_pool: AccountPool | None = None


def _load_accounts_from_file() -> list[GeminiAccount]:
    path = os.getenv("GEMINI_ACCOUNTS_FILE", "")
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [GeminiAccount(**item) for item in raw]
    except Exception as e:
        logger.warning("Could not load accounts file %s: %s", path, e)
        return []


def get_account_pool() -> AccountPool:
    """Lazy singleton — picks up accounts from the env-configured JSON file."""
    global _pool
    if _pool is not None:
        return _pool
    accounts = _load_accounts_from_file()
    # In dev, fall back to a single anonymous "default" account so the pipeline
    # still works without a configured pool.
    if not accounts:
        default_profile = os.path.join(os.path.dirname(__file__), "..", "..", "browser_data")
        accounts = [GeminiAccount(email="default", profile_dir=os.path.abspath(default_profile))]

    redis_client = None
    try:
        from app.cache import _r, _REDIS_OK
        if _REDIS_OK:
            redis_client = _r
    except Exception:
        pass

    _pool = AccountPool(accounts, redis_client=redis_client)
    return _pool
