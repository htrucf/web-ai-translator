"""Proxy rotation pool.

Round-robins through a list of HTTP/SOCKS5 proxy URLs loaded from a file. On
failure (timeout, 4xx auth) workers call ``mark_bad()`` to drop the proxy for
a cooldown period.

For the DATN demo, an empty file is allowed — pipelines then just use the
local egress IP. Production deployments would mount a residential-proxy list
as a Docker secret.
"""

from __future__ import annotations

import logging
import os
import random
import time
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = int(os.getenv("PROXY_COOLDOWN", "900"))   # 15 min


class ProxyPool:
    def __init__(self, proxies: list[str]):
        self._all = list(dict.fromkeys(p.strip() for p in proxies if p.strip()))
        self._lock = Lock()
        # value = epoch-seconds when proxy becomes usable again (0 = healthy)
        self._cooldowns: dict[str, float] = {p: 0.0 for p in self._all}

    def acquire(self) -> Optional[str]:
        """Pick a random healthy proxy. None if list is empty / all on cooldown."""
        with self._lock:
            now = time.time()
            healthy = [p for p, until in self._cooldowns.items() if until <= now]
            if not healthy:
                return None
            return random.choice(healthy)

    def mark_bad(self, proxy: str, reason: str = "") -> None:
        with self._lock:
            self._cooldowns[proxy] = time.time() + COOLDOWN_SECONDS
        logger.warning("ProxyPool: %s cooled down (%s)", proxy, reason)

    def mark_good(self, proxy: str) -> None:
        with self._lock:
            self._cooldowns[proxy] = 0.0

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            healthy = sum(1 for u in self._cooldowns.values() if u <= now)
            return {"total": len(self._all), "healthy": healthy, "cooldown": len(self._all) - healthy}


_pool: ProxyPool | None = None


def get_proxy_pool() -> ProxyPool:
    global _pool
    if _pool is not None:
        return _pool

    proxies: list[str] = []
    path = os.getenv("PROXY_LIST_FILE", "")
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                proxies = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        except Exception as e:
            logger.warning("Could not load proxy list %s: %s", path, e)

    _pool = ProxyPool(proxies)
    return _pool
