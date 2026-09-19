@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYCMD="
where python >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if not defined PYCMD (
  where py >nul 2>nul
  if not errorlevel 1 set "PYCMD=py -3"
)
if not defined PYCMD (
  echo [错误] 未找到 Python。请先安装 Python 3 并勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)
echo 正在从 ZCode 会话库刷新数据（只读，不写数据库），请稍候...
echo.
%PYCMD% refresh.py
if errorlevel 1 (
  echo.
  echo [失败] 数据刷新未完成，成品 HTML 保持为上一次的数据。请阅读上方错误信息。
  pause
  exit /b 1
)
echo.
echo [完成] 已生成 AI-Agent监控台.html，双击它即可查看最新数据。
timeout /t 3 >nul
endlocal
