"""Round-robin baseline.

Cycles through accounts in a fixed order, ignoring any history signals. Acts
as the no-op control group in the benchmark — if a smarter strategy can't
beat round-robin, it isn't pulling its weight.
"""

from __future__ import annotations

import threading
from typing import Optional

from app.pools.schedulers.base import Scheduler, SchedulerContext


class RoundRobinScheduler:
    name: str = "round_robin"

    def __init__(self):
        self._cursor = 0
        self._lock = threading.Lock()

    def pick(self, ctx: SchedulerContext) -> Optional[str]:
        if not ctx.free:
            return None
        # Sort for determinism — the underlying pool dict ordering is stable
        # but tests benefit from a canonical order.
        candidates = sorted(ctx.free)
        with self._lock:
            email = candidates[self._cursor % len(candidates)]
            self._cursor = (self._cursor + 1) % len(candidates)
        return email
