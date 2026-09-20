@echo off
rem 中文 Windows 标准代码页，避免 UTF-8 批处理解析错位
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo === GAUGE 衡 · AI Agent 监控台 - 构建安装包 ===
echo.

rem ---- 1. 定位 Inno Setup 7 编译器 ISCC.exe ----
set "ISCC="
where ISCC >nul 2>nul
if not errorlevel 1 set "ISCC=ISCC"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if not defined ISCC (
    echo [错误] 未找到 Inno Setup 7 编译器 ISCC.exe。
    echo        请先安装 Inno Setup 7，官方下载地址: https://jrsoftware.org/isdl.php
    echo        或把 ISCC.exe 所在目录加入 PATH 后重试。
    pause
    exit /b 1
)

rem ---- 2. 检查待打包的 exe 是否已构建 ----
set "SRCEXE=%~dp0..\dist\AI-Agent监控台.exe"
if not exist "%SRCEXE%" (
    echo [错误] 未找到已构建的 exe:
    echo        %SRCEXE%
    echo        请先运行 desktop\构建EXE.bat 生成，或手动执行:
    echo        python -m PyInstaller desktop\monitor.spec --noconfirm
    pause
    exit /b 1
)

echo [1/2] 已检测到编译器: %ISCC%
echo [2/2] 正在编译 installer\GAUGE衡.iss ...
"%ISCC%" "%~dp0GAUGE衡.iss"
if errorlevel 1 (
    echo.
    echo [错误] 安装包编译失败，请查看上方 ISCC 日志。
    pause
    exit /b 1
)

echo.
echo [完成] 安装包已生成:
echo        %~dp0out\GAUGE-衡-Setup-1.2.0.exe
pause
exit /b 0
