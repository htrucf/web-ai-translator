"""Scheduler interface.

Every concrete scheduler implements ``pick`` — given a list of candidate
emails (already filtered to *free* accounts) plus historical context, return
the email that should be leased next, or ``None`` if no candidate is suitable
yet.

Schedulers are stateless on their own (except for tiny in-memory cursors like
the round-robin index); persistent signals come from ``AccountHistory``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Protocol

from app.pools.account_history import AccountStats


@dataclass
class SchedulerContext:
    """Snapshot passed to ``Scheduler.pick`` at each acquire attempt.

    `free`: accounts currently in state == "free" (already filtered).
    `stats`: per-account history, keyed by email. May contain entries for
             accounts that aren't currently free.
    `now`: wall-clock time at the moment of decision.
    """

    free: list[str]
    stats: dict[str, AccountStats]
    now: float

    @classmethod
    def make(cls, free: list[str], stats: dict[str, AccountStats]) -> "SchedulerContext":
        return cls(free=list(free), stats=stats, now=time.time())


class Scheduler(Protocol):
    """All concrete schedulers must implement this single method.

    Implementations may store a small amount of in-memory state (e.g. a
    round-robin index). They MUST NOT touch Redis directly — the pool does
    that.
    """

    name: str

    def pick(self, ctx: SchedulerContext) -> Optional[str]:
        ...
