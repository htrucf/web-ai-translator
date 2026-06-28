"""Web AI Translator — launcher.pyw

Double-click để chạy. Luồng hoạt động:
  1. Mở cửa sổ điều khiển (tkinter)
  2. Tìm chrome.exe → launch Chrome translator với profile RIÊNG
     (browser_data/chrome_cdp_profile) + --remote-debugging-port=9222.
     Profile riêng tránh xung đột với Chrome thường: 2 Chrome chạy song song được.
     Lần đầu user phải login Gemini trong Chrome translator; cookie lưu xuống đĩa
     nên các lần sau Chrome tự nhận session, không cần login lại.
  3. Tự động kill process cũ trên port 8000 → khởi động backend → chờ healthy
  4. Tự mở trình duyệt tới http://localhost:8000
  5. Nút Tắt = dừng backend + đóng Chrome translator. Nút Bật = khởi động lại.
  6. Đóng cửa sổ launcher = tắt backend + Chrome translator rồi thoát.

KHÔNG đụng Chrome thường của user — Chrome thường vẫn chạy bình thường.
"""

import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

# ── Đường dẫn ────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
LOG_DIR     = os.path.join(BACKEND_DIR, "logs")
PORT        = 8000
APP_URL     = f"http://localhost:{PORT}"
HEALTH_URL  = f"{APP_URL}/health"
CDP_PORT    = 9222
# Profile thật của user — chứa cookies, extensions, lịch sử, accounts đã đăng nhập.
# Chỉ dùng để TÌM chrome.exe + (cũ) lấy last_used profile. KHÔNG launch Chrome lên
# profile này nữa vì Chrome không cho 2 instance cùng user-data-dir → nếu Chrome
# thường đang chạy, instance CDP bị reject im lặng.
CHROME_USER_DATA = os.path.expandvars(r"%LocalAppData%\Google\Chrome\User Data")
EDGE_USER_DATA   = os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\User Data")
# Profile riêng cho CDP — tách biệt hoàn toàn với Chrome thường của user.
# Lần đầu dùng phải login Gemini/ChatGPT một lần, các lần sau cookie persist trên đĩa
# (Default/Cookies SQLite) nên Chrome tự nhận session, không cần login lại.
CHROME_CDP_PROFILE = os.path.join(BACKEND_DIR, "browser_data", "chrome_cdp_profile")
# Fallback cũ — giữ lại tên cho tương thích ngược (1 vài chỗ tham chiếu).
CHROME_DATA_FALLBACK = CHROME_CDP_PROFILE
RUNTIME_SETTINGS = os.path.join(BACKEND_DIR, "runtime_settings.json")

os.makedirs(LOG_DIR, exist_ok=True)

# Ẩn cửa sổ console cho subprocess trên Windows
_NO_WINDOW = subprocess.CREATE_NO_WINDOW

# Import helper port-cleanup từ backend (dùng chung với run.py / _serve.py)
sys.path.insert(0, BACKEND_DIR)
try:
    from app.utils.port import ensure_port_free as _ensure_port_free
except Exception:
    _ensure_port_free = None


# ── Tìm virtual environment ───────────────────────────────────────────────────
def _find_venv():
    for name in ("venv", "venv312"):
        py = os.path.join(BACKEND_DIR, name, "Scripts", "python.exe")
        uv = os.path.join(BACKEND_DIR, name, "Scripts", "uvicorn.exe")
        if os.path.exists(py):
            return py, (uv if os.path.exists(uv) else None)
    return None, None


VENV_PYTHON, VENV_UVICORN = _find_venv()


# ── Chrome CDP ────────────────────────────────────────────────────────────────

def _find_browser() -> tuple[str, str] | None:
    """Tìm browser tốt nhất + thư mục profile THẬT của user.

    Trả về (browser_exe_path, user_data_dir) hoặc None.
    Ưu tiên Chrome (vì user dùng profile Chrome → có sẵn Gemini/ChatGPT Pro logged in).
    """
    chrome_paths = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in chrome_paths:
        if os.path.exists(path):
            return path, CHROME_USER_DATA

    edge_paths = [
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in edge_paths:
        if os.path.exists(path):
            return path, EDGE_USER_DATA

    return None


# ── Chrome shortcut setup ─────────────────────────────────────────────────────

def _real_desktop_path() -> str:
    """Lấy đường dẫn Desktop thật, kể cả khi bị OneDrive redirect.

    %USERPROFILE%\\Desktop có thể KHÔNG tồn tại nếu user dùng OneDrive backup —
    Windows redirect Desktop sang %USERPROFILE%\\OneDrive\\Desktop.
    Hỏi shell qua Environment.GetFolderPath để chắc.
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_WINDOW,
        )
        path = result.stdout.strip()
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return os.path.expandvars(r"%USERPROFILE%\Desktop")


def setup_chrome_shortcut() -> dict:
    """Tạo shortcut "Google Chrome (Translator).lnk" trên Desktop trỏ tới Chrome
    với profile RIÊNG (browser_data/chrome_cdp_profile) + CDP flag.

    Không scan/sửa shortcut Chrome khác của user nữa — flow cũ thêm flag vào
    shortcut Chrome thường gây bối rối: nếu Chrome thường đang chạy, instance
    mới bị reject im lặng → flag bị vứt đi. Profile riêng tránh xung đột.

    Trả về dict: {created, desktop_shortcut}.
    Giữ các key cũ (modified/already/failed/modified_paths) = 0/[] để UI tương thích.
    """
    found = _find_browser()
    if not found:
        return {"created": False, "modified": 0, "already": 0, "failed": 0,
                "desktop_shortcut": None, "modified_paths": []}
    chrome_exe, _ = found

    desktop = _real_desktop_path()
    new_shortcut = os.path.join(desktop, "Google Chrome (Translator).lnk")

    # Đảm bảo profile riêng tồn tại trước (để Chrome không phàn nàn lần đầu)
    os.makedirs(CHROME_CDP_PROFILE, exist_ok=True)

    ps_script = (
        "$WshShell = New-Object -ComObject WScript.Shell\n"
        "$created = $false\n"
        "try {\n"
        f"    $sc = $WshShell.CreateShortcut('{new_shortcut}')\n"
        f"    $sc.TargetPath = '{chrome_exe}'\n"
        f"    $sc.Arguments = '--remote-debugging-port={CDP_PORT} --remote-allow-origins=* --user-data-dir=\"{CHROME_CDP_PROFILE}\" --no-first-run --no-default-browser-check'\n"
        f"    $sc.IconLocation = '{chrome_exe},0'\n"
        "    $sc.WorkingDirectory = Split-Path $sc.TargetPath\n"
        "    $sc.Save()\n"
        "    $created = $true\n"
        "} catch {}\n"
        "Write-Output \"CREATED:$created\"\n"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
        created = False
        for line in result.stdout.splitlines():
            if line.strip().startswith("CREATED:"):
                created = line.strip().split(":", 1)[1].lower() == "true"
                break
        return {
            "created": created,
            "modified": 0, "already": 0, "failed": 0,
            "desktop_shortcut": new_shortcut if created else None,
            "modified_paths": [],
        }
    except Exception as e:
        print(f"[Launcher] Setup shortcut failed: {e}")
        return {"created": False, "modified": 0, "already": 0, "failed": 0,
                "desktop_shortcut": None, "modified_paths": []}


def _cdp_ready() -> bool:
    """Kiểm tra Chrome CDP đã lắng nghe trên port chưa."""
    try:
        urllib.request.urlopen(
            f"http://localhost:{CDP_PORT}/json/version", timeout=1
        )
        return True
    except Exception:
        return False


def _set_translator_mode(mode: str):
    """Ghi translator_mode vào runtime_settings.json để backend đọc."""
    try:
        try:
            with open(RUNTIME_SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data["translator_mode"] = mode
        with open(RUNTIME_SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


_chrome_proc = None


def start_chrome() -> tuple[str, str]:
    """Mở Chrome instance riêng cho CDP (không đụng Chrome thường của user).

    Statuses:
      "ready"      — CDP đã sẵn sàng (port 9222 đang lắng nghe)
      "launched"   — Vừa launch Chrome CDP với profile riêng, CDP ready
      "no_browser" — Không tìm thấy Chrome/Edge trên máy
      "timeout"    — Đã launch Chrome nhưng CDP không phản hồi sau 15s

    Lý do dùng profile riêng (browser_data/chrome_cdp_profile/):
      - Chrome refuse 2 instance cùng `user-data-dir` → nếu launch lên profile
        thật khi user đang mở Chrome, instance mới bị reject im lặng, flag
        --remote-debugging-port bị vứt đi → CDP không bao giờ bật.
      - Profile riêng tách biệt → 2 Chrome chạy song song không xung đột.
      - Cookie persist trên đĩa (Default/Cookies SQLite) → login Gemini 1 lần,
        các lần sau Chrome tự nhận session.
    """
    global _chrome_proc

    # 1. CDP đã sẵn sàng (Chrome CDP đã chạy từ trước, hoặc shortcut Translator)
    if _cdp_ready():
        return ("ready", "Chrome CDP đã sẵn sàng")

    # 2. Tìm chrome.exe (chỉ cần đường dẫn exe, không cần profile thật)
    found = _find_browser()
    if not found:
        return ("no_browser", "Không tìm thấy Chrome/Edge trên máy")
    chrome_exe, _ = found

    # 3. Đảm bảo profile CDP tồn tại
    os.makedirs(CHROME_CDP_PROFILE, exist_ok=True)

    # 4. Launch Chrome với profile RIÊNG + CDP.
    # Không dùng --profile-directory: profile riêng chỉ có "Default", không có
    # picker, không cần skip. Không có --enable-automation → không banner bot,
    # Cloudflare không detect.
    _chrome_proc = subprocess.Popen(
        [
            chrome_exe,
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={CHROME_CDP_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )

    # Chờ CDP sẵn sàng (tối đa 15s)
    for _ in range(30):
        time.sleep(0.5)
        if _cdp_ready():
            return ("launched", "Đã mở Chrome CDP (profile riêng)")

    return ("timeout", "Chrome đã mở nhưng CDP không phản hồi sau 15s")


def stop_chrome():
    global _chrome_proc
    if _chrome_proc and _chrome_proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(_chrome_proc.pid)],
                capture_output=True, timeout=5,
                creationflags=_NO_WINDOW,
            )
        except Exception:
            try:
                _chrome_proc.kill()
            except Exception:
                pass
    _chrome_proc = None


# ── Kiểm tra trạng thái ──────────────────────────────────────────────────────
def _port_in_use() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("localhost", PORT)) == 0


def _backend_healthy() -> bool:
    try:
        urllib.request.urlopen(HEALTH_URL, timeout=2)
        return True
    except Exception:
        return False


# ── Kill process trên port ────────────────────────────────────────────────────
def _kill_port(port: int):
    """Kill tất cả process Python/uvicorn LISTENING trên port.

    Delegate sang `app.utils.port.ensure_port_free` (force=True vì launcher biết
    rõ port này thuộc app). Fallback PowerShell nếu helper không import được.
    """
    if _ensure_port_free is not None:
        try:
            _ensure_port_free(port, timeout=5.0, force=True)
            return
        except Exception:
            pass

    # Fallback: PowerShell trực tiếp (giữ hành vi cũ nếu helper lỗi import)
    script = (
        f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
        f"-ErrorAction SilentlyContinue).OwningProcess | "
        f"Sort-Object -Unique | ForEach-Object {{ "
        f"taskkill /F /T /PID $_ 2>$null | Out-Null }}"
    )
    try:
        subprocess.run(
            ["powershell", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        pass


# ── Process manager ───────────────────────────────────────────────────────────
_proc = None
_log_handle = None


def _open_log():
    global _log_handle
    if _log_handle:
        try:
            _log_handle.close()
        except Exception:
            pass
    _log_handle = open(
        os.path.join(LOG_DIR, "backend.log"), "a", encoding="utf-8", buffering=1
    )
    return _log_handle


def start_backend():
    """Kill cũ → start uvicorn mới. Trả về Popen hoặc None."""
    global _proc

    _kill_port(PORT)
    time.sleep(1)

    if not VENV_PYTHON:
        return None

    cmd = (
        [VENV_UVICORN, "app.main:app", "--host", "0.0.0.0", "--port", str(PORT)]
        if VENV_UVICORN
        else [VENV_PYTHON, "-m", "uvicorn", "app.main:app",
              "--host", "0.0.0.0", "--port", str(PORT)]
    )

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    log = _open_log()
    log.write(
        f"\n{'='*60}\n"
        f"[Launcher] Starting at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'='*60}\n"
    )

    _proc = subprocess.Popen(
        cmd,
        cwd=BACKEND_DIR,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | _NO_WINDOW,
    )
    return _proc


def stop_backend():
    """Dừng backend đang chạy và giải phóng port."""
    global _proc
    if _proc and _proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(_proc.pid)],
                capture_output=True, timeout=8,
                creationflags=_NO_WINDOW,
            )
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None
    _kill_port(PORT)


atexit.register(stop_backend)
atexit.register(stop_chrome)


# ── Giao diện ─────────────────────────────────────────────────────────────────
def run_ui():
    import tkinter as tk
    from tkinter import font as tkfont

    BG       = "#1e1e2e"
    SURFACE  = "#313244"
    FG       = "#cdd6f4"
    FG_DIM   = "#a6adc8"
    GREEN    = "#a6e3a1"
    RED      = "#f38ba8"
    YELLOW   = "#f9e2af"
    BLUE     = "#89b4fa"
    LAVENDER = "#b4befe"

    root = tk.Tk()
    root.title("Web AI Translator")
    root.resizable(False, False)
    root.configure(bg=BG)

    W, H = 380, 360
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")
    root.attributes("-topmost", True)

    f_title  = tkfont.Font(family="Segoe UI", size=14, weight="bold")
    f_sub    = tkfont.Font(family="Segoe UI", size=9)
    f_label  = tkfont.Font(family="Segoe UI", size=10)
    f_status = tkfont.Font(family="Segoe UI", size=10)
    f_btn    = tkfont.Font(family="Segoe UI", size=10, weight="bold")

    # ── Tiêu đề ──────────────────────────────────────────────────────────────
    tk.Label(root, text="Web AI Translator", font=f_title,
             bg=BG, fg=LAVENDER).pack(pady=(20, 2))
    tk.Label(root, text="Dich thuat tai lieu hoc thuat tu dong",
             font=f_sub, bg=BG, fg=FG_DIM).pack()

    tk.Frame(root, height=1, bg=SURFACE).pack(fill="x", padx=24, pady=12)

    # ── Status panel ──────────────────────────────────────────────────────────
    sp = tk.Frame(root, bg=BG)
    sp.pack(padx=28, fill="x")

    # Chrome row
    ch_dot = tk.Label(sp, text="●", font=f_status, bg=BG, fg=FG_DIM)
    ch_dot.grid(row=0, column=0, sticky="w", padx=(0, 6))
    tk.Label(sp, text="Chrome", font=f_label, bg=BG, fg=FG_DIM,
             width=12, anchor="w").grid(row=0, column=1, sticky="w")
    ch_lbl = tk.Label(sp, text="--", font=f_status, bg=BG, fg=FG_DIM)
    ch_lbl.grid(row=0, column=2, sticky="w")

    # Backend row
    be_dot = tk.Label(sp, text="●", font=f_status, bg=BG, fg=FG_DIM)
    be_dot.grid(row=1, column=0, sticky="w", padx=(0, 6))
    tk.Label(sp, text="Backend", font=f_label, bg=BG, fg=FG_DIM,
             width=12, anchor="w").grid(row=1, column=1, sticky="w")
    be_lbl = tk.Label(sp, text="--", font=f_status, bg=BG, fg=FG_DIM)
    be_lbl.grid(row=1, column=2, sticky="w")

    # URL row
    url_dot = tk.Label(sp, text="●", font=f_status, bg=BG, fg=FG_DIM)
    url_dot.grid(row=2, column=0, sticky="w", padx=(0, 6))
    tk.Label(sp, text="URL", font=f_label, bg=BG, fg=FG_DIM,
             width=12, anchor="w").grid(row=2, column=1, sticky="w")
    tk.Label(sp, text=APP_URL, font=f_status, bg=BG, fg=FG_DIM).grid(
        row=2, column=2, sticky="w")

    tk.Frame(root, height=1, bg=SURFACE).pack(fill="x", padx=24, pady=12)

    # ── Buttons ───────────────────────────────────────────────────────────────
    bf = tk.Frame(root, bg=BG)
    bf.pack(pady=4)

    start_btn = tk.Button(bf, text="Bat", font=f_btn, width=8,
                          bg="#40a02b", fg="#fff", activebackground="#2d7a1f",
                          relief="flat", padx=10, pady=6, cursor="hand2")
    start_btn.pack(side="left", padx=6)

    stop_btn = tk.Button(bf, text="Tat", font=f_btn, width=8,
                         bg="#d20f39", fg="#fff", activebackground="#a00c2c",
                         relief="flat", padx=10, pady=6, cursor="hand2")
    stop_btn.pack(side="left", padx=6)

    open_btn = tk.Button(bf, text="Mo web", font=f_btn, width=8,
                         bg=BLUE, fg=BG, activebackground="#6fa3e8",
                         relief="flat", padx=10, pady=6, cursor="hand2",
                         command=lambda: webbrowser.open(APP_URL))
    open_btn.pack(side="left", padx=6)

    # Setup row — thêm flag CDP vào shortcut Chrome (one-time)
    setup_frame = tk.Frame(root, bg=BG)
    setup_frame.pack(pady=(4, 0))

    def _do_setup_chrome():
        from tkinter import messagebox
        r = setup_chrome_shortcut()
        if not r["created"] and r["modified"] == 0 and r["already"] == 0:
            messagebox.showerror(
                "Cai dat Chrome",
                "Khong tim thay Chrome tren may, hoac khong tao duoc shortcut.\n"
                "Hay cai Chrome roi thu lai.",
            )
            return

        lines = []
        if r["created"]:
            lines.append(
                f"✓ Da tao shortcut moi tren Desktop:\n"
                f"   {os.path.basename(r['desktop_shortcut'])}\n"
                f"   → Mo qua shortcut nay de inspect/login Chrome translator."
            )

        msg = "\n\n".join(lines)
        msg += "\n\n──────────────\nCach dung:"
        msg += "\n1. Bam Bat trong launcher → tu mo Chrome translator (profile rieng) + CDP."
        msg += "\n   Khong can dong Chrome thuong cua ban — 2 Chrome chay song song duoc."
        msg += "\n2. Lan dau: login Gemini/ChatGPT trong cua so Chrome translator."
        msg += "\n   Cookie luu lai → tu lan sau khong can login nua."
        msg += "\n3. Dong cua so launcher = dong Chrome translator + backend."
        messagebox.showinfo("Cai dat Chrome", msg)

    setup_btn = tk.Button(
        setup_frame, text="Cai dat Chrome shortcut", font=f_sub,
        bg=SURFACE, fg=FG, activebackground="#45475a",
        relief="flat", padx=12, pady=4, cursor="hand2",
        command=lambda: threading.Thread(target=_do_setup_chrome, daemon=True).start(),
    )
    setup_btn.pack()

    tk.Label(root, text=os.path.join(LOG_DIR, "backend.log"),
             font=f_sub, bg=BG, fg="#585b70", wraplength=W - 20).pack(pady=(8, 4))

    # ── State machine ─────────────────────────────────────────────────────────
    _state = {"v": "stopped"}

    def _ui(fn):
        try:
            root.after(0, fn)
        except Exception:
            pass

    def set_chrome_status(status: str, msg: str = ""):
        """status: 'checking' | 'ready' | 'launched' | 'no_browser' | 'timeout' | 'idle'"""
        def _apply():
            if status == "checking":
                ch_dot.config(fg=YELLOW); ch_lbl.config(text="Dang mo Chrome...", fg=YELLOW)
            elif status in ("ready", "launched"):
                ch_dot.config(fg=GREEN)
                ch_lbl.config(
                    text="San sang (profile rieng)" if status == "launched" else "San sang (CDP)",
                    fg=GREEN,
                )
            elif status == "no_browser":
                ch_dot.config(fg=YELLOW)
                ch_lbl.config(text="Khong co Chrome/Edge", fg=YELLOW)
            elif status == "timeout":
                ch_dot.config(fg=YELLOW)
                ch_lbl.config(text="CDP khong phan hoi", fg=YELLOW)
            else:
                ch_dot.config(fg=FG_DIM); ch_lbl.config(text="--", fg=FG_DIM)
        _ui(_apply)

    def set_state(state):
        _state["v"] = state
        def _apply():
            if state == "stopped":
                be_dot.config(fg=RED); be_lbl.config(text="Da dung", fg=RED)
                # Không reset Chrome status — giữ lại thông báo (ví dụ "Dong Chrome roi Bat lai")
                url_dot.config(fg=FG_DIM)
                start_btn.config(state="normal", bg="#40a02b")
                stop_btn.config(state="disabled", bg=SURFACE)
                open_btn.config(state="disabled")
            elif state == "starting":
                be_dot.config(fg=YELLOW); be_lbl.config(text="Dang khoi dong...", fg=YELLOW)
                start_btn.config(state="disabled", bg=SURFACE)
                stop_btn.config(state="normal", bg="#d20f39")
                open_btn.config(state="disabled")
            elif state == "running":
                be_dot.config(fg=GREEN); be_lbl.config(text="San sang", fg=GREEN)
                url_dot.config(fg=GREEN)
                start_btn.config(state="disabled", bg=SURFACE)
                stop_btn.config(state="normal", bg="#d20f39")
                open_btn.config(state="normal")
            elif state == "stopping":
                be_dot.config(fg=YELLOW); be_lbl.config(text="Dang dung...", fg=YELLOW)
                start_btn.config(state="disabled", bg=SURFACE)
                stop_btn.config(state="disabled", bg=SURFACE)
                open_btn.config(state="disabled")
        _ui(_apply)

    # ── Actions ───────────────────────────────────────────────────────────────
    def _do_start():
        set_state("starting")

        # 1) Mở Chrome CDP với profile riêng — không đụng Chrome thường của user
        set_chrome_status("checking")
        status, msg = start_chrome()
        set_chrome_status(status, msg)

        if status in ("ready", "launched"):
            _set_translator_mode("cdp")
        else:
            # no_browser / timeout — fallback: Playwright tự launch (có thể bị detect)
            _set_translator_mode("new_browser")

        # 2) Start backend
        proc = start_backend()

        # Chờ healthy tối đa 60s
        for _ in range(60):
            if _state["v"] != "starting":
                return
            if _backend_healthy():
                set_state("running")
                _ui(lambda: webbrowser.open(APP_URL))
                _start_monitor()
                return
            if proc and proc.poll() is not None:
                set_state("stopped")
                _ui(lambda: be_lbl.config(text="Loi khoi dong — xem log", fg=RED))
                return
            time.sleep(1)
        set_state("stopped")
        _ui(lambda: be_lbl.config(text="Timeout — xem log", fg=RED))

    def _do_stop():
        set_state("stopping")
        stop_backend()
        stop_chrome()
        for _ in range(10):
            if not _port_in_use():
                break
            time.sleep(1)
        url_dot.config(fg=FG_DIM)
        set_state("stopped")

    def _start_monitor():
        def _monitor():
            while _state["v"] == "running":
                time.sleep(3)
                if _proc and _proc.poll() is not None:
                    set_state("stopped")
                    _ui(lambda: be_lbl.config(text="Backend crash — xem log", fg=RED))
                    return
                if not _backend_healthy():
                    set_state("stopped")
                    return
        threading.Thread(target=_monitor, daemon=True).start()

    def on_start():
        threading.Thread(target=_do_start, daemon=True).start()

    def on_stop():
        threading.Thread(target=_do_stop, daemon=True).start()

    start_btn.config(command=on_start)
    stop_btn.config(command=on_stop)

    # ── Khởi tạo: tự động bật ────────────────────────────────────────────────
    def _auto_start():
        if _backend_healthy():
            set_state("running")
            if _cdp_ready():
                set_chrome_status("ready")
            _start_monitor()
        else:
            _do_start()

    threading.Thread(target=_auto_start, daemon=True).start()

    # ── Đóng cửa sổ = tắt luôn ───────────────────────────────────────────────
    def _on_close():
        if _state["v"] in ("running", "starting"):
            stop_backend()
            stop_chrome()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not VENV_PYTHON:
        import tkinter as tk
        from tkinter import messagebox
        _r = tk.Tk(); _r.withdraw()
        messagebox.showerror(
            "Web AI Translator",
            "Khong tim thay virtual environment (venv hoac venv312).\n\n"
            "Hay chay setup.bat truoc.",
        )
        _r.destroy()
        sys.exit(1)

    run_ui()
