@echo off
title workbuddy2api - Stop
cd /d "%~dp0"

echo ========================================
echo   workbuddy2api Stop Service
echo ========================================
echo.

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8790" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F
    echo Stopped PID=%%a
)

netstat -ano | findstr ":8790" | findstr "LISTENING" >/dev/null 2>&1
if %errorlevel%==0 (
    echo Failed to stop!
) else (
    echo Service stopped.
)

echo.
pause
