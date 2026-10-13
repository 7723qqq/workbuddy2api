@echo off
title workbuddy2api - Start
cd /d "%~dp0"

echo ========================================
echo   workbuddy2api Start Service
echo ========================================
echo.

REM Kill old instance on port 8790
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8790" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >/dev/null 2>&1
)

REM Start service in background
set PYTHONUTF8=1
start "" /B ".venv\Scripts\python.exe" main.py

echo Starting, please wait...
timeout /t 5 /nobreak >/dev/null

REM Check
netstat -ano | findstr ":8790" | findstr "LISTENING" >/dev/null 2>&1
if %errorlevel%==0 (
    echo Service started OK!
) else (
    echo Start FAILED - check logs\daemon.log
)

echo.
echo ========================================
echo   Admin UI : http://127.0.0.1:8790/admin
echo   Username : admin
echo   Password : Wb2api!Test2026
echo ========================================
echo.
echo Service is running in background.
echo Close this window is OK.
echo To stop: double-click stop-service.bat
echo.
pause
