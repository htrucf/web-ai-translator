"""WebSocket fan-out backed by Redis pub/sub.

Architecture:
  - FastAPI accepts WS connections at ``/ws/jobs/{job_id}``.
  - Each connection subscribes to the Redis channel ``job:{job_id}:events``.
  - Celery workers publish progress events to the same channel via
    ``publish_event()``. They never need to know which clients are listening.

This decoupling is what lets the API and worker processes scale independently
(workers don't hold WS connections, API replicas don't hold task state).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

PUBSUB_URL = os.getenv("PUBSUB_REDIS_URL", os.getenv("REDIS_URL", "redis://localhost:6379/4"))

try:
    import redis.asyncio as aioredis
    _async_client = aioredis.from_url(PUBSUB_URL, decode_responses=True)
    _PUBSUB_OK = True
except Exception as e:
    _async_client = None
    _PUBSUB_OK = False
    logger.warning("WS: Redis pub/sub unavailable (%s) — falling back to in-process broadcast", e)


# Sync publisher — used by Celery workers
try:
    import redis as _sync_redis
    _sync_client = _sync_redis.Redis.from_url(PUBSUB_URL, decode_responses=True, socket_timeout=2)
except Exception:
    _sync_client = None


# In-process broadcast (fallback / dev)
_local_subscribers: dict[str, set[asyncio.Queue]] = {}


def _channel(job_id: str) -> str:
    return f"job:{job_id}:events"


def publish_event(job_id: str, event: dict[str, Any]) -> None:
    """Worker-side: push an event to the channel. Safe to call from sync code."""
    payload = json.dumps(event, ensure_ascii=False, default=str)
    if _sync_client is not None:
        try:
            _sync_client.publish(_channel(job_id), payload)
            return
        except Exception as e:
            logger.warning("WS publish failed (%s) — local fanout only", e)

    # Local fallback: hand the message to in-process queues
    queues = _local_subscribers.get(job_id, set())
    for q in list(queues):
        try:
            q.put_nowait(payload)
        except Exception:
            pass


async def stream_job_events(websocket: WebSocket, job_id: str) -> None:
    """API-side: stream events from Redis to the WS client until disconnect."""
    await websocket.accept()

    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
    _local_subscribers.setdefault(job_id, set()).add(queue)

    pubsub_task: asyncio.Task | None = None
    if _PUBSUB_OK and _async_client is not None:
        async def _consume_redis():
            pubsub = _async_client.pubsub()
            await pubsub.subscribe(_channel(job_id))
            try:
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        queue.put_nowait(msg["data"])
                    except asyncio.QueueFull:
                        pass
            finally:
                try:
                    await pubsub.unsubscribe(_channel(job_id))
                    await pubsub.close()
                except Exception:
                    pass

        pubsub_task = asyncio.create_task(_consume_redis())

    try:
        # Send a hello so the client knows the channel is live
        await websocket.send_json({"type": "subscribed", "job_id": job_id})
        while True:
            payload = await queue.get()
            await websocket.send_text(payload)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("WS error (%s): %s", job_id, e)
    finally:
        if pubsub_task:
            pubsub_task.cancel()
        _local_subscribers.get(job_id, set()).discard(queue)


# ── Convenience event helpers ────────────────────────────────────────────────

def emit_progress(job_id: str, status: str, current: int = 0, total: int = 0, **extra) -> None:
    publish_event(job_id, {
        "type": "progress",
        "job_id": job_id,
        "status": status,
        "current": current,
        "total": total,
        **extra,
    })


def emit_done(job_id: str, **extra) -> None:
    publish_event(job_id, {"type": "done", "job_id": job_id, **extra})


def emit_error(job_id: str, error: str, **extra) -> None:
    publish_event(job_id, {"type": "error", "job_id": job_id, "error": error, **extra})
