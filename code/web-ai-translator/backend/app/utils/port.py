"""Auto-free port trước khi bind — chống lỗi `Address already in use`.

Khi backend khởi động (qua `run.py`, `_serve.py`, `start.bat`, hoặc `launcher.pyw`),
port 8000 có thể vẫn bị giữ bởi:
  - Instance cũ chưa thoát sạch (Ctrl+C giữa chừng).
  - Playwright Chromium child process còn sống.
  - TCP TIME_WAIT sau restart nhanh.

Helper này:
  1. Tìm PID nào đang LISTENING trên port.
  2. Chỉ kill nếu process là `python.exe` / `uvicorn.exe` để tránh giết nhầm
     app khác đang dùng cùng port (vd IIS, dev server khác).
  3. Chờ port release (poll tới timeout).

Idempotent — gọi nhiều lần vô hại. Không raise nếu kill thất bại; chỉ log
để uvicorn vẫn nhận lỗi rõ ràng.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time


# Process names được phép kill (case-insensitive). Mở rộng nếu cần.
_SAFE_TO_KILL = {
    "python.exe", "pythonw.exe", "python",
    "uvicorn.exe", "uvicorn",
}

_IS_WIN = sys.platform == "win32"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if _IS_WIN else 0


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True nếu có process đang LISTENING trên `port`."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _list_pids_owning_port(port: int) -> list[int]:
    """Liệt PID đang LISTENING trên port. Trả [] nếu không tìm thấy."""
    if _IS_WIN:
        ps_cmd = (
            f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
            f"-ErrorAction SilentlyContinue).OwningProcess | Sort-Object -Unique"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=5,
                creationflags=_NO_WINDOW,
            )
            return [int(p) for p in result.stdout.split() if p.strip().isdigit()]
        except Exception:
            return []

    # Linux/Mac: ưu tiên lsof, fallback ss
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3,
        )
        return [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    except Exception:
        return []


def _process_name(pid: int) -> str:
    """Tên process từ PID, lowercase. Rỗng nếu không lookup được."""
    if _IS_WIN:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=3,
                creationflags=_NO_WINDOW,
            )
            # CSV: "python.exe","1234",...
            first = result.stdout.strip().split(",", 1)[0].strip().strip('"')
            return first.lower()
        except Exception:
            return ""

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip().lower()
    except Exception:
        return ""


def _kill_pid(pid: int) -> bool:
    """Force kill PID + cây con. True nếu lệnh chạy không lỗi."""
    try:
        if _IS_WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
                creationflags=_NO_WINDOW,
            )
        else:
            subprocess.run(
                ["kill", "-9", str(pid)],
                capture_output=True, timeout=5,
            )
        return True
    except Exception:
        return False


def _wait_port_free(port: int, timeout: float) -> bool:
    """Poll cho đến khi port free hoặc hết timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_port_in_use(port):
            return True
        time.sleep(0.25)
    return not is_port_in_use(port)


def free_port(port: int, timeout: float = 5.0, force: bool = False) -> bool:
    """Kill mọi Python/uvicorn process giữ `port`. Trả True nếu port đã free khi xong.

    Args:
      port:    cổng cần giải phóng.
      timeout: thời gian tối đa chờ port release sau khi kill (giây).
      force:   nếu True, kill cả process không phải Python (chỉ dùng trong launcher
               nơi user đã biết rõ port này thuộc về app).

    Behavior:
      - Port đang free → trả True ngay.
      - Tìm PID owning port; nếu không tìm thấy nhưng port vẫn busy → chờ TIME_WAIT.
      - Process là python/uvicorn (hoặc force=True) → kill + chờ release.
      - Process lạ → in cảnh báo + skip (uvicorn sau đó báo lỗi rõ hơn).
    """
    if not is_port_in_use(port):
        return True

    pids = _list_pids_owning_port(port)
    if not pids:
        # Port busy nhưng không có owner — TIME_WAIT từ socket vừa đóng
        return _wait_port_free(port, timeout)

    killed_any = False
    for pid in pids:
        name = _process_name(pid)
        if not force and name and name not in _SAFE_TO_KILL:
            print(
                f"[port] Port {port} đang bị {name} (PID {pid}) chiếm — "
                f"không phải Python/uvicorn nên bỏ qua. "
                f"Đóng app đó hoặc gọi free_port(force=True).",
                file=sys.stderr,
            )
            continue
        if _kill_pid(pid):
            killed_any = True
            print(f"[port] Đã kill {name or 'PID'} {pid} đang giữ port {port}.")

    if not killed_any:
        return False
    return _wait_port_free(port, timeout)


def ensure_port_free(port: int, timeout: float = 5.0, force: bool = False) -> None:
    """Wrapper idempotent — gọi đầu mỗi entry point trước khi bind.

    Không raise nếu kill thất bại; chỉ log để uvicorn vẫn nhận lỗi rõ ràng
    (nguyên tắc: helper là best-effort, không được block startup).
    """
    try:
        if is_port_in_use(port):
            free_port(port, timeout=timeout, force=force)
    except Exception as e:
        print(f"[port] ensure_port_free({port}) failed: {e}", file=sys.stderr)
