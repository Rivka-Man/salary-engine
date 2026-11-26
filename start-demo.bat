@echo off
rem Demo mode: the chat on a local database (fast, no network). The shared DB is not touched.
cd /d "%~dp0"
chcp 65001 >nul

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

set "DATABASE_URL=local"
set "PORT=5060"
set "PYTHONIOENCODING=utf-8"

"%PY%" -c "import pgserver" 2>nul || "%PY%" -m pip install pgserver

for /f "delims=" %%u in ('"%PY%" demo_local.py') do set "URL=%%u"
echo.
echo  Demo chat: %URL%
echo.
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 5; Start-Process '%URL%'"
"%PY%" app.py
