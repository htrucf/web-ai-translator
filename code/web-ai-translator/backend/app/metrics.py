"""Prometheus metrics — exposed at ``/metrics`` on every backend replica.

Wired up by ``setup_metrics(app)`` in ``app.main``. We deliberately use the
multi-process collector under gunicorn so each worker doesn't report its own
view of the world (which would produce nonsensical totals).
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from starlette.requests import Request
from starlette.responses import Response


# ── App-level metrics ────────────────────────────────────────────────────────

http_requests_total = Counter(
    "wat_http_requests_total",
    "HTTP requests by method, route, status",
    ["method", "route", "status"],
)

http_request_duration_seconds = Histogram(
    "wat_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# ── Translation job metrics ──────────────────────────────────────────────────

jobs_enqueued_total = Counter(
    "wat_jobs_enqueued_total",
    "Translation jobs enqueued",
    ["source_type"],   # "latex" / "pdf"
)

jobs_completed_total = Counter(
    "wat_jobs_completed_total",
    "Translation jobs that reached a terminal status",
    ["source_type", "status"],   # status: done | done_with_warnings | error | cancelled
)

jobs_in_progress = Gauge(
    "wat_jobs_in_progress",
    "Translation jobs currently running",
    ["source_type"],
    multiprocess_mode="livesum",
)

job_duration_seconds = Histogram(
    "wat_job_duration_seconds",
    "End-to-end job duration",
    ["source_type"],
    buckets=(30, 60, 120, 300, 600, 1200, 1800, 3600, 7200),
)

chunks_translated_total = Counter(
    "wat_chunks_translated_total",
    "Individual chunks translated",
    ["source_type"],
)

chunk_translation_seconds = Histogram(
    "wat_chunk_translation_seconds",
    "Time to translate a single chunk",
    ["source_type"],
    buckets=(1, 2, 5, 10, 20, 30, 60, 120),
)

# ── Bot-detection / pool health ──────────────────────────────────────────────

account_pool_state = Gauge(
    "wat_account_pool_state",
    "Account pool state counters",
    ["state"],   # free | in_use | cooldown
    multiprocess_mode="livesum",
)

# ── Scheduler / multi-account research metrics ───────────────────────────────

scheduler_strategy = Gauge(
    "wat_scheduler_strategy",
    "Currently active account scheduling strategy (1=active, 0=inactive)",
    ["strategy"],   # round_robin | cooldown_aware | lru | adaptive
    multiprocess_mode="livesum",
)

scheduler_acquire_total = Counter(
    "wat_scheduler_acquire_total",
    "Account lease acquisitions by strategy and outcome",
    ["strategy", "outcome"],   # outcome: success | timeout
)

scheduler_chunks_total = Counter(
    "wat_scheduler_chunks_total",
    "Chunks processed per (strategy, account, outcome) — feeds throughput",
    ["strategy", "account", "outcome"],   # outcome: success | fail
)

scheduler_account_alive = Gauge(
    "wat_scheduler_account_alive",
    "1 if the account is currently usable (free or in_use), 0 if cooldown — feeds survival rate",
    ["strategy", "account"],
    multiprocess_mode="livesum",
)

scheduler_job_completed_total = Counter(
    "wat_scheduler_job_completed_total",
    "Jobs that reached a terminal state, by scheduling strategy",
    ["strategy", "status"],   # status: done | error | cancelled
)

scheduler_chunk_latency_seconds = Histogram(
    "wat_scheduler_chunk_latency_seconds",
    "Per-chunk latency by scheduling strategy",
    ["strategy"],
    buckets=(1, 2, 5, 10, 20, 30, 60, 120),
)

proxy_pool_state = Gauge(
    "wat_proxy_pool_state",
    "Proxy pool state counters",
    ["state"],   # healthy | cooldown
    multiprocess_mode="livesum",
)

bot_block_total = Counter(
    "wat_bot_block_total",
    "Times Gemini blocked the worker (CAPTCHA, rate-limit…)",
    ["reason"],
)

# ── VLM / self-healing browser metrics (Contribution 2) ──────────────────────
# These feed the KPIs for the self-healing thesis claim:
#   - vlm_fallback_total     → how often hardcoded CSS broke
#   - selector_learning_total → how often a new selector was learned from VLM
#   - selector_memory_lookup → learned-selector hit rate (CSS-fast recovery)
#   - vlm_call_latency       → cost of the VLM round-trip (Ollama screenshot)

vlm_fallback_total = Counter(
    "wat_vlm_fallback_total",
    "Times the VLM fallback was invoked because CSS selectors failed",
    ["backend", "element_type", "outcome"],   # outcome: found | not_found | error
)

selector_learning_total = Counter(
    "wat_selector_learning_total",
    "New CSS selectors learned by deriving from VLM-located coordinates",
    ["backend", "element_type"],
)

selector_memory_lookup_total = Counter(
    "wat_selector_memory_lookup_total",
    "Learned-selector cache lookups",
    ["backend", "element_type", "outcome"],   # outcome: hit | miss
)

vlm_call_latency_seconds = Histogram(
    "wat_vlm_call_latency_seconds",
    "End-to-end VLM call latency (screenshot + Ollama inference + parse)",
    ["element_type"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60),
)

# ── Cache metrics ────────────────────────────────────────────────────────────

cache_hits_total = Counter("wat_cache_hits_total", "Cache hits", ["bucket"])
cache_misses_total = Counter("wat_cache_misses_total", "Cache misses", ["bucket"])


# ── Setup ───────────────────────────────────────────────────────────────────

def _registry():
    """Multi-process collector if PROMETHEUS_MULTIPROC_DIR is set (gunicorn);
    process-default registry otherwise (single-process uvicorn dev)."""
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        os.makedirs(os.environ["PROMETHEUS_MULTIPROC_DIR"], exist_ok=True)
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry
    from prometheus_client import REGISTRY
    return REGISTRY


def setup_metrics(app: FastAPI) -> None:
    """Install /metrics endpoint + a per-request timing middleware."""
    import time as _time
    from starlette.middleware.base import BaseHTTPMiddleware

    class _MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            start = _time.perf_counter()
            response = await call_next(request)
            duration = _time.perf_counter() - start
            # Coarse route — use the matched path template if available,
            # else the raw path. (Avoids /api/job/<uuid> exploding cardinality.)
            route = request.scope.get("route")
            template = getattr(route, "path", request.url.path) if route else request.url.path
            http_requests_total.labels(
                method=request.method,
                route=template,
                status=str(response.status_code),
            ).inc()
            http_request_duration_seconds.labels(
                method=request.method,
                route=template,
            ).observe(duration)
            return response

    app.add_middleware(_MetricsMiddleware)

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint() -> Response:
        # Refresh pool gauges on scrape (cheap)
        try:
            from app.pools import get_account_pool, get_proxy_pool
            pool = get_account_pool()
            astats = pool.stats()
            pstats = get_proxy_pool().stats()
            account_pool_state.labels(state="free").set(astats["free"])
            account_pool_state.labels(state="in_use").set(astats["in_use"])
            account_pool_state.labels(state="cooldown").set(astats["cooldown"])
            proxy_pool_state.labels(state="healthy").set(pstats["healthy"])
            proxy_pool_state.labels(state="cooldown").set(pstats["cooldown"])

            # Scheduler gauges — refresh active strategy + per-account survival
            strategy = pool.scheduler_name()
            for s in ("round_robin", "cooldown_aware", "lru", "adaptive"):
                scheduler_strategy.labels(strategy=s).set(1 if s == strategy else 0)
            for email in pool.accounts:
                state = pool._state(email)
                scheduler_account_alive.labels(
                    strategy=strategy, account=email
                ).set(0 if state == "cooldown" else 1)
        except Exception:
            pass

        data = generate_latest(_registry())
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)
