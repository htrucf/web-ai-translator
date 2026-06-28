import time, os, sys, subprocess

time.sleep(1.5)

# Kill uvicorn reloader (parent) — worker dies with it
for pid in [34448, 34480]:
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, -1)
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass

time.sleep(0.5)

# Start new uvicorn in detached mode
subprocess.Popen(
    [r"C:\Users\LENOVO\Downloads\DATN\web-ai-translator\backend\venv312\Scripts\uvicorn.exe", "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"],
    cwd=r"C:\Users\LENOVO\Downloads\DATN\web-ai-translator\backend",
    creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    close_fds=True,
)
