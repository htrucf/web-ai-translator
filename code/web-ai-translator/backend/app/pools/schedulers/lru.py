"""Least-Recently-Used.

Picks the account that has been idle the longest. The intuition is two-fold:
  - Spreading load evenly across accounts maximises the time each one has to
    "recover" from any soft throttling Gemini applies silently.
  - It mimics how a careful human would rotate accounts.

Untried accounts (last_used_ts == 0) sort first, which is what we want.
"""

from __future__ import annotations

from typing import Optional

from app.pools.schedulers.base import Scheduler, SchedulerContext


class LRUScheduler:
    name: str = "lru"

    def pick(self, ctx: SchedulerContext) -> Optional[str]:
        if not ctx.free:
            return None

        def key(email: str) -> tuple[float, str]:
            st = ctx.stats.get(email)
            last_used = st.last_used_ts if st else 0.0
            return (last_used, email)

        return min(ctx.free, key=key)
