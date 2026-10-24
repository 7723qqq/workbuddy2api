@echo off
title workbuddy2api - Start
cd /d "%~dp0"
setlocal

echo ========================================
echo   workbuddy2api Start Service
echo ========================================
echo.

REM ---------------------------------------------------------------------------
REM Ensure MySQL is running BEFORE we touch the service.
REM Why: ADMIN_DATABASE_URL points at a local MySQL that is NOT registered as a
REM Windows service, so it does not come back after a reboot. With MySQL down the
REM app still boots, but uvicorn retries the DB 30 x 2s = 60s BEFORE it binds
REM port 8790 -- so the 11s port check at the bottom reports a false
REM "Start FAILED", and the watchdog keeps spawning duplicate instances meanwhile.
REM Starting MySQL here makes the port come up in about two seconds.
REM ---------------------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure-mysql.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] MySQL is not available - aborting start.
    echo         Start MySQL manually, then run this script again.
    echo.
    pause
    exit /b 1
)
echo.

REM 先停掉旧实例 / 旧守护
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Stop >nul 2>&1

REM -Detach：用 Start-Process -WindowStyle Hidden 把守护进程完全脱离本窗口/会话。
REM 不要用 start /B：那会把服务挂在当前控制台上，关窗口即被回收。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Detach

echo Starting detached daemon + service, please wait...
timeout /t 6 /nobreak >nul

netstat -ano | findstr ":8790" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Service started OK!
) else (
    echo Still starting... checking again in 5s
    timeout /t 5 /nobreak >nul
    netstat -ano | findstr ":8790" | findstr "LISTENING" >nul 2>&1
    if %errorlevel%==0 (
        echo Service started OK!
    ) else (
        echo Start FAILED - check logs\daemon.log and logs\service.err.log
    )
)

echo.
echo ========================================
echo   Admin UI : http://127.0.0.1:8790/admin
echo   Username : admin
echo   Password : Wb2api!Test2026
echo ========================================
echo.
echo Service runs detached. You can close this window now.
echo Watchdog auto-restarts the service if it dies.
echo To stop: double-click stop-service.bat
echo.
pause
