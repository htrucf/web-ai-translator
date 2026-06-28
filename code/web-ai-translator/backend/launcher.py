"""Desktop launcher — single entrypoint for the packaged app.

What this does:
  1. Picks a free TCP port on the loopback interface (avoids the "8000 is
     already in use" failure when another dev server is running).
  2. Starts the FastAPI backend bound to 127.0.0.1 only (no network exposure).
  3. Polls /health until the backend is ready, then opens the user's default
     browser at http://127.0.0.1:PORT/.
  4. Writes startup + uvicorn output to launcher.log inside the OS user-data
     dir so users can attach it when reporting bugs.

Run directly:
    python -m launcher              # from backend/
    python launcher.py              # also fine

PyInstaller will package this as the entrypoint of the desktop binary.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Make sure `app.*` imports work whether we're run from backend/ or frozen.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from app import paths  # noqa: E402  (sys.path tweak above)


def _pick_free_port() -> int:
    """Ask the OS for any free port on the loopback interface.

    There's a tiny race between releasing the socket here and uvicorn
    binding to it — acceptable for a single-user desktop app. If it ever
    fires, the user just relaunches.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_health(port: int, timeout: float = 30.0) -> bool:
    """Poll /health until the backend answers or we give up."""
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.2)
    return False


def _open_browser_when_ready(port: int) -> None:
    """Background thread: wait for /health then launch the default browser."""
    if _wait_for_health(port):
        url = f"http://127.0.0.1:{port}/"
        print(f"[launcher] Backend ready — opening {url}")
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"[launcher] Could not open browser automatically: {e}")
            print(f"[launcher] Open this URL manually: {url}")
    else:
        print("[launcher] Backend did not become ready within 30s — check launcher.log")


class _Tee:
    """Write to two streams at once (real stdout + log file).

    Delegates everything else to the primary stream so libraries that probe
    `isatty`, `fileno`, `encoding`, etc. (uvicorn's color formatter does this)
    don't crash.
    """

    def __init__(self, primary, *others):
        self._primary = primary
        self._streams = (primary, *others)

    def write(self, data: str) -> int:
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._primary, name)


def _setup_logging() -> Path:
    """Tee stdout/stderr to launcher.log inside the OS user-data dir."""
    log_path = Path(paths.user_data_dir()) / "launcher.log"
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    log_file.write(f"\n=== launcher start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)
    return log_path


def main() -> int:
    log_path = _setup_logging()
    print(f"[launcher] Logging to {log_path}")
    print(f"[launcher] User data dir: {paths.user_data_dir()}")
    print(f"[launcher] Workspace:     {paths.workspace_dir()}")

    port = _pick_free_port()
    print(f"[launcher] Selected port: {port}")

    # Spawn the browser-opener before uvicorn.run — uvicorn blocks the main
    # thread, so the watcher needs to live elsewhere.
    threading.Thread(
        target=_open_browser_when_ready, args=(port,), daemon=True
    ).start()

    # Import inside main so any startup errors land in the log we just wired up.
    import uvicorn
    from app.main import app

    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=port,
            log_level="info",
            access_log=False,
        )
    except KeyboardInterrupt:
        print("[launcher] Shutting down (Ctrl+C)")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
