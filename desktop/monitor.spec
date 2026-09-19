# -*- mode: python ; coding: utf-8 -*-
# AI Agent 监控台 — PyInstaller 打包配置（v1.2.0）
# 构建：pyinstaller desktop/monitor.spec   （在 agent-monitor 仓库根执行）
# 产物：dist/AI-Agent监控台.exe —— 部署到 agent-monitor 根目录运行
#       （与 refresh.py / template.html / AI-Agent监控台.html 同目录；不依赖本机 Python）

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent          # agent-monitor 仓库根
DESKTOP = ROOT / "desktop"

a = Analysis(
    [str(DESKTOP / "app.py")],
    pathex=[str(DESKTOP)],
    binaries=[],
    datas=[
        # 冻结运行时需要的源码资源（refresh.py 线程内 importlib 加载；widget.html 为贴纸页面）
        (str(ROOT / "refresh.py"), "."),
        (str(DESKTOP / "widget.html"), "."),
    ],
    hiddenimports=[
        "monitor", "monitor.stats", "monitor.config", "monitor.pin_desktop",
        "pystray._util.win32",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AI-Agent监控台",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(DESKTOP / "monitor.ico"),
)
