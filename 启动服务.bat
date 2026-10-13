@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   workbuddy2api 一键启动
echo ================================
echo.
echo 管理后台: http://127.0.0.1:8790/admin
echo 登录: admin / Wb2api!Test2026
echo.
echo 启动后可关闭本窗口，服务在后台运行。
echo 关闭服务请运行「停止服务.bat」
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" %*

echo.
echo 服务已启动（后台运行）。
pause
