# -*- coding: utf-8 -*-
"""pin_desktop — 把窗口"钉"到桌面层（壁纸之上、桌面图标之下）。

原理（Research V2 预研）：向 Progman 发送 0x052C 消息使其 spawn 一个 WorkerW，
枚举找到含 SHELLDLL_DefView（桌面图标视图）的 WorkerW，其后的下一个 WorkerW
即壁纸层；SetParent 到该层即可。Rainmeter/Lively 同源方案，Win11 24H2 实测可用。

已知限制（README/气泡需提示用户）：
- explorer 重启后父子关系失效，需重新钉（应用不崩溃，贴纸回到普通置顶层）；
- 极少数 Windows 预览构建上该层级方案失效（Lively #2074）——失败时返回 False，
  调用方回退置顶模式。
"""
import ctypes
from ctypes import wintypes
from typing import Optional, Tuple

user32 = ctypes.windll.user32

_PROGMAN_SPAWN_MSG = 0x052C        # Progman 蜂鸣消息：spawn WorkerW
_GWL_STYLE = -16
_WS_CHILD = 0x40000000             # SetParent 后子窗口必须带 WS_CHILD
_HWND_BOTTOM = 1


def _find_workerw_behind_icons() -> Optional[int]:
    """返回壁纸层 WorkerW 的句柄；找不到返回 None。"""
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return None
    user32.SendMessageW(progman, _PROGMAN_SPAWN_MSG, 0, 0)

    result: list = []

    @ctypes.WINFUNCTYPE(wintypes.HWND, wintypes.HWND, wintypes.LPARAM)
    def enum_child(child: int, _lparam: int) -> bool:
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, buf, 64)
        if buf.value == "SHELLDLL_DefView":
            # 同级之后的下一个 WorkerW 即壁纸层
            worker = user32.FindWindowExW(progman, child, "WorkerW", None)
            if worker:
                result.append(worker)
            else:
                # 某些构建下 WorkerW 是 Progman 的兄弟而非子级：枚举顶层窗口兜底
                result.append(-1)   # 标记走兜底枚举
        return True

    user32.EnumChildWindows(progman, enum_child, 0)
    if result and result[0] == -1:
        top_worker = user32.FindWindowExW(None, None, "WorkerW", None)
        if top_worker:
            result[0] = top_worker
        else:
            return None
    return result[0] if result else None


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def pin_to_desktop(hwnd: int) -> bool:
    """把给定顶层窗口钉到桌面层。成功 True；失败 False（调用方回退置顶）。

    SetParent 会使窗口坐标变为相对 WorkerW (0,0)：内部先记录原屏幕矩形，
    钉住后 SetWindowPos 移回原屏幕位置（WorkerW 全屏覆盖桌面，两者一致）。
    HWND_BOTTOM 同时清除 TOPMOST（钉桌面与置顶互斥）。
    """
    try:
        hwnd = int(hwnd)
        worker = _find_workerw_behind_icons()
        if not worker:
            return False
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
        user32.SetWindowLongW(hwnd, _GWL_STYLE, (style & ~0x80000000) | _WS_CHILD)
        if not user32.SetParent(hwnd, worker):
            return False
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        # NOMOVE/NOSIZE 之外显式移动回原屏幕位置；HWND_BOTTOM 清除 TOPMOST
        user32.SetWindowPos(hwnd, _HWND_BOTTOM, rect.left, rect.top, w, h, 0x0004)
        return True
    except Exception:
        return False


def unpin_from_desktop(hwnd: int, keep_on_top: bool = True) -> bool:
    """从桌面层取回：解除父子关系、移除 WS_CHILD，可选恢复 TOPMOST。成功 True。"""
    try:
        hwnd = int(hwnd)
        style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
        user32.SetWindowLongW(hwnd, _GWL_STYLE, style & ~_WS_CHILD)
        if not user32.SetParent(hwnd, None):
            return False
        user32.SetWindowPos(hwnd, -1 if keep_on_top else -2, 0, 0, 0, 0, 0x0053)
        return True
    except Exception:
        return False
