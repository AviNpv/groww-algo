@echo off
cd /d C:\Users\essve\groww-algo
cd .venv\Scripts
call activate
cd ..
timeout /t 2 /nobreak >nul
python.exe -m uvicorn engine.api:app --host 127.0.0.1 --port 8000 --log-level info