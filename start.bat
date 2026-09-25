@echo off
title IEEE CIS Summer School - Attendance System
color 0A

echo ============================================
echo  IEEE CIS Summer School - Attendance System
echo ============================================
echo.

:: Set PATH to include Python and Git
set "PATH=C:\Users\Hp\AppData\Local\Programs\Python\Python312;C:\Users\Hp\AppData\Local\Programs\Python\Python312\Scripts;C:\Users\Hp\AppData\Local\Programs\Git\cmd;%PATH%"

:: Change to the script's directory
cd /d "%~dp0"

:: Check if .env exists
if not exist ".env" (
    echo [ERROR] .env file not found! Please create it with your SUPABASE_URL and SUPABASE_KEY.
    pause
    exit /b 1
)

:: Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.12+.
    pause
    exit /b 1
)

:: Install/verify requirements
echo [1/3] Checking and installing requirements...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.
    pause
    exit /b 1
)
echo      Requirements OK.
echo.

:: Load participants
echo [2/3] Loading participants into Supabase...
python load_participants.py
if errorlevel 1 (
    echo [WARNING] load_participants.py had an issue. Check your .env SUPABASE_KEY.
    echo           Continuing to start the server anyway...
)
echo.

:: Start server
echo [3/3] Starting the attendance server...
echo.
echo  Access the app at: http://localhost:8000
echo  To stop the server, press Ctrl+C
echo.
python -m uvicorn app:app --host 0.0.0.0 --port 8000

pause
