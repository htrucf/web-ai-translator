"""Adaptive multi-signal scheduler.

Each candidate gets a score in [0, 1]; the highest score wins. The score is a
weighted sum of four normalised signals that the user picked:

  1. recent success rate (sliding window in ``AccountStats.recent``)
  2. recent latency (lower is better — captures degraded accounts)
  3. cooldown frequency (fewer cooldowns is better — captures fragile ones)
  4. idle time / time-since-last-use (longer is better — gives accounts time
     to recover from invisible throttling)

The weights are tunable via env vars (``ADAPTIVE_W_SUCCESS`` etc.) so the
benchmark can sweep them. Defaults are conservative: success rate dominates,
the other three each contribute a smaller correction.

Untried accounts get an optimistic prior (score ≈ 0.85) so they're explored
before the scheduler commits to its favourites. This is a simple ε-greedy-ish
exploration that prevents lockout when one account happens to win early.
"""

from __future__ import annotations

import math
import os
import time
from typing import Optional

from app.pools.schedulers.base import Scheduler, SchedulerContext


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


W_SUCCESS = _envf("ADAPTIVE_W_SUCCESS", 0.45)
W_LATENCY = _envf("ADAPTIVE_W_LATENCY", 0.15)
W_COOLDOWN = _envf("ADAPTIVE_W_COOLDOWN", 0.20)
W_IDLE = _envf("ADAPTIVE_W_IDLE", 0.20)

# Normalisation references — tuned to typical Gemini-web behaviour.
LATENCY_REF = _envf("ADAPTIVE_LATENCY_REF", 20.0)   # seconds; >20s = degraded
IDLE_REF = _envf("ADAPTIVE_IDLE_REF", 600.0)        # seconds; 10min = fully cooled

UNTRIED_PRIOR = 0.85


class AdaptiveScheduler:
    name: str = "adaptive"

    def pick(self, ctx: SchedulerContext) -> Optional[str]:
        if not ctx.free:
            return None
        scored = [(self._score(email, ctx), email) for email in ctx.free]
        # Highest score wins; deterministic tie-break by email.
        scored.sort(key=lambda t: (-t[0], t[1]))
        return scored[0][1]

    def explain(self, email: str, ctx: SchedulerContext) -> dict:
        """Return the score breakdown — used by the admin panel for transparency."""
        return self._components(email, ctx)

    def _score(self, email: str, ctx: SchedulerContext) -> float:
        c = self._components(email, ctx)
        return (
            W_SUCCESS * c["success"]
            + W_LATENCY * c["latency"]
            + W_COOLDOWN * c["cooldown"]
            + W_IDLE * c["idle"]
        )

    def _components(self, email: str, ctx: SchedulerContext) -> dict:
        st = ctx.stats.get(email)
        if st is None or (st.success == 0 and st.fail == 0 and st.cooldowns == 0):
            # Untried — flat optimistic prior on every signal so the score
            # equals UNTRIED_PRIOR regardless of weights.
            return {
                "success": UNTRIED_PRIOR,
                "latency": UNTRIED_PRIOR,
                "cooldown": UNTRIED_PRIOR,
                "idle": UNTRIED_PRIOR,
            }

        # 1. Success rate from the rolling window (1.0 if window is empty).
        success_signal = st.recent_success_rate()

        # 2. Latency: 1.0 when latency == 0, decays smoothly to 0 as it grows
        #    past LATENCY_REF. exp(-x/ref) is monotonic and bounded.
        lat = st.avg_latency or st.last_latency
        latency_signal = math.exp(-lat / LATENCY_REF) if lat > 0 else UNTRIED_PRIOR

        # 3. Cooldown frequency: penalise accounts that have triggered many
        #    cooldowns relative to their total uses. Pure cooldowns count /
        #    total attempts, inverted.
        total = st.success + st.fail
        cd_rate = (st.cooldowns / total) if total > 0 else 0.0
        cooldown_signal = max(0.0, 1.0 - 2.0 * cd_rate)   # 50% cd-rate → 0

        # 4. Idle time since last use. Longer idle = closer to 1, capped.
        idle = (ctx.now - st.last_used_ts) if st.last_used_ts > 0 else IDLE_REF
        idle_signal = min(1.0, idle / IDLE_REF)

        return {
            "success": success_signal,
            "latency": latency_signal,
            "cooldown": cooldown_signal,
            "idle": idle_signal,
        }
