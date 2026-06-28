"""Pluggable account-selection strategies.

The pool exposes a fixed interface (``acquire``/``release``/``cooldown``).
Which *free* account gets handed out next is delegated to a ``Scheduler``
implementation chosen at runtime via ``ACCOUNT_SCHEDULER`` env var or the
admin API.

Strategies:
  - round_robin: classic baseline, no awareness of state
  - cooldown_aware: skip accounts that recently exited cooldown
  - lru: prefer the least-recently-used account (longest idle gap)
  - adaptive: weighted score combining 4 signals (success rate, latency,
    cooldown frequency, idle time)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from app.pools.schedulers.base import Scheduler, SchedulerContext
from app.pools.schedulers.round_robin import RoundRobinScheduler
from app.pools.schedulers.cooldown_aware import CooldownAwareScheduler
from app.pools.schedulers.lru import LRUScheduler
from app.pools.schedulers.adaptive import AdaptiveScheduler

logger = logging.getLogger(__name__)

_REGISTRY = {
    "round_robin": RoundRobinScheduler,
    "cooldown_aware": CooldownAwareScheduler,
    "lru": LRUScheduler,
    "adaptive": AdaptiveScheduler,
}


def list_strategies() -> list[str]:
    return list(_REGISTRY.keys())


def build_scheduler(name: Optional[str] = None) -> Scheduler:
    """Build a scheduler from the registry. Unknown names fall back to round_robin."""
    if name is None:
        name = os.getenv("ACCOUNT_SCHEDULER", "cooldown_aware").lower()
    cls = _REGISTRY.get(name)
    if cls is None:
        logger.warning("Unknown scheduler %r; defaulting to round_robin", name)
        cls = RoundRobinScheduler
        name = "round_robin"
    inst = cls()
    inst.name = name
    return inst


__all__ = [
    "Scheduler",
    "SchedulerContext",
    "RoundRobinScheduler",
    "CooldownAwareScheduler",
    "LRUScheduler",
    "AdaptiveScheduler",
    "build_scheduler",
    "list_strategies",
]
