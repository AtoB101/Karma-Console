@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   KARMA SUPER CONSOLE
echo   启动前先自检，日志写入 logs\ 目录
echo   http://127.0.0.1:8000/
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_server.ps1"
echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
