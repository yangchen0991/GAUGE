#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — AI Agent 监控台 Windows 桌面壳（双进程架构）

为什么双进程：pystray 与 pywebview 同进程共存时，Shell_NotifyIcon(NIM_ADD) 在
pywebview（pythonnet/CLR）环境中会静默失败（实测注册表 NotifyIconSettings 无条目），
而独立进程同样用法注册成功。故按 pywebview 官方 pystray 示例的结构拆分：

- 托盘主进程（本进程）：pystray icon.run() 占用主线程（pystray 设计场景）
  · 单实例锁（127.0.0.1:59321）
  · 托盘菜单：显示监控台 / 立即刷新 / 自动刷新间隔(5/15/30 分钟·关闭，默认 5) /
    今日用量气泡 / 打开数据目录 / 退出
  · 自动刷新线程（Event.wait 循环调用上级目录 refresh.py，只读数据库）
  · 今日统计（tooltip / 气泡）与 WebView2 预检弹窗
  · 对 Shell_NotifyIcon 的每次调用记录返回值（NIM_ADD=False 时自动重试一次并留日志）
- 窗口子进程（spawn）：pywebview 渲染上级目录 AI-Agent监控台.html
  · 关闭窗口 = 隐藏到托盘（closing 事件 return False 取消关闭）
  · 通过 Pipe 接收主进程命令：SHOW（显示）/ RELOAD（刷新页面）/ EXIT（退出）
- 退出：托盘菜单"退出" → 发 EXIT → 子进程销毁窗口退出 → 主进程 join 后 icon.stop()
- --smoke：冒烟模式（单进程，无托盘、无单实例锁），结果写 desktop/smoke_result.json
"""
import os
import sys

# ---- pythonw 入口防护（必须最先执行：无控制台时三流为 None）----
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import json
import logging
import multiprocessing
import socket
import sqlite3
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import pystray
import webview

# ---------- 路径与常量 ----------
APP_DIR = Path(__file__).resolve().parent          # ...\agent-monitor\desktop
PARENT_DIR = APP_DIR.parent                        # ...\agent-monitor
HTML_PATH = PARENT_DIR / "AI-Agent监控台.html"
REFRESH_PY = PARENT_DIR / "refresh.py"
LOG_PATH = APP_DIR / "app.log"
ICO_PATH = APP_DIR / "monitor.ico"
SMOKE_PATH = APP_DIR / "smoke_result.json"
DB_PATH = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"

WINDOW_TITLE = "AI Agent 监控台"
SINGLE_PORT = 59321
REFRESH_TIMEOUT = 600          # 秒，与 refresh.py 的超时口径一致
TOOLTIP_MAX = 128              # Windows 托盘 tooltip 上限约 128 字符
NOTIFY_MAX = 200               # 失败气泡最多展示 stderr 尾部 200 字
CHILD_JOIN_TIMEOUT = 10        # 退出时等待窗口子进程的秒数

# 冻结价目（元/百万 token）：model_id -> (输入, 缓存读取, 输出)；未列出的模型按 0 计
PRICES = {
    "GLM-5.3-Flash": (0.8, 0.23, 2.8),
    "GLM-5.3": (8.0, 2.0, 28.0),
}

# 自动刷新间隔选项（label, 秒）；0 = 关闭。默认 5 分钟。
INTERVAL_CHOICES = (("5 分钟", 300), ("15 分钟", 900), ("30 分钟", 1800), ("关闭", 0))

WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
# Edge WebView2 Runtime 的 EdgeUpdate Clients 键（HKLM WOW6432Node 与 HKCU 两处）
WEBVIEW2_REG_SUB = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
    r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)

_log = logging.getLogger("agent_monitor")


def log(msg):
    _log.info(msg)


def setup_logging(tag=None):
    """tag 用于区分窗口子进程日志（如 [win]），避免双进程日志无法归属。"""
    handlers = []
    try:
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8", delay=True))
    except Exception:
        pass
    if sys.stdout is not None:
        try:
            handlers.append(logging.StreamHandler(sys.stdout))
        except Exception:
            pass
    prefix = ("[%s] " % tag) if tag else ""
    logging.basicConfig(
        level=logging.INFO,
        format="%%(asctime)s [%%(levelname)s] %s%%(message)s" % prefix,
        handlers=handlers,
    )


class _TrayState:
    """托盘主进程状态。"""

    def __init__(self):
        self.icon = None
        self.pipe = None                # 与窗口子进程的通信端
        self.proc = None                # 窗口子进程
        self.srv = None                 # 单实例监听 socket
        self.stop_event = threading.Event()
        self.refresh_wake = threading.Event()
        self.refresh_lock = threading.Lock()
        self.pipe_lock = threading.Lock()   # Pipe.send 的跨线程互斥
        self.interval_secs = 300
        self.child_exiting = False


TRAY = _TrayState()


# ---------- 今日统计（tooltip / 气泡用；只读数据库） ----------
def today_stats():
    """今日请求量与估算成本。

    返回 {"requests": N, "cost": X}；查询失败返回 {"error": "..."}。
    成本公式（与 Web 版一致，元/百万 token）：
      cost = (input - cache_read) * 输入价 + cache_read * 缓存读取价 + output * 输出价
    """
    try:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since_ms = int(midnight.timestamp() * 1000)   # 今日本地 00:00 的 epoch 毫秒
        uri = DB_PATH.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA query_only=1")
            rows = conn.execute(
                "SELECT model_id, COUNT(*), COALESCE(SUM(input_tokens),0), "
                "COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read_input_tokens),0) "
                "FROM model_usage WHERE started_at >= ? GROUP BY model_id",
                (since_ms,),
            ).fetchall()
        finally:
            conn.close()
        n = 0
        cost = 0.0
        for model_id, cnt, it, ot, cr in rows:
            pin, pcr, pout = PRICES.get(model_id, (0.0, 0.0, 0.0))
            n += cnt
            cost += ((it - cr) * pin + cr * pcr + ot * pout) / 1000000.0
        return {"requests": n, "cost": round(cost, 2)}
    except Exception as e:  # noqa: BLE001
        _log.exception("今日统计查询失败")
        return {"error": "%s: %s" % (type(e).__name__, e)}


def tooltip_text():
    st = today_stats()
    if "error" in st:
        return "AI Agent 监控台\n数据读取失败"
    return ("AI Agent 监控台\n今日请求 %d · 估算 ¥%.2f" % (st["requests"], st["cost"]))[:TOOLTIP_MAX]


# ---------- 托盘图标（PIL 运行时生成） ----------
def build_icon_image():
    """深蓝圆角方块 + 亮蓝三柱；同步保存 monitor.ico 供快捷方式使用。异常时纯色兜底。"""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((6, 6, 250, 250), radius=52, fill=(27, 36, 56, 255))   # #1b2438
        bar = (79, 140, 255, 255)                                                  # #4f8cff
        d.rounded_rectangle((58, 146, 100, 206), radius=10, fill=bar)
        d.rounded_rectangle((108, 96, 150, 206), radius=10, fill=bar)
        d.rounded_rectangle((158, 50, 200, 206), radius=10, fill=bar)
        try:
            img.save(ICO_PATH, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
            log("托盘图标已保存：%s" % ICO_PATH)
        except Exception:
            _log.exception("monitor.ico 保存失败（不致命）")
        return img
    except Exception:
        _log.exception("托盘图标生成失败，尝试纯色兜底")
        try:
            from PIL import Image
            return Image.new("RGBA", (256, 256), (27, 36, 56, 255))
        except Exception:
            _log.exception("PIL 不可用，无法生成托盘图标")
            return None


# ---------- 数据刷新 ----------
def run_refresh():
    """调用上级目录 refresh.py。返回 (ok, 错误详情或空串)。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # 子进程经管道输出统一 UTF-8，避免本地码页歧义
    try:
        r = subprocess.run(
            [sys.executable, str(REFRESH_PY)],
            cwd=str(PARENT_DIR),
            capture_output=True,
            timeout=REFRESH_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "刷新超时（>%d 秒）" % REFRESH_TIMEOUT
    except Exception as e:  # noqa: BLE001
        return False, "无法启动刷新进程：%r" % (e,)
    if r.returncode != 0:
        tail = (r.stderr or b"").decode("utf-8", "replace").strip()
        if len(tail) > NOTIFY_MAX:
            tail = tail[-NOTIFY_MAX:]
        return False, tail or ("刷新进程退出码 %d" % r.returncode)
    return True, ""


def child_send(cmd):
    """向窗口子进程发送命令。返回是否成功。"""
    pipe = TRAY.pipe
    if pipe is None:
        return False
    try:
        with TRAY.pipe_lock:
            pipe.send(cmd)
        return True
    except Exception:
        _log.exception("发送命令 %r 失败（窗口子进程可能已退出）", cmd)
        return False


def refresh_tooltip():
    icon = TRAY.icon
    if icon is None:
        return
    try:
        icon.title = tooltip_text()
        log("托盘提示已更新")
    except Exception:
        _log.exception("更新托盘提示失败")


def refresh_once(reason):
    """执行一次刷新（防重入）。成功静默 + 通知子进程重载页面；失败弹气泡。"""
    if not TRAY.refresh_lock.acquire(blocking=False):
        log("刷新已在进行，跳过本次请求（%s）" % reason)
        return
    try:
        t0 = time.monotonic()
        ok, detail = run_refresh()
        elapsed = time.monotonic() - t0
        if ok:
            log("刷新成功（%s，耗时 %.1fs）" % (reason, elapsed))
            refresh_tooltip()
            if child_send("RELOAD"):
                log("已通知窗口重载最新数据")
        else:
            log("刷新失败（%s，耗时 %.1fs）：%s" % (reason, elapsed, detail))
            notify_user("数据刷新失败", detail or "未知错误")
    finally:
        TRAY.refresh_lock.release()


def auto_refresh_loop():
    log("自动刷新线程运行中（当前间隔 %d 秒）" % TRAY.interval_secs)
    while not TRAY.stop_event.is_set():
        iv = TRAY.interval_secs
        if iv <= 0:
            # 自动刷新已关闭：挂起，直到间隔被重新设置
            TRAY.refresh_wake.wait()
            TRAY.refresh_wake.clear()
            continue
        fired = TRAY.refresh_wake.wait(iv)
        TRAY.refresh_wake.clear()
        if TRAY.stop_event.is_set():
            break
        if fired:
            # 间隔被切换：立即按新间隔重新计时（不触发刷新）
            continue
        refresh_once("auto")
    log("自动刷新线程退出")


# ---------- 托盘 ----------
def notify_user(title, message):
    icon = TRAY.icon
    if icon is None:
        log("托盘不可用，气泡未发送：%s | %s" % (title, message))
        return
    try:
        if not icon.HAS_NOTIFICATION:
            log("当前平台不支持气泡通知：%s | %s" % (title, message))
            return
        icon.notify((message or "")[:NOTIFY_MAX], title)
    except Exception:
        _log.exception("气泡通知失败")


def set_interval(secs):
    TRAY.interval_secs = secs
    TRAY.refresh_wake.set()      # 立即生效：唤醒计时循环
    log("自动刷新间隔切换为 %d 秒" % secs)
    update_menu()


def update_menu():
    icon = TRAY.icon
    if icon is None:
        return
    try:
        icon.update_menu()
    except Exception:
        _log.exception("更新托盘菜单失败")


def tray_notify(icon, item):
    st = today_stats()
    if "error" in st:
        notify_user("今日用量", "数据读取失败：%s" % st["error"])
    else:
        notify_user("今日用量", "今日请求 %d 次 · 估算 ¥%.2f" % (st["requests"], st["cost"]))


def tray_open_dir(icon, item):
    try:
        os.startfile(str(PARENT_DIR))
    except Exception:
        try:
            subprocess.Popen(["explorer", str(PARENT_DIR)])
        except Exception:
            _log.exception("打开数据目录失败")


def tray_exit(icon, item):
    """托盘菜单"退出"：通知子进程退出 → 停托盘 → 主流程 join 清理。"""
    if TRAY.child_exiting:
        return
    TRAY.child_exiting = True
    log("退出：托盘菜单请求")
    TRAY.stop_event.set()
    TRAY.refresh_wake.set()
    child_send("EXIT")
    try:
        icon.stop()               # 从消息循环线程内调用是 pystray 标准用法
    except Exception:
        _log.exception("icon.stop 失败")


def _interval_item(label, secs):
    # 工厂函数闭包绑定 secs，避免循环变量共享
    # 注意：pystray 的 checked 回调签名是单参 (item)，action 才是 (icon, item)；
    # 签名写错会让 icon.run 在 _create_menu 时抛 TypeError（曾导致托盘静默失效）
    return pystray.MenuItem(
        label,
        lambda icon, item: set_interval(secs),
        radio=True,
        checked=lambda item: TRAY.interval_secs == secs,
    )


def build_menu():
    return pystray.Menu(
        pystray.MenuItem("显示监控台", lambda icon, item: child_send("SHOW"), default=True),
        pystray.MenuItem(
            "立即刷新",
            lambda icon, item: threading.Thread(
                target=refresh_once, args=("manual",), daemon=True
            ).start(),
        ),
        pystray.MenuItem(
            "自动刷新间隔",
            pystray.Menu(*[_interval_item(label, secs) for label, secs in INTERVAL_CHOICES]),
        ),
        pystray.MenuItem("今日用量气泡", tray_notify),
        pystray.MenuItem("打开数据目录", tray_open_dir),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", tray_exit),
    )


def tray_setup(icon):
    """pystray run(setup) 回调：显示图标。

    Shell_NotifyIcon 的返回值已由 patch_shell_notify 记录——
    NIM_ADD 返回 True 即 explorer 已接受注册（图标必然可见）。
    """
    try:
        icon.visible = True
    except Exception:
        _log.exception("托盘显示失败")
    log("托盘就绪")


def patch_shell_notify():
    """为 Shell_NotifyIcon 加返回值检查：失败打日志并重试一次。

    pystray 自身不检查 Shell_NotifyIcon 的 BOOL 返回值，NIM_ADD 静默失败时
    图标不显示且无任何日志（本应用曾因此出现"托盘无图标"缺陷）。
    """
    try:
        from pystray import _win32 as _pw
        orig = _pw.win32.Shell_NotifyIcon

        def checked(code, data):
            r = orig(code, data)
            if not r:
                _log.warning("Shell_NotifyIcon(%s) 返回失败，1 秒后重试一次", code)
                time.sleep(1)
                r = orig(code, data)
                _log.warning("Shell_NotifyIcon(%s) 重试结果: %s", code, bool(r))
            elif code == 0:      # NIM_ADD：图标注册成功的 API 级证据
                log("Shell_NotifyIcon(NIM_ADD) 成功——托盘图标已注册")
            return r

        _pw.win32.Shell_NotifyIcon = checked
        log("Shell_NotifyIcon 返回值检查已启用")
    except Exception:
        _log.exception("Shell_NotifyIcon 补丁失败（不影响功能）")


def start_tray():
    icon_img = build_icon_image()
    if icon_img is None:
        log("托盘图标不可用，跳过托盘（窗口功能不受影响）")
        return None
    TRAY.icon = pystray.Icon(
        "agent-monitor", icon=icon_img, title=tooltip_text(), menu=build_menu()
    )
    try:
        # 预构建菜单：让 checked/action 签名错误在这里显式暴露，
        # 而不是等 icon.run 的 _mark_ready 内部炸掉后静默失去托盘
        TRAY.icon.update_menu()
    except Exception as e:
        _log.exception("托盘菜单构建失败")
        error_dialog("托盘菜单初始化失败：%r\n\n详情见 desktop\\app.log" % (e,))
        return None
    log("托盘初始化完成（icon.run 将在主线程运行）")
    return TRAY.icon


# ---------- 窗口子进程 ----------
def on_loaded(state):
    state.window_ready.set()
    log("页面加载完成：%s" % HTML_PATH.name)


def on_closing(state):
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


class _WindowState:
    def __init__(self):
        self.window = None
        self.window_ready = threading.Event()
        self.exiting = False


def _window_cmd_loop(state, pipe):
    """webview.start 回调（子线程）：接收主进程命令 SHOW / RELOAD / EXIT。"""
    while True:
        try:
            if not pipe.poll(0.3):
                continue
            cmd = pipe.recv()
        except (EOFError, OSError):
            log("[win] 与主进程的管道断开（主进程退出），窗口随之退出")
            state.exiting = True
            try:
                state.window.destroy()
            except Exception:
                pass
            return
        except Exception:
            _log.exception("[win] 命令接收异常")
            continue
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
            return


def window_process_main(pipe):
    """窗口子进程入口（spawn）：创建 webview 窗口并服务命令循环。

    正常返回 0（收到 EXIT 或主进程管道关闭）；窗口创建失败返回 1。
    """
    setup_logging("win")
    log("[win] 窗口子进程启动（python=%s）" % sys.version.split()[0])
    state = _WindowState()

    try:
        window = webview.create_window(
            WINDOW_TITLE,
            HTML_PATH.as_uri(),
            width=1280,
            height=820,
            min_size=(960, 600),
        )
    except Exception as e:  # noqa: BLE001
        _log.exception("[win] 创建窗口失败")
        log("[win] FATAL: %r（可能缺少 Microsoft Edge WebView2 Runtime）" % (e,))
        return 1

    state.window = window
    window.events.loaded += lambda: on_loaded(state)
    window.events.closing += lambda: on_closing(state)

    threading.Thread(
        target=_window_cmd_loop, args=(state, pipe), daemon=True, name="cmd-loop"
    ).start()

    try:
        webview.start()
    except Exception:
        _log.exception("[win] GUI 主循环异常退出")
        return 1
    log("[win] GUI 主循环结束，窗口子进程退出")
    return 0 if state.exiting else 0


def child_watch_loop(proc):
    """监控窗口子进程：意外死亡时提示并停托盘，让整个应用干净退出。"""
    code = proc.join()
    if TRAY.child_exiting or TRAY.stop_event.is_set():
        return
    log("窗口子进程意外退出（exitcode=%r）" % (proc.exitcode,))
    notify_user("监控台提示", "监控台窗口已退出，应用即将关闭。如需继续使用请重新启动。")
    TRAY.stop_event.set()
    TRAY.refresh_wake.set()
    icon = TRAY.icon
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass


# ---------- 单实例 ----------
def bind_single_instance():
    """绑定 127.0.0.1:59321 并监听 SHOW 指令；绑定失败返回 None。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", SINGLE_PORT))
        srv.listen(4)
    except OSError:
        try:
            srv.close()
        except OSError:
            pass
        return None

    def accept_loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            try:
                data = conn.recv(16)
                conn.close()
            except OSError:
                continue
            if data.strip().upper().startswith(b"SHOW"):
                log("收到 SHOW 指令 → 转发给窗口子进程")
                child_send("SHOW")

    threading.Thread(target=accept_loop, daemon=True, name="single-instance").start()
    return srv


def notify_existing_instance():
    try:
        with socket.create_connection(("127.0.0.1", SINGLE_PORT), timeout=2) as c:
            c.sendall(b"SHOW")
        return True
    except OSError:
        return False


# ---------- WebView2 检测 ----------
def webview2_installed():
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


def webview2_dialog(missing):
    """WebView2 缺失提示（tkinter 弹窗 + 可选打开官方下载页）。仅在托盘主进程调用。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            if missing:
                msg = ("未检测到 Microsoft Edge WebView2 Runtime。\n\n"
                       "「AI Agent 监控台」需要 WebView2 渲染界面。\n"
                       "是否现在打开微软官方下载页？")
            else:
                msg = ("创建主窗口失败，可能缺少 Microsoft Edge WebView2 Runtime。\n\n"
                       "是否打开微软官方下载页安装后重试？")
            if messagebox.askyesno(WINDOW_TITLE, msg):
                webbrowser.open(WEBVIEW2_URL)
        finally:
            root.destroy()
    except Exception:
        _log.exception("WebView2 提示弹窗失败")


def error_dialog(msg):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            messagebox.showerror(WINDOW_TITLE, msg)
        finally:
            root.destroy()
    except Exception:
        _log.exception("错误弹窗失败")


# ---------- 冒烟模式（单进程，无托盘） ----------
def smoke_run(window):
    """webview.start 的回调（子线程）：等待 loaded → 取证 → 销毁窗口。"""
    result = {"ok": False, "title": None, "requests": None, "today": None, "errors": []}
    try:
        loaded = _SMOKE_STATE["window_ready"].wait(8)
        if not loaded:
            result["errors"].append("等待页面加载超时（8s）")
        else:
            try:
                raw = window.evaluate_js(
                    "document.title + '|' + "
                    "(typeof DATA!=='undefined'&&DATA?DATA.meta.counts.requests:'NO_DATA')"
                )
                if isinstance(raw, str) and "|" in raw:
                    title, _, tail = raw.rpartition("|")
                    result["title"] = title
                    result["requests"] = int(tail) if tail.isdigit() else tail
                else:
                    result["errors"].append("evaluate_js 返回异常：%r" % (raw,))
            except Exception as e:  # noqa: BLE001
                result["errors"].append("evaluate_js 失败：%r" % (e,))
        result["today"] = today_stats()
        result["ok"] = bool(
            result["title"] == WINDOW_TITLE
            and isinstance(result["requests"], int)
            and result["requests"] > 0
            and isinstance(result["today"], dict)
            and isinstance(result["today"].get("requests"), int)
            and result["today"]["requests"] > 0
            and not result["errors"]
        )
    except Exception as e:  # noqa: BLE001
        result["errors"].append("smoke 异常：%r" % (e,))
    finally:
        _SMOKE_STATE["result"] = result
        try:
            window.destroy()
        except Exception:
            _log.exception("smoke 销毁窗口失败")


_SMOKE_STATE = {"window_ready": threading.Event(), "result": None}


def run_smoke():
    """冒烟模式：单进程创建窗口取证后退出。返回退出码。"""
    if not HTML_PATH.exists():
        ok, detail = run_refresh()
        if not ok:
            _SMOKE_STATE["result"] = {
                "ok": False, "title": None, "requests": None,
                "today": today_stats(), "errors": ["生成监控台页面失败：%s" % detail],
            }
            return 1
    try:
        window = webview.create_window(WINDOW_TITLE, HTML_PATH.as_uri(), width=1280, height=820)
    except Exception as e:  # noqa: BLE001
        _log.exception("创建窗口失败")
        _SMOKE_STATE["result"] = {
            "ok": False, "title": None, "requests": None,
            "today": today_stats(),
            "errors": ["创建窗口失败：%r（可能缺少 WebView2 Runtime）" % (e,)],
        }
        return 1
    window.events.loaded += lambda: _SMOKE_STATE["window_ready"].set()
    webview.start(smoke_run, window)
    res = _SMOKE_STATE["result"]
    return 0 if isinstance(res, dict) and res.get("ok") else 1


# ---------- 主流程（托盘主进程） ----------
def run():
    # 单实例锁
    srv = bind_single_instance()
    if srv is None:
        ok = notify_existing_instance()
        log("已有实例运行（端口 %d），已发送 SHOW=%s，本进程静默退出" % (SINGLE_PORT, ok))
        return 0
    TRAY.srv = srv
    log("单实例锁绑定 127.0.0.1:%d" % SINGLE_PORT)

    # 成品 HTML 兜底：不存在时先跑一次 refresh.py 生成
    if not HTML_PATH.exists():
        log("成品 HTML 不存在，先执行 refresh.py 生成")
        ok, detail = run_refresh()
        if not ok:
            error_dialog("生成监控台页面失败：%s\n\n详情见 desktop\\app.log" % detail)
            return 1

    # WebView2 预检（不阻断）
    check = webview2_installed()
    if check is False:
        log("注册表检测：WebView2 Runtime 未安装")
        webview2_dialog(missing=True)
    else:
        log("WebView2 检测：%s" % ("已安装" if check else "不确定，继续尝试"))

    # 托盘（Shell_NotifyIcon 返回值检查必须在 icon.run 之前打好补丁）
    patch_shell_notify()
    icon = start_tray()
    if icon is None:
        error_dialog("托盘图标初始化失败（PIL 不可用），无法启动。\n详情见 desktop\\app.log")
        return 1

    # 窗口子进程
    parent_conn, child_conn = multiprocessing.Pipe(duplex=True)
    proc = multiprocessing.Process(
        target=window_process_main, args=(child_conn,), name="monitor-window", daemon=True
    )
    proc.start()
    child_conn.close()               # 子进程端在子进程内使用，主进程及时关闭
    TRAY.pipe = parent_conn
    TRAY.proc = proc
    log("窗口子进程已启动（pid=%s）" % proc.pid)

    # 自动刷新 + 子进程监控
    threading.Thread(target=auto_refresh_loop, daemon=True, name="auto-refresh").start()
    threading.Thread(target=child_watch_loop, args=(proc,), daemon=True, name="child-watch").start()

    # pystray 主循环：阻塞至托盘"退出"（或子进程意外退出触发 icon.stop）
    try:
        icon.run(tray_setup)
    except Exception:
        _log.exception("托盘主循环异常退出")

    # ---- 退出清理 ----
    log("托盘主循环结束，开始清理")
    TRAY.child_exiting = True
    TRAY.stop_event.set()
    TRAY.refresh_wake.set()
    child_send("EXIT")
    if proc.is_alive():
        proc.join(timeout=CHILD_JOIN_TIMEOUT)
        if proc.is_alive():
            log("窗口子进程未在 %d 秒内退出，terminate 兜底" % CHILD_JOIN_TIMEOUT)
            proc.terminate()
            proc.join(timeout=5)
    log("窗口子进程已结束（exitcode=%r）" % (proc.exitcode,))
    if TRAY.srv is not None:
        try:
            TRAY.srv.close()
        except OSError:
            pass
    return 0


def main():
    smoke = "--smoke" in sys.argv[1:]
    setup_logging("tray" if not smoke else None)
    log("=== %s 桌面版启动（smoke=%s, python=%s, exe=%s）==="
        % (WINDOW_TITLE, smoke, sys.version.split()[0], Path(sys.executable).name))
    try:
        if smoke:
            code = run_smoke()
        else:
            code = run()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    except Exception:
        _log.exception("未捕获异常")
        code = 1
    if smoke:
        res = _SMOKE_STATE["result"]
        if not isinstance(res, dict):
            res = {
                "ok": False, "title": None, "requests": None, "today": None,
                "errors": ["run() 未产出结果（提前退出，退出码 %s）" % code],
            }
        res.setdefault("ok", False)
        try:
            SMOKE_PATH.write_text(
                json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log("冒烟结果已写入 %s" % SMOKE_PATH)
        except Exception:
            _log.exception("写入冒烟结果失败")
    log("进程退出，退出码 %s" % code)
    return code


if __name__ == "__main__":
    sys.exit(main())
