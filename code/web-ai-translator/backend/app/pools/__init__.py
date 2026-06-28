"""Resource pools — Gemini account rotation, proxy rotation.

Both pools are backed by Redis (atomic check-out / check-in) so that multiple
worker replicas don't race to the same account or burn through a hot proxy.
"""

from app.pools.account_pool import AccountPool, get_account_pool
from app.pools.proxy_pool import ProxyPool, get_proxy_pool

__all__ = ["AccountPool", "ProxyPool", "get_account_pool", "get_proxy_pool"]
