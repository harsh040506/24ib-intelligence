@echo off
setlocal

rem Always run from this script's own folder, no matter where it's launched from.
cd /d "%~dp0"

echo ============================================
echo   24IB Intelligence Platform
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

echo Checking dependencies...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. See the output above.
    pause
    exit /b 1
)

echo.
echo Starting the server at http://127.0.0.1:5000 ...
echo (Close this window, or press Ctrl+C, to stop the server.)
echo.

rem Open the browser a couple seconds after launch, once the server is up.
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:5000"

python run.py

echo.
echo Server stopped.
pause
