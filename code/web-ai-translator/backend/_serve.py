"""Inner server process — spawned by run.py watcher.
Sets ProactorEventLoop before uvicorn creates its event loop.
"""
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

from app.utils.port import ensure_port_free

PORT = 8001


async def main():
    # Tự kill instance cũ còn giữ port (Ctrl+C giữa chừng, Playwright child sống,
    # TIME_WAIT…) — user không cần biết port là gì.
    ensure_port_free(PORT, timeout=5.0)

    config = uvicorn.Config(
        "app.main:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
