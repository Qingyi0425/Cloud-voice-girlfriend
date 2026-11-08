@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Missing project Python environment: .venv\Scripts\python.exe
    echo Run the setup instructions in README.md first.
    pause
    exit /b 1
)

echo Starting Xiao Lan...
echo Open http://127.0.0.1:8001 in your browser.
echo Keep this window open while using the service.
echo.

".venv\Scripts\python.exe" -m app
set "exit_code=%errorlevel%"

if not "%exit_code%"=="0" (
    echo.
    echo Xiao Lan stopped with an error. Check server.err.log if available.
    pause
)

exit /b %exit_code%
