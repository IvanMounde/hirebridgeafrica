@echo off
REM HireBridge Africa — Windows Quick Start
REM Run this once on a fresh install.

echo ============================================
echo   HireBridge Africa — Quick Start
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from python.org then retry.
    pause & exit /b 1
)
echo Python: & python --version
echo.

echo Installing dependencies...
pip install -r requirements.txt --quiet
echo.

echo Checking .env file...
if not exist .env (
    copy .env.example .env >nul
    echo Created .env from .env.example
    echo IMPORTANT: Open .env and set your SECRET_KEY and email before continuing.
    echo Press any key once you have edited .env ...
    pause >nul
) else (
    echo .env already exists
)
echo.

echo Initialising database...
python setup.py
if errorlevel 1 (
    echo ERROR: Setup failed. Check the output above.
    pause & exit /b 1
)
echo.

echo ============================================
echo   SETUP COMPLETE
echo ============================================
echo.
echo Login at:  http://localhost:5000/signin
echo Admin at:  http://localhost:5000/admin
echo.
echo Your admin email is set in ADMIN_EMAILS inside .env
echo Your admin password was printed above by setup.py — copy it now, it is shown only once.
echo.

set /p REPLY="Start the app now? (y/n): "
if /i "%REPLY%"=="y" (
    echo.
    echo Starting... press Ctrl+C to stop.
    flask run
) else (
    echo.
    echo To start later: flask run
    pause
)
