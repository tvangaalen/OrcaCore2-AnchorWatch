@echo off
setlocal
title Anchor Watch HTTPS Web Server
cd /d "%~dp0"

echo Preparing the HTTPS certificate (issued by the Mastervolt Local CA)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_local_https.ps1"
if errorlevel 1 (
  echo HTTPS certificate setup failed.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the Python environment...
  py -m venv .venv && ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)

echo.
set /p ANCHOR_BIND=<"%~dp0certs\bind-address.txt"
echo Starting Anchor Watch HTTPS web server...
echo Private network address: https://%ANCHOR_BIND%:8766
echo The application binds only to this private-LAN address, never to a public interface.
echo.
echo Press Ctrl+C to stop the server.
echo.
start "" "https://%ANCHOR_BIND%:8766/"

".venv\Scripts\python.exe" anchor_watch.py --bind %ANCHOR_BIND% --port 8766 --certfile "%~dp0certs\server-cert.pem" --keyfile "%~dp0certs\server-key.pem" %*

echo.
echo Web server stopped.
pause
