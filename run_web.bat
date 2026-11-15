@echo off
setlocal
cd /d "%~dp0\frontend"
if not exist "node_modules" (
  echo Installing frontend dependencies...
  call npm install || goto :fail
)
echo Starting dashboard on http://127.0.0.1:5173
call npm run dev
goto :eof

:fail
echo.
echo npm install failed. Check that Node.js 20+ is on PATH.
pause
