@echo off
title workbuddy2api - Console
cd /d "%~dp0"
setlocal

REM 中文菜单文字全部放在 scripts\service-console.ps1 里 —— bat 用 UTF-8 存中文，
REM 在默认的 GBK 控制台里显示出来就是乱码（现有 bat 里的中文都只在 REM 注释里）。
REM 本文件只是入口：定位项目目录 -> 用 PowerShell 打开控制台（启动服务 + 菜单）。
REM 也可带参数，例如：start-console.bat -Port 8790

if not exist "%~dp0scripts\service-console.ps1" (
    echo [ERROR] scripts\service-console.ps1 not found.
    pause
    exit /b 1
)

REM Make sure MySQL is up before the console launches the service -- otherwise the
REM service blocks in its 60s DB retry before binding port 8790 and every start
REM attempt looks like a failure. Failure here is NOT fatal: the console still
REM opens so the user can read logs / fix the DB.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure-mysql.ps1"
if errorlevel 1 (
    echo.
    echo [WARN] MySQL is not available. The console will still open,
    echo        but the admin backend cannot serve requests until MySQL is up.
    echo.
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\service-console.ps1" %*
set RC=%errorlevel%
if not "%RC%"=="0" (
    echo.
    echo Console exited with code %RC%.
    pause
)

endlocal
