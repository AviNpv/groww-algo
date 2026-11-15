@echo off
setlocal
cd /d "%~dp0"
echo Starting backend and dashboard in two windows...
start "Groww Algo API" cmd /k run_api.bat
timeout /t 3 >nul
start "Groww Algo Web" cmd /k run_web.bat
timeout /t 4 >nul
start "" http://127.0.0.1:5173
echo.
echo API    : http://127.0.0.1:8000   (docs at /docs)
echo Web    : http://127.0.0.1:5173
echo Close the two console windows to stop everything.
