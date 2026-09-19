@echo off
rem 中文 Windows 标准代码页，避免 UTF-8 批处理解析错位
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo === AI Agent 监控台 - 创建桌面快捷方式 ===
echo.

if not exist "%~dp0monitor.ico" (
    echo [提示] 未找到 monitor.ico，请先运行一次应用：
    echo        方式一: 双击 [启动监控台.pyw]
    echo        方式二: 命令行运行 python "%~dp0app.py"
    echo        应用首次启动时会自动生成图标文件 monitor.ico。
    pause
    exit /b 1
)

rem ---- 定位 pythonw.exe ----
set "PYWEXE="
for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined PYWEXE set "PYWEXE=%%P"
if not defined PYWEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYWEXE set "PYWEXE=%%~dpPpythonw.exe"
)
if not defined PYWEXE (
    rem py 启动器回退：python.exe 未加入 PATH 时由 py 定位其安装目录下的 pythonw.exe
    for /f "delims=" %%P in ('py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do if not defined PYWEXE set "PYWEXE=%%P"
)
if not defined PYWEXE (
    echo [错误] 未找到 pythonw.exe。请安装 Python 3 并勾选 "Add python.exe to PATH"，
    echo        或确保 py 启动器可用。
    pause
    exit /b 1
)
if not exist "%PYWEXE%" (
    echo [错误] pythonw.exe 不存在: "%PYWEXE%"
    pause
    exit /b 1
)

set "LAUNCHER=%~dp0启动监控台.pyw"
set "WORKDIR=%~dp0"
set "ICONFILE=%~dp0monitor.ico"

echo 目标程序: %PYWEXE%
echo 启动参数: "%LAUNCHER%"
echo 工作目录: %WORKDIR%
echo 图标:     %ICONFILE%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $desktop = [Environment]::GetFolderPath('Desktop'); $lnkPath = Join-Path $desktop 'AI Agent 监控台.lnk'; $lnk = $ws.CreateShortcut($lnkPath); $lnk.TargetPath = $env:PYWEXE; $lnk.Arguments = '\"' + $env:LAUNCHER + '\"'; $lnk.WorkingDirectory = $env:WORKDIR; $lnk.IconLocation = $env:ICONFILE + ',0'; $lnk.Save(); Write-Host ('[完成] 快捷方式已创建: ' + $lnkPath)"
if errorlevel 1 (
    echo [错误] 快捷方式创建失败。
    pause
    exit /b 1
)
pause
exit /b 0
