@echo off
chcp 65001 >nul
cd /d "%~dp0"
title MarkdownToHandwrite 本地工作台
echo ============================================
echo   MarkdownToHandwrite 手写模拟器
echo   启动后浏览器将自动打开:
echo   http://127.0.0.1:8765/
echo.
echo   关闭本窗口即停止服务
echo ============================================
echo.
".venv\Scripts\markdown-to-handwrite-web.exe"
echo.
echo 服务已退出，按任意键关闭窗口
pause >nul