# -*- coding: utf-8 -*-
"""refreshctl — 数据刷新编排（托盘主进程侧，批次3a 自 app.py 拆分）。

刷新路径：源码形态子进程调用 refresh.py；冻结（exe）形态线程内 importlib
加载打包的 refresh.py。统计消费主路径是 refresh 随成品 HTML 写出的 sidecar
（单一统计链，与网页同源同价目），sidecar 缺失/损坏时回退 monitor.stats
只读直查数据库。

依赖方向：appenv ← traycore ← refreshctl（包内依赖无环）。
"""
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple

from monitor.appenv import (
    APP_DIR, NOTIFY_MAX, PARENT_DIR, REFRESH_PY, REFRESH_TIMEOUT, SIDECAR_PATH,
    log,
)
from monitor.stats import tooltip_text, widget_stats
from monitor.traycore import TRAY, child_send, notify_user

_log = logging.getLogger("agent_monitor")


def load_sidecar() -> Optional[Dict[str, Any]]:
    """读取 refresh 管线产出的桌面 sidecar JSON（桌面统计链主路径）。

    缺失/损坏/非 dict/缺 "today" 键一律返回 None（异常吞掉，调用方回退
    monitor.stats 直查数据库）。
    """
    try:
        with open(SIDECAR_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or "today" not in data:
        return None
    return data


def _run_refresh_inline() -> Tuple[bool, str]:
    """冻结（exe）形态：无外部 Python 可用，线程内加载打包的 refresh.py 执行数据管线。

    refresh.py 的 die/SystemExit 转为 (False, 输出尾部)；print 经 redirect 捕获。
    """
    import contextlib
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location("refresh_inline", REFRESH_PY)
    if spec is None or spec.loader is None:
        return False, "无法加载数据管线模块：%s" % REFRESH_PY
    mod = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            spec.loader.exec_module(mod)     # 只装载定义（__main__ 守卫不触发）
            # refresh.py 模块顶层会把 OUTPUT_PATH 设回其模块目录（_MEIPASS 只读
            # 临时区），因此必须在 exec_module 之后覆盖；且 refresh.py 以字符串
            # 方式拼接（OUTPUT_PATH + ".tmp"），必须是 str 而非 Path。
            # 覆盖为 exe 目录，保证成品 HTML 写到用户可见位置
            # （setattr 等价于 mod.OUTPUT_PATH = ...；ModuleType 动态属性走 setattr 以过 mypy）
            setattr(mod, "OUTPUT_PATH", str(APP_DIR / "AI-Agent监控台.html"))
            try:
                mod.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
    except Exception as e:  # noqa: BLE001
        return False, "数据管线异常：%r" % (e,)
    return (code == 0), buf.getvalue()


def run_refresh() -> Tuple[bool, str]:
    """执行数据刷新。源码形态：子进程调用 refresh.py；冻结形态：线程内 importlib。

    返回 (ok, 错误详情或空串)。
    """
    if getattr(sys, "frozen", False):
        try:
            return _run_refresh_inline()
        except Exception as e:  # noqa: BLE001
            return False, "内联刷新异常：%r" % (e,)
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


def refresh_tooltip() -> None:
    """数据刷新成功后刷新托盘悬停提示（优先 sidecar，缺失回退 stats 直查）。"""
    icon = TRAY.icon
    if icon is None:
        return
    try:
        sc = load_sidecar()
        if sc is not None:
            icon.title = tooltip_text(sc["today"])
            log("托盘提示已更新（sidecar）")
        else:
            icon.title = tooltip_text()
            log("sidecar 不可用，托盘提示回退直查")
    except Exception:
        _log.exception("更新托盘提示失败")


def _notify_widget_refresh_state(state: str, detail: str = "") -> None:
    """把刷新生命周期推给贴纸，页面据此保留旧数据并显式标记状态。"""
    payload: Dict[str, Any] = {"state": state}
    if detail:
        payload["detail"] = detail[:NOTIFY_MAX]
    child_send(("WIDGET_REFRESH_STATE", payload))


def refresh_once(reason: str) -> None:
    """执行一次刷新（防重入）。成功静默 + 通知子进程重载页面；失败弹气泡。"""
    if not TRAY.refresh_lock.acquire(blocking=False):
        log("刷新已在进行，跳过本次请求（%s）" % reason)
        return
    try:
        t0 = time.monotonic()
        _notify_widget_refresh_state("refreshing")
        ok, detail = run_refresh()
        elapsed = time.monotonic() - t0
        if ok:
            log("刷新成功（%s，耗时 %.1fs）" % (reason, elapsed))
            TRAY.refresh_count += 1
            TRAY.refresh_secs_total += elapsed
            refresh_tooltip()
            if child_send("RELOAD"):
                log("已通知窗口重载最新数据")
            sc = load_sidecar()
            if sc is not None:
                child_send(("WIDGET_DATA", sc))
                log("已推送贴纸数据（sidecar）")
            else:
                child_send(("WIDGET_DATA", widget_stats()))
                log("sidecar 不可用，贴纸回退直查")
        else:
            log("刷新失败（%s，耗时 %.1fs）：%s" % (reason, elapsed, detail))
            _notify_widget_refresh_state("failed", detail or "未知错误")
            notify_user("数据刷新失败", detail or "未知错误")
    finally:
        TRAY.refresh_lock.release()


def auto_refresh_loop() -> None:
    """自动刷新后台线程：按当前间隔循环触发刷新；间隔切换或退出经 Event 立即唤醒。"""
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
