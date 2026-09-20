@echo off
rem 中文 Windows 标准代码页，避免 UTF-8 批处理解析错位
chcp 936 >nul
setlocal
cd /d "%~dp0.."

echo === AI Agent 监控台 - 构建 EXE ===
echo.

set "PYCMD="
where python >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if not defined PYCMD (
    where py >nul 2>nul
    if not errorlevel 1 set "PYCMD=py -3"
)
if not defined PYCMD (
    echo [错误] 未找到 Python。请先安装 Python 3.10 或更高版本，安装时勾选 "Add python.exe to PATH"，
    echo        或确保 py 启动器可用。官方下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/2] 已检测到 Python:
%PYCMD% --version
echo.
echo [2/2] 正在 PyInstaller 打包 desktop/monitor.spec ...
%PYCMD% -m PyInstaller desktop/monitor.spec --noconfirm
if errorlevel 1 (
    echo.
    echo [错误] 构建失败。请先安装开发/打包依赖后重试:
    echo        %PYCMD% -m pip install -r "%~dp0..\requirements-dev.txt"
    pause
    exit /b 1
)

echo.
echo [完成] 构建成功。产物: dist\AI-Agent监控台.exe
echo        部署方式: 把 dist\AI-Agent监控台.exe 复制到仓库根目录（与 refresh.py、
echo        template.html 同目录）双击运行；exe 为离线包，不依赖本机 Python 环境。
pause
exit /b 0
