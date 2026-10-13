@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   workbuddy2api 停止服务
echo ================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Stop

echo.
echo 服务已停止。
pause
