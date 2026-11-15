@echo off
cd /d C:\Users\essve\groww-algo
cd .venv\Scripts
call activate
cd ..
timeout /t 2 /nobreak >nul
title Groww Algo Backend
python -u -m uvicorn engine.api:app --host 127.0.0.1 --port 8000 --log-level info > server.log 2>&1 &
echo Backend started PID %%
timeout /t 3 /nobreak >nul
echo View logs: type server.log