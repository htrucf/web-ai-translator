@echo off
cd /d "%~dp0frontend"
echo [Build] Cai dat dependencies...
call npm install
echo [Build] Building frontend...
call npm run build
echo [Build] Xong! Frontend da duoc build vao frontend/dist/
pause
