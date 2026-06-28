@echo off
cd /d "%~dp0"
echo ============================================
echo  Web AI Translator - Setup
echo ============================================
echo.

echo [1/4] Kiem tra Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [LOI] Python chua duoc cai. Tai tai: https://python.org
    pause & exit /b 1
)

echo [2/4] Tao venv312 cho backend...
cd backend
if not exist "venv312\Scripts\python.exe" (
    python -m uv venv --python 3.12 venv312 2>nul
    if errorlevel 1 (
        python -m venv venv312
    )
)
echo [2/4] Cai dat Python dependencies...
venv312\Scripts\pip install -r requirements.txt --quiet
venv312\Scripts\python -m playwright install chromium
cd ..

echo [3/4] Cai dat frontend dependencies va build...
cd frontend
call npm install --silent
call npm run build
cd ..

echo [4/4] Cai dat pystray (tray icon, optional)...
call "%~dp0backend\venv312\Scripts\pip.exe" install pystray pillow --quiet

echo.
echo ============================================
echo  Setup hoan tat!
echo  Chay: launcher.pyw (double-click)
echo ============================================
pause
