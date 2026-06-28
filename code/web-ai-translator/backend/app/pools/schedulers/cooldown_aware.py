"""Cooldown-aware rotation.

Round-robin variant that biases against accounts which exited cooldown
recently. Even after a cooldown's TTL expires, the account is still likely to
trip the rate-limit again if hit immediately — so we prefer peers that
haven't been throttled lately.

Heuristic: among the free candidates, pick the one whose ``last_cooldown_ts``
is oldest (i.e. it has been "clean" longest). Untried accounts have ts == 0
and naturally sort first, which is the right behaviour (let them prove
themselves).
"""

from __future__ import annotations

from typing import Optional

from app.pools.schedulers.base import Scheduler, SchedulerContext


class CooldownAwareScheduler:
    name: str = "cooldown_aware"

    def pick(self, ctx: SchedulerContext) -> Optional[str]:
        if not ctx.free:
            return None

        def key(email: str) -> tuple[float, str]:
            st = ctx.stats.get(email)
            last_cd = st.last_cooldown_ts if st else 0.0
            # Tie-break by email so the ordering is deterministic in tests.
            return (last_cd, email)

        return min(ctx.free, key=key)
