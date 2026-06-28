@echo off
cd /d "%~dp0"

echo [Backend] Kiem tra venv312...
if not exist "venv312\Scripts\python.exe" (
    echo [LOI] Chua co venv312. Chay lenh sau de tao:
    echo   python -m uv venv --python 3.12 venv312
    echo   python -m uv pip install --python venv312\Scripts\python.exe -r requirements.txt
    pause
    exit /b 1
)

echo [Backend] Dang khoi dong server voi Python 3.12...
echo [Backend] API: http://localhost:8000
echo [Backend] Docs: http://localhost:8000/docs
echo.

:loop
venv312\Scripts\python.exe -c "from app.utils.port import ensure_port_free; ensure_port_free(8000)"
venv312\Scripts\uvicorn.exe app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8000
echo.
echo [%time%] Backend da dung (exit %errorlevel%). Tu dong khoi dong lai sau 2 giay...
timeout /t 2 /nobreak >nul
goto loop
