@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Markdown 转手写 PDF
if "%~1"=="" (
  echo 用法：把 .md 文件拖放到本脚本图标上，即可生成手写风格 PDF
  echo 生成位置: output\pdf\ 目录
  echo.
  pause
  exit /b
)
echo 正在生成: %~nx1
".venv\Scripts\markdown-to-handwrite.exe" "%~1" -o "output\pdf\%~n1.pdf"
echo.
echo 完成! 输出文件: output\pdf\%~n1.pdf
pause