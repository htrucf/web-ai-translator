"""Dev runner with auto-reload for Windows (ProactorEventLoop compatible).

Usage:
    venv312\Scripts\python.exe run.py

Watches app/ for .py changes and automatically restarts the server.
Press Ctrl+C to stop.
"""
import os
import sys
import time
import signal
import subprocess
import threading
from pathlib import Path

WATCH_DIR = Path(__file__).parent / "app"
SERVER_SCRIPT = Path(__file__).parent / "_serve.py"
PYTHON = sys.executable
POLL_INTERVAL = 1.0  # seconds
SERVER_PORT = 8001

# Cho phép import app.utils.port khi chạy `python run.py` từ thư mục backend
sys.path.insert(0, str(Path(__file__).parent))
try:
    from app.utils.port import ensure_port_free
except Exception:
    def ensure_port_free(port: int, timeout: float = 5.0, force: bool = False) -> None:
        pass


def get_mtimes(watch_dir: Path) -> dict:
    mtimes = {}
    for f in watch_dir.rglob("*.py"):
        try:
            mtimes[str(f)] = f.stat().st_mtime
        except OSError:
            pass
    return mtimes


def start_server() -> subprocess.Popen:
    print("\n[run.py] Starting server...", flush=True)
    # Reload nhanh có thể bind trước khi instance cũ release port → kill instance
    # cũ (python/uvicorn) đang giữ port trước khi spawn lại.
    ensure_port_free(SERVER_PORT, timeout=5.0)
    proc = subprocess.Popen(
        [PYTHON, str(SERVER_SCRIPT)],
        cwd=str(Path(__file__).parent),
    )
    return proc


def stop_server(proc: subprocess.Popen):
    if proc and proc.poll() is None:
        print("[run.py] Stopping server...", flush=True)
        if sys.platform == "win32":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    proc = start_server()
    mtimes = get_mtimes(WATCH_DIR)

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # Check if server crashed — restart it
            if proc.poll() is not None:
                print(f"[run.py] Server exited (code {proc.returncode}), restarting...", flush=True)
                time.sleep(1)
                proc = start_server()
                mtimes = get_mtimes(WATCH_DIR)
                continue

            # Check for file changes
            new_mtimes = get_mtimes(WATCH_DIR)
            changed = [f for f, t in new_mtimes.items() if mtimes.get(f) != t]
            if changed:
                for f in changed[:3]:
                    print(f"[run.py] Changed: {Path(f).relative_to(WATCH_DIR.parent)}", flush=True)
                if len(changed) > 3:
                    print(f"[run.py] ...and {len(changed)-3} more files", flush=True)
                stop_server(proc)
                time.sleep(0.5)
                proc = start_server()
                mtimes = new_mtimes

    except KeyboardInterrupt:
        print("\n[run.py] Shutting down...", flush=True)
        stop_server(proc)
        print("[run.py] Done.", flush=True)


if __name__ == "__main__":
    main()
