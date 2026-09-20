# -*- coding: utf-8 -*-
"""dialogs — WebView2 预检与 Win32 原生弹窗（批次3a 自 app.py 拆分，仅托盘主进程调用）。

弹窗走 ctypes 直调 user32：旧方案依赖的 GUI 库已被 monitor.spec 的 excludes
排除，exe 形态下 import 必然失败、弹窗静默只剩日志，故改用原生 API。
"""
import ctypes
import logging
import webbrowser
from typing import Optional

from monitor.appenv import WINDOW_TITLE

_log = logging.getLogger("agent_monitor")

WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
# Edge WebView2 Runtime 的 EdgeUpdate Clients 键（HKLM WOW6432Node 与 HKCU 两处）
WEBVIEW2_REG_SUB = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
    r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)


def webview2_installed() -> Optional[bool]:
    """True=已安装 / False=明确未安装 / None=不确定（异常一律按不确定）。"""
    try:
        import winreg
        found_key = False
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, WEBVIEW2_REG_SUB) as k:
                    found_key = True
                    pv, _ = winreg.QueryValueEx(k, "pv")
                    s = str(pv).strip()
                    if s and s != "0.0.0.0":
                        return True
            except OSError:
                continue
        return None if found_key else False
    except Exception:
        return None


# flags 数值来源 Win32 SDK winuser.h：MB_OK=0x00、MB_YESNO=0x04、MB_ICONERROR=0x10、
# MB_ICONWARNING=0x30；返回值为按下的按钮 ID，IDYES=6（winuser.h IDYES=6）
def _message_box(text: str, caption: str, flags: int) -> int:
    """Win32 MessageBoxW 原生弹窗（模态阻塞调用线程，与原 messagebox 行为等价）。"""
    return ctypes.windll.user32.MessageBoxW(None, text, caption, flags)


def webview2_dialog(missing: bool) -> None:
    """WebView2 缺失提示（Win32 弹窗 + 可选打开官方下载页）。仅在托盘主进程调用。"""
    try:
        if missing:
            msg = ("未检测到 Microsoft Edge WebView2 Runtime。\n\n"
                   "「GAUGE 衡 · AI Agent 监控台」需要 WebView2 渲染界面。\n"
                   "是否现在打开微软官方下载页？")
        else:
            msg = ("创建主窗口失败，可能缺少 Microsoft Edge WebView2 Runtime。\n\n"
                   "是否打开微软官方下载页安装后重试？")
        if _message_box(msg, WINDOW_TITLE, 0x34) == 6:   # MB_YESNO|MB_ICONWARNING；IDYES
            webbrowser.open(WEBVIEW2_URL)
    except Exception:
        _log.exception("WebView2 提示弹窗失败")


def error_dialog(msg: str) -> None:
    """错误弹窗（Win32 MessageBoxW，仅托盘主进程调用；失败仅留日志不阻断）。"""
    try:
        _message_box(msg, WINDOW_TITLE, 0x10)   # MB_OK | MB_ICONERROR
    except Exception:
        _log.exception("错误弹窗失败")
