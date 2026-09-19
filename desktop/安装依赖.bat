@echo off
rem 中文 Windows 标准代码页，避免 UTF-8 批处理解析错位
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo === AI Agent 监控台 - 依赖安装 ===
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
echo [2/2] 正在安装 pywebview 与 pystray ...
%PYCMD% -m pip install pywebview pystray
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。
    echo        可能原因: 网络受限或 PyPI 不可达，可尝试国内镜像:
    echo        %PYCMD% -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pywebview pystray
    pause
    exit /b 1
)

echo.
echo [完成] 依赖安装成功。
echo        启动方式一: 双击 [启动监控台.pyw]
echo        启动方式二: 运行 [创建桌面快捷方式.bat] 固定到桌面
pause
exit /b 0
