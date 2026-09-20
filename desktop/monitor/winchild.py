# -*- coding: utf-8 -*-
"""winchild — 窗口子进程（multiprocessing spawn 侧，批次3a 自 app.py 拆分）。

承载两个 webview 窗口的创建与命令服务：
- 主面板：AI-Agent监控台.html；关闭窗口 = 隐藏到托盘（closing return False）
- 桌面贴纸：widget.html，无边框+透明+置顶，Win11 DWM Acrylic 毛玻璃
  （DWMWA_SYSTEMBACKDROP_TYPE=38→3，主题事件重设时低频幂等重打），
  鼠标穿透（WS_EX_TRANSPARENT 整窗开关）、拖动位置回传主进程记忆

关键约束（批次3a 修复）：window_process_main 是 spawn target，必须定义在
可导入模块中——frozen（PyInstaller）形态下 spawn 按模块路径反序列化 target，
定义在 __main__（app.py）会让子进程引导卡住（py-spy 栈空，项目记忆已记录）。
"""
import json
import logging
import os
import sys
import threading
from typing import Any, Dict, Optional

import webview

from monitor.appenv import (
    HTML_PATH, WIDGET_BACKDROP_MIN_BUILD, WIDGET_HTML_PATH,
    WIDGET_TITLE, WINDOW_TITLE, log, setup_logging,
)
from monitor.config import (
    DEFAULTS, WIDGET_DEFAULT_POS, validate_widget_settings,
    widget_settings_from_cfg,
)
from monitor.pin_desktop import pin_to_desktop, unpin_from_desktop

_log = logging.getLogger("agent_monitor")

# 贴纸两种布局只在当前运行记忆，不写入 widget.json。compact 是默认桌面
# 常驻尺寸，wide 用于需要更大历史图的临时查看；页面通过
# renderWidgetLayout() 同步 CSS 状态，原生窗口 resize 是唯一尺寸来源。
WIDGET_WINDOW_SIZES = {
    "compact": (380, 460),
    "wide": (740, 460),
}
WIDGET_WINDOW_SIZE = WIDGET_WINDOW_SIZES["compact"]

# 贴纸圆角半径（CSS px）：必须与 widget.html 的 #widget{border-radius:28px} 一致，
# 原生 SetWindowRgn 裁剪与页面 CSS 圆角共用该单一来源。
WIDGET_RADIUS_CSS = 28

# 官方用量页（贴纸 ↗ 与托盘「官方用量页」共用；托盘侧从本模块导入，单一来源）
USAGE_PAGE_URL = "https://bigmodel.cn/coding-plan/personal/usage"


class _WindowState:
    """窗口子进程状态：主面板与贴纸两个窗口对象、就绪事件、退出标志与贴纸配置缓存。"""

    def __init__(self) -> None:
        self.window: Any = None              # 主面板窗口
        self.widget: Any = None              # 桌面贴纸窗口
        self.window_ready = threading.Event()
        self.widget_ready = threading.Event()
        self.widget_native_hwnd: Optional[int] = None
        self.exiting = False
        # 最近一次贴纸数据（WIDGET_DATA payload）：销毁重建后经 loaded 钩子
        # 立即重注入，避免贴纸空白等待下一次刷新。
        self.last_widget_data: Optional[Dict[str, Any]] = None
        # 初始贴纸配置缓存 = config.DEFAULTS（与原字面量等价；仅键集多出 pinned，
        # 子进程内只做 .get 读取，不回传、不持久化）
        self.cfg: Dict[str, Any] = dict(DEFAULTS)
        self.layout = "compact"
        self._layout_lock = threading.Lock()
        self._pos_timer: Any = None
        self._pos_lock = threading.Lock()


def on_loaded(state: "_WindowState") -> None:
    """主面板 loaded 事件：标记窗口就绪并留日志。"""
    state.window_ready.set()
    log("页面加载完成：%s" % HTML_PATH.name)


def on_closing(state: "_WindowState") -> Optional[bool]:
    """关闭窗口 = 隐藏到托盘（return False 取消关闭）。退出流程中放行。"""
    if state.exiting:
        return None
    log("[win] 窗口关闭请求 → 隐藏到托盘")
    try:
        w = state.window
        if w is not None:
            w.hide()
    except Exception:
        _log.exception("[win] 隐藏窗口失败")
    return False


# ---- Win32：毛玻璃与鼠标穿透（仅贴纸窗口；失效自动降级） ----
def _win_hwnd(window: Any) -> Optional[int]:
    """取 pywebview 窗口的 Win32 句柄（native 为 WinForms BrowserForm）。

    native.Handle 是 .NET IntPtr，须经 ToInt64() 转 int（直接 int() 会 TypeError）。
    """
    try:
        native = getattr(window, "native", None)
        if native is None:
            return None
        handle = getattr(native, "Handle", None)
        if handle is None:
            return None
        try:
            return int(handle.ToInt64())
        except AttributeError:
            return int(handle)
    except Exception:
        return None


def apply_acrylic_backdrop(hwnd: int) -> bool:
    """Win11 22621+：DWMWA_SYSTEMBACKDROP_TYPE=3（Acrylic）+ 暗色 + sheet-of-glass。

    注意：pywebview 会在系统主题变化事件时把 38 号属性重设（dark→2/light→1），
    因此该函数需要低频幂等重调（数据注入/窗口 shown 时）。
    返回 True=已设置；False=环境不支持（调用方应加深 CSS 背景降级）。
    """
    try:
        if sys.getwindowsversion().build < WIDGET_BACKDROP_MIN_BUILD:
            return False
        import ctypes
        d = ctypes.windll.dwmapi
        hwnd = int(hwnd)

        class MARGINS(ctypes.Structure):
            _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                        ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]

        backdrop = ctypes.c_int(3)      # DWMSBT_TRANSIENTWINDOW → Acrylic
        dark = ctypes.c_int(1)          # DWMWA_USE_IMMERSIVE_DARK_MODE
        from ctypes import wintypes as wt
        d.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
        d.DwmSetWindowAttribute.restype = ctypes.c_long
        d.DwmExtendFrameIntoClientArea.argtypes = [wt.HWND, ctypes.POINTER(MARGINS)]
        d.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
        result = d.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), 4)
        d.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)
        margins = MARGINS(-1, -1, -1, -1)   # sheet of glass
        frame_result = d.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
        return result == 0 and frame_result == 0
    except Exception:
        _log.exception("[win] 毛玻璃设置失败（降级为深色实底）")
        return False


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
CLICK_THROUGH_MASK = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE


def set_click_through(hwnd: int, on: bool) -> bool:
    """整窗鼠标穿透开关（WS_EX_TRANSPARENT 是 all-or-nothing）。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(hwnd)
        old = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        new = (old | CLICK_THROUGH_MASK) if on else (old & ~CLICK_THROUGH_MASK)
        if new != old:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
        return True
    except Exception:
        _log.exception("[win] 鼠标穿透切换失败")
        return False



def configure_process_dpi() -> bool:
    """Request per-monitor DPI before WinForms creates any HWND in this process."""
    try:
        import ctypes
        api = ctypes.windll.user32.SetProcessDpiAwarenessContext
        api.argtypes = [ctypes.c_void_p]
        api.restype = ctypes.c_bool
        return bool(api(ctypes.c_void_p(-4)))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        return False


def _on_widget_ui(widget: Any, action: Any) -> Any:
    """All WinForms geometry and paint changes run on its owning UI thread."""
    native = getattr(widget, "native", None)
    if native is None:
        raise RuntimeError("Widget native window is not ready")
    result: list = []
    failures: list = []

    def run() -> None:
        try:
            result.append(action(native))
        except Exception as exc:
            failures.append(exc)

    if native.InvokeRequired:
        from System import Action
        native.Invoke(Action(run))
    else:
        run()
    if failures:
        raise failures[0]
    return result[0] if result else None


def _round_window_rgn(hwnd: int, width: int, height: int, scale: float) -> None:
    """Apply the rounded clip from one shared geometry reading.

    width/height 是同一次 UI action 里读回的客户区尺寸，scale 与该客户区
    取自同一次 GetDpiForWindow；客户区、WebView2 与裁剪因此共用同一份几何。
    """
    import ctypes
    from ctypes import wintypes as wt
    user = ctypes.windll.user32
    gdi = ctypes.windll.gdi32
    gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
    gdi.CreateRoundRectRgn.restype = wt.HRGN
    user.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, wt.BOOL]
    user.SetWindowRgn.restype = ctypes.c_int
    gdi.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi.DeleteObject.restype = wt.BOOL
    # CreateRoundRectRgn takes ELLIPSE DIAMETER; CSS takes radius.
    diameter = max(2, round(2 * WIDGET_RADIUS_CSS * scale))
    region = gdi.CreateRoundRectRgn(0, 0, width + 1, height + 1,
                                    diameter, diameter)
    if region and not user.SetWindowRgn(hwnd, region, True):
        gdi.DeleteObject(region)
        raise OSError("SetWindowRgn failed")


def _sync_widget_geometry(state: "_WindowState", widget: Any,
                          layout: Optional[str] = None) -> None:
    """Size the CLIENT area, not the pre-frameless outer Size of BrowserForm."""
    width, height = WIDGET_WINDOW_SIZES[layout or state.layout]

    def sync(native: Any) -> None:
        import ctypes
        from System.Drawing import Color, Size
        from System.Windows.Forms import DockStyle
        hwnd = _win_hwnd(widget)
        if not hwnd:
            return
        scale = max(1.0, ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0)
        # The default Control background paints a grey rectangle below a
        # transparent WebView. Black is the DWM glass backing surface.
        native.BackColor = Color.Black
        target = Size(round(width * scale), round(height * scale))
        if native.ClientSize != target:
            native.ClientSize = target
        view = native.webview
        view.DefaultBackgroundColor = Color.Transparent
        view.Dock = DockStyle.Fill
        # Read the client rect back so the WebView bounds and the rounded
        # clip share one geometry source even if WinForms adjusts the size.
        client = native.ClientRectangle
        view.Bounds = client
        native.PerformLayout()
        view.Invalidate(True)
        native.Invalidate(True)
        _round_window_rgn(hwnd, client.Width, client.Height, scale)
    _on_widget_ui(widget, sync)


def _apply_rounded_region(widget: Any, state: "_WindowState") -> None:
    """Clip the native surface to the same 28 CSS px radius as the page."""
    if widget is None:
        return

    def clip(native: Any) -> None:
        import ctypes
        from ctypes import wintypes as wt
        hwnd = _win_hwnd(widget)
        if not hwnd:
            return
        user = ctypes.windll.user32
        user.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        user.GetClientRect.restype = wt.BOOL
        user.GetDpiForWindow.argtypes = [wt.HWND]
        user.GetDpiForWindow.restype = wt.UINT
        rect = wt.RECT()
        if not user.GetClientRect(hwnd, ctypes.byref(rect)):
            return
        width, height = rect.right, rect.bottom
        if width <= 0 or height <= 0:
            return
        scale = max(1.0, user.GetDpiForWindow(hwnd) / 96.0)
        _round_window_rgn(hwnd, width, height, scale)
    try:
        _on_widget_ui(widget, clip)
    except Exception:
        _log.exception("[win] 贴纸圆角裁剪失败")

class WidgetApi:
    """贴纸页面的 js_api。

    网页只能通过本类发起设置更新，主进程收到结构化消息后再次验证并
    成为 widget.json 的唯一写者。布局切换只改变当前窗口，不落盘。
    """

    def __init__(self, state: "_WindowState") -> None:
        self._state = state

    def open_main(self) -> bool:
        """展开主面板窗口（贴纸 footer ⚙ 按钮）。"""
        try:
            if self._state.window is not None:
                self._state.window.show()
            return True
        except Exception:
            _log.exception("[win] open_main 失败")
            return False

    def open_usage_page(self) -> bool:
        """打开官方用量页（贴纸 footer ↗ 按钮，默认浏览器）。"""
        try:
            os.startfile(USAGE_PAGE_URL)
            return True
        except Exception:
            _log.exception("[win] 打开官方用量页失败：%s" % USAGE_PAGE_URL)
            return False


    def toggle_layout(self) -> bool:
        """Resize the existing WebView; keep its bridge, data and HWND alive."""
        state = self._state
        widget = state.widget
        if widget is None or not state._layout_lock.acquire(blocking=False):
            return False
        old = state.layout
        target = "wide" if old == "compact" else "compact"
        try:
            _sync_widget_geometry(state, widget, target)
            widget.evaluate_js("renderWidgetLayout(%s);" % json.dumps(target))
            state.layout = target
            log("[win] 贴纸布局切换为 %s（保留窗口）" % target)
            return True
        except Exception:
            _log.exception("[win] 贴纸布局切换失败，恢复原布局")
            try:
                _sync_widget_geometry(state, widget, old)
                widget.evaluate_js("renderWidgetLayout(%s);" % json.dumps(old))
            except Exception:
                _log.exception("[win] 原布局恢复失败")
            return False
        finally:
            state._layout_lock.release()

    def get_widget_settings(self) -> Dict[str, Any]:
        """返回当前主进程反馈过来的三个可编辑设置字段。"""
        return widget_settings_from_cfg(self._state.cfg)

    def update_widget_settings(self, settings: Any) -> bool:
        """验证并向主进程请求部分设置更新。

        这里只发送白名单字段；主进程会再次验证、持久化并通过 WIDGET_CFG
        回传最终状态。无效输入不会触碰窗口样式或配置文件。
        """
        clean = validate_widget_settings(settings)
        if clean is None:
            log("[win] 拒绝非法贴纸设置：%r" % (settings,))
            return False
        try:
            pipe = _CHILD_PIPE[0]
            if pipe is None:
                return False
            with _CHILD_PIPE_LOCK:
                pipe.send(["WIDGET_SETTINGS", clean])
            log("[win] 已请求贴纸设置更新：%s" % clean)
            return True
        except Exception:
            _log.exception("[win] 贴纸设置更新发送失败")
            return False

    def refresh_now(self) -> bool:
        """贴纸「立即刷新」（↻ 按钮）：经既有子进程→主进程 Pipe 通道请求一次刷新。

        主进程 child_msg_loop 收到 "WIDGET_REFRESH_NOW" 后走既有 refresh_once
        （刷新成功会自动向贴纸推送最新 WIDGET_DATA）。不新建机制。
        """
        try:
            pipe = _CHILD_PIPE[0]
            if pipe is not None:
                with _CHILD_PIPE_LOCK:
                    pipe.send("WIDGET_REFRESH_NOW")
                log("[win] 已请求主进程立即刷新")
            return True
        except Exception:
            _log.exception("[win] refresh_now 发送失败")
            return False

    def hide_widget(self) -> bool:
        """隐藏贴纸（页面侧触发，同步托盘菜单勾选状态）。"""
        try:
            if self._state.widget is not None:
                self._state.widget.hide()
            pipe = _CHILD_PIPE[0]
            if pipe is not None:
                with _CHILD_PIPE_LOCK:
                    pipe.send("WIDGET_STATE:hidden")
            return True
        except Exception:
            _log.exception("[win] hide_widget 失败")
            return False

    def move_widget_by(self, dx: float, dy: float) -> bool:
        """页面拖动增量（物理像素）：屏幕坐标系 GetWindowRect+SetWindowPos 平移。

        WebView2 mousemove 的 screenX 增量实测即屏幕物理尺度，不再做 DPI 换算；
        钉桌面（WorkerW 子窗口）时经 MapWindowPoints 转父窗口客户区坐标，
        两种挂载形态下均严格 1:1 跟手。|dx|/|dy|>300 视为异常尖峰拒绝。
        """
        widget = self._state.widget
        if not isinstance(dx, (int, float)) or not isinstance(dy, (int, float)):
            return False
        if abs(dx) > 300 or abs(dy) > 300:
            return False
        hwnd = self._state.widget_native_hwnd or _win_hwnd(widget)
        if not widget or not hwnd:
            return False
        import ctypes
        from ctypes import wintypes as wt
        user32 = ctypes.windll.user32

        def mv(_native: Any) -> None:
            r = wt.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                raise OSError("GetWindowRect failed")   # hwnd 校验后失效：交外层记日志并返 False
            x = r.left + round(dx)
            y = r.top + round(dy)
            parent = user32.GetParent(hwnd)
            if parent:
                pt = wt.POINT(x, y)
                user32.MapWindowPoints(None, parent, ctypes.byref(pt), 1)
                x, y = pt.x, pt.y
            # SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE
            user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)
        try:
            _on_widget_ui(widget, mv)
            return True
        except Exception:
            _log.exception("[win] move_widget_by 失败")
            return False


# 子进程侧持有自己的 pipe 引用（js_api 回调里需要反向通知主进程）
_CHILD_PIPE: list = [None]
_CHILD_PIPE_LOCK = threading.Lock()



def _widget_moved_debounced(state: "_WindowState", x: int, y: int) -> None:
    """Persist position once dragging settles; then resync for the new DPI."""
    widget = state.widget

    def flush() -> None:
        if state.exiting or state.widget is not widget or widget is None:
            return
        state.cfg.update(x=int(x), y=int(y))
        try:
            pipe = _CHILD_PIPE[0]
            if pipe is not None:
                with _CHILD_PIPE_LOCK:
                    pipe.send(["WIDGET_POS", int(x), int(y)])
            _sync_widget_geometry(state, widget)
        except Exception:
            _log.exception("[win] 贴纸位置/跨屏几何同步失败")
    with state._pos_lock:
        if state._pos_timer is not None:
            state._pos_timer.cancel()
        state._pos_timer = threading.Timer(0.5, flush)
        state._pos_timer.daemon = True
        state._pos_timer.start()


def on_widget_loaded(state: "_WindowState", widget: Any = None) -> None:
    """One initialization path for initial and reopened widgets."""
    widget = widget or state.widget
    if widget is None or widget is not state.widget or state.exiting:
        return
    try:
        _sync_widget_geometry(state, widget)
        hwnd = _win_hwnd(widget)
        state.widget_native_hwnd = hwnd
        glass = bool(hwnd and apply_acrylic_backdrop(hwnd))
        if hwnd:
            set_click_through(hwnd, bool(state.cfg.get("passthrough")))
        script = (
            "renderWidgetSettings(%s);renderWidgetLayout(%s);"
            "if(window.renderWidgetMaterial)renderWidgetMaterial(%s);"
        ) % (
            json.dumps(widget_settings_from_cfg(state.cfg)),
            json.dumps(state.layout), json.dumps({"glass": glass}),
        )
        if state.last_widget_data is not None:
            script += "renderWidget(%s);" % json.dumps(
                state.last_widget_data, ensure_ascii=False)
        widget.evaluate_js(script)
        log("[win] 贴纸初始化完成（%s，glass=%s）" % (state.layout, glass))
    except Exception:
        _log.exception("[win] 贴纸初始化失败")
    finally:
        if widget is state.widget:
            state.widget_ready.set()


def on_widget_shown(state: "_WindowState", widget: Any = None) -> None:
    widget = widget or state.widget
    if widget is None or widget is not state.widget or state.exiting:
        return
    try:
        _sync_widget_geometry(state, widget)
        hwnd = _win_hwnd(widget)
        state.widget_native_hwnd = hwnd
        if hwnd:
            apply_acrylic_backdrop(hwnd)
    except Exception:
        _log.exception("[win] 贴纸显示几何同步失败")


def on_widget_closed(state: "_WindowState", widget: Any = None) -> None:
    """Ignore a stale window event; only the current window owns visibility."""
    widget = widget or state.widget
    if widget is not state.widget:
        return
    state.widget = None
    state.widget_ready.clear()
    state.widget_native_hwnd = None
    if state._pos_timer is not None:
        state._pos_timer.cancel()
    if state.exiting:
        return
    pipe = _CHILD_PIPE[0]
    if pipe is not None:
        try:
            with _CHILD_PIPE_LOCK:
                pipe.send(["WIDGET_CLOSED"])
        except Exception:
            _log.exception("[win] WIDGET_CLOSED 上报失败")


def _bind_widget_events(state: "_WindowState", widget: Any) -> None:
    widget.events.loaded += lambda: on_widget_loaded(state, widget)
    widget.events.shown += lambda: on_widget_shown(state, widget)
    widget.events.closed += lambda: on_widget_closed(state, widget)

    def moved(*_args: Any) -> None:
        if widget is state.widget:
            _widget_moved_debounced(state, int(widget.x), int(widget.y))

    def resized(*_args: Any) -> None:
        if widget is state.widget:
            _apply_rounded_region(widget, state)
    widget.events.moved += moved
    widget.events.resized += resized


def _ensure_widget_window(
    state: "_WindowState",
    x: Optional[int] = None,
    y: Optional[int] = None,
    size: Optional[tuple] = None,
) -> bool:
    """Recreate only a genuinely closed window; layout changes never destroy it."""
    if state.widget is not None:
        return True
    width, height = size or WIDGET_WINDOW_SIZES[state.layout]
    try:
        widget = webview.create_window(
            WIDGET_TITLE, WIDGET_HTML_PATH.as_uri(), width=width, height=height,
            x=x if x is not None else state.cfg.get("x", WIDGET_DEFAULT_POS[0]),
            y=y if y is not None else state.cfg.get("y", WIDGET_DEFAULT_POS[1]),
            frameless=True, transparent=True, background_color="#0A0A0C",
            resizable=False, easy_drag=False, on_top=True, js_api=WidgetApi(state),
        )
        if widget is None:
            return False
        state.widget = widget
        _bind_widget_events(state, widget)
        # Runtime create_window can finish navigation before listeners attach.
        if widget.events.loaded.is_set():
            on_widget_loaded(state, widget)
        return True
    except Exception:
        _log.exception("[win] 贴纸窗口创建失败")
        return False


def _window_cmd_loop(state: "_WindowState", pipe: Any) -> None:
    """webview.start 回调（子线程）：处理主进程命令。

    协议：str 命令（SHOW/RELOAD/EXIT）或 tuple（CMD, payload）：
      ("WIDGET_SHOW", None) / ("WIDGET_HIDE", None)
      ("WIDGET_CFG", {"passthrough":bool,"opacity":float,"pinned":bool})
      ("WIDGET_DATA", stats_dict)
      ("WIDGET_REFRESH_STATE", {"state":"refreshing"|"failed", ...})
    子→主反向消息：["WIDGET_POS", x, y]（拖动去抖）、"WIDGET_STATE:hidden"。
    """
    while True:
        try:
            if not pipe.poll(0.3):
                continue
            msg = pipe.recv()
        except (EOFError, OSError):
            log("[win] 与主进程的管道断开（主进程退出），窗口随之退出")
            state.exiting = True
            for window in (state.widget, state.window):
                if window is not None:
                    try:
                        window.destroy()
                    except Exception:
                        _log.exception("[win] 管道断开后的窗口清理失败")
            return
        except Exception:
            _log.exception("[win] 命令接收异常")
            continue

        cmd, payload = (msg if isinstance(msg, tuple) else (msg, None))

        if cmd == "SHOW":
            log("[win] 收到 SHOW → 显示窗口")
            try:
                state.window.show()
            except Exception:
                _log.exception("[win] 显示窗口失败")
        elif cmd == "RELOAD":
            log("[win] 收到 RELOAD → 重载页面")
            try:
                state.window.evaluate_js("location.reload()")
            except Exception:
                _log.exception("[win] 页面重载失败")
        elif cmd == "EXIT":
            log("[win] 收到 EXIT → 销毁窗口退出")
            state.exiting = True
            try:
                state.window.destroy()
            except Exception:
                _log.exception("[win] 销毁窗口失败")
            try:
                if state.widget is not None:
                    state.widget.destroy()
            except Exception:
                pass
            return
        elif cmd == "WIDGET_SHOW":
            log("[win] 收到 WIDGET_SHOW → 显示贴纸")
            try:
                if not _ensure_widget_window(state):
                    log("[win] 贴纸窗口重建失败，忽略 WIDGET_SHOW")
                    continue
                state.widget.show()
                on_widget_shown(state)
            except Exception:
                _log.exception("[win] 显示贴纸失败")
        elif cmd == "WIDGET_HIDE":
            log("[win] 收到 WIDGET_HIDE → 隐藏贴纸")
            try:
                state.widget.hide()
            except Exception:
                _log.exception("[win] 隐藏贴纸失败")
        elif cmd == "WIDGET_PIN":
            want = bool(payload)
            hwnd = state.widget_native_hwnd or _win_hwnd(state.widget)
            ok = False
            if hwnd:
                ok = pin_to_desktop(hwnd) if want else unpin_from_desktop(hwnd, keep_on_top=True)
            log("[win] WIDGET_PIN=%s → %s" % (want, "成功" if ok else "失败"))
            with _CHILD_PIPE_LOCK:
                pipe.send(["WIDGET_PIN_RESULT", want, ok])
        elif cmd == "WIDGET_CFG":
            clean = validate_widget_settings(payload)
            if clean is not None:
                state.cfg.update(clean)
                hwnd = state.widget_native_hwnd or _win_hwnd(state.widget)
                if hwnd:
                    set_click_through(hwnd, bool(state.cfg.get("passthrough")))
                try:
                    state.widget.evaluate_js(
                        "renderWidgetSettings(%s);"
                        % json.dumps(widget_settings_from_cfg(state.cfg))
                    )
                except Exception:
                    _log.exception("[win] 贴纸设置页面同步失败")
                log("[win] 贴纸配置已应用：%s" % clean)
            else:
                log("[win] 忽略非法 WIDGET_CFG：%r" % (payload,))
        elif cmd == "WIDGET_REFRESH_STATE":
            if isinstance(payload, dict):
                try:
                    state.widget_ready.wait(5)
                    state.widget.evaluate_js(
                        "renderRefreshState(%s);"
                        % json.dumps(payload, ensure_ascii=False)
                    )
                except Exception:
                    _log.exception("[win] 刷新状态注入失败")
        elif cmd == "WIDGET_DATA":
            if isinstance(payload, dict):
                state.last_widget_data = payload   # 缓存：贴纸销毁重建后立即重注入
                try:
                    state.widget_ready.wait(5)   # 页面未就绪时等待，避免注入丢失
                    state.widget.evaluate_js(
                        "renderWidget(%s);" % json.dumps(payload, ensure_ascii=False)
                    )
                    log("[win] 贴纸数据已注入")
                except Exception:
                    _log.exception("[win] 贴纸数据注入失败")
        else:
            log("[win] 忽略未知命令：%r" % (msg,))


def window_process_main(pipe: Any, widget_cfg: Optional[Dict[str, Any]] = None) -> int:
    """窗口子进程入口（spawn target）：创建主窗口与贴纸窗口并服务命令循环。

    必须保持在可导入模块顶层（见模块 docstring 的 frozen spawn 约束）。
    正常返回 0（收到 EXIT 或主进程管道关闭）；主窗口创建失败返回 1。
    """
    configure_process_dpi()
    setup_logging("win")
    log("[win] 窗口子进程启动（python=%s）" % sys.version.split()[0])
    state = _WindowState()
    if isinstance(widget_cfg, dict):
        state.cfg.update(widget_cfg)

    try:
        window = webview.create_window(
            WINDOW_TITLE,
            HTML_PATH.as_uri(),
            width=1280,
            height=820,
            min_size=(960, 600),
        )
        _CHILD_PIPE[0] = pipe
        state.widget = webview.create_window(
            WIDGET_TITLE,
            WIDGET_HTML_PATH.as_uri(),
            width=WIDGET_WINDOW_SIZE[0],
            height=WIDGET_WINDOW_SIZE[1],
            x=state.cfg.get("x", WIDGET_DEFAULT_POS[0]),
            y=state.cfg.get("y", WIDGET_DEFAULT_POS[1]),
            frameless=True,
            transparent=True,
            background_color="#0A0A0C",
            resizable=False,
            easy_drag=False,
            on_top=True,
            hidden=not state.cfg.get("visible", True),
            js_api=WidgetApi(state),
        )
    except Exception as e:  # noqa: BLE001
        _log.exception("[win] 创建窗口失败")
        log("[win] FATAL: %r（可能缺少 Microsoft Edge WebView2 Runtime）" % (e,))
        return 1

    state.window = window
    assert window is not None   # webview stubs 标注 Optional[Window]；创建失败已在 except 分支返回
    window.events.loaded += lambda: on_loaded(state)
    window.events.closing += lambda: on_closing(state)
    _bind_widget_events(state, state.widget)

    threading.Thread(
        target=_window_cmd_loop, args=(state, pipe), daemon=True, name="cmd-loop"
    ).start()

    try:
        webview.start()
    except Exception:
        _log.exception("[win] GUI 主循环异常退出")
        return 1
    log("[win] GUI 主循环结束，窗口子进程退出")
    return 0
