@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment...
  python -m venv .venv || goto :fail
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip || goto :fail
  echo [2/3] Installing Python dependencies...
  .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :fail
)
echo [3/3] Starting Groww Algo API on http://127.0.0.1:8000
echo       Dashboard: http://127.0.0.1:5173  (start run_web.bat in a second window)
.venv\Scripts\python.exe -m uvicorn engine.api:app --host 127.0.0.1 --port 8000
goto :eof

:fail
echo.
echo Setup failed. Check that Python 3.11+ is on PATH and run this file again.
pause
