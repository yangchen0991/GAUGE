#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — GAUGE 衡 · AI Agent 监控台 Windows 桌面壳（v1.2.0 入口层，双进程架构）

为什么双进程：pystray 与 pywebview 同进程共存时，Shell_NotifyIcon(NIM_ADD) 在
pywebview（pythonnet/CLR）环境中会静默失败（实测注册表 NotifyIconSettings 无条目），
而独立进程同样用法注册成功。故按 pywebview 官方 pystray 示例的结构拆分：

- 托盘主进程（本进程）：pystray icon.run() 占用主线程（pystray 设计场景）
  · 单实例锁（127.0.0.1:59321，monitor.singleinst）
  · 托盘菜单：显示监控台 / 立即刷新 / 自动刷新间隔 / 运行状态 / 桌面贴纸开关 /
    贴纸设置（鼠标穿透、透明度、档位 Lite·Pro·Max）/ 今日用量气泡 / 官方用量页 /
    打开数据目录 / 退出
  · 自动刷新线程（Event.wait 循环调用上级目录 refresh.py，只读数据库，
    monitor.refreshctl）
  · 贴纸/托盘统计数据：优先消费 refresh.py 随成品 HTML 写出的 sidecar
    （AI-Agent监控台.data.json，单一统计链，与网页同源同价目）；
    sidecar 缺失/损坏时回退 monitor.stats 只读直查数据库
    （--smoke 冒烟验证的就是 stats 直查链）
  · 对 Shell_NotifyIcon 的每次调用记录返回值（NIM_ADD 失败时自动重试一次并留日志）
- 窗口子进程（spawn）：两个 webview 窗口（入口 monitor.winchild.window_process_main；
  必须位于可导入模块——frozen 形态下 spawn 按模块路径反序列化 target，
  定义在 __main__ 会让子进程引导卡住）
  · 主面板：AI-Agent监控台.html；关闭窗口 = 隐藏到托盘（closing return False）
  · 桌面贴纸：widget.html，无边框+透明+置顶，Win11 DWM Acrylic 毛玻璃
    （DWMWA_SYSTEMBACKDROP_TYPE=38→3，主题事件重设时低频幂等重打），
    鼠标穿透（WS_EX_TRANSPARENT 整窗开关）、位置记忆（widget.json，主进程唯一写者）
  · 通过 Pipe 接收主进程命令：SHOW / RELOAD / EXIT / WIDGET_SHOW / WIDGET_HIDE /
    WIDGET_CFG（穿透+透明度）/ WIDGET_DATA（统计数据注入渲染）
- 退出：托盘菜单"退出" → 发 EXIT → 子进程销毁两窗口退出 → 主进程 join 后 icon.stop()
- --smoke：冒烟模式（单进程，无托盘、无单实例锁），结果写 desktop/smoke_result.json

monitor 包模块布局（依赖无环，批次3a 拆分）：
  appenv（路径/常量/轮转日志）← singleinst（单实例锁）/ dialogs（预检与弹窗）
  ← traycore（托盘状态/图标/通知/子进程发送）← refreshctl（刷新编排）；
  winchild（窗口子进程：appenv + config + pin_desktop）。
  本文件只保留：菜单 actions、build_menu/start_tray、run() 主流程、
  --smoke 冒烟与 main()。
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

# ---- PyInstaller 冻结环境：multiprocessing spawn 子进程引导必须最早接管 ----
# frozen 子进程由本处 freeze_support 引导后，反序列化
# monitor.winchild.window_process_main 作为 spawn target（必须可被子进程 import）
if getattr(sys, "frozen", False):
    import multiprocessing
    multiprocessing.freeze_support()

import json
import logging
import multiprocessing
import subprocess
import threading
import time
from pathlib import Path

from monitor.appenv import (
    CHILD_JOIN_TIMEOUT, HTML_PATH, INTERVAL_CHOICES, PARENT_DIR, SINGLE_PORT,
    SMOKE_PATH, WIDGET_OPACITY_CHOICES, WINDOW_TITLE, log, setup_logging,
)
from monitor.config import (
    load_plan_tier, load_widget_cfg, validate_widget_settings,
)
from monitor.dialogs import error_dialog, webview2_dialog, webview2_installed
from monitor.refreshctl import auto_refresh_loop, load_sidecar, refresh_once, run_refresh
from monitor.singleinst import bind_single_instance, notify_existing_instance
from monitor.stats import today_stats, tooltip_text, widget_stats
from monitor.traycore import (
    TRAY, _persist_widget_cfg, apply_widget_cfg, build_icon_image, child_send,
    notify_user, tray_runtime_status,
)
from monitor.winchild import USAGE_PAGE_URL, window_process_main

import pystray
import webview

_log = logging.getLogger("agent_monitor")


# ---------- 托盘间隔/菜单辅助 ----------
def set_interval(secs):
    """切换自动刷新间隔并立即生效（唤醒计时线程按新间隔重排）。"""
    TRAY.interval_secs = secs
    TRAY.refresh_wake.set()      # 立即生效：唤醒计时循环
    log("自动刷新间隔切换为 %d 秒" % secs)
    update_menu()


def update_menu():
    """让 pystray 重建菜单（radio/checked 状态由动态回调结果决定）。"""
    icon = TRAY.icon
    if icon is None:
        return
    try:
        icon.update_menu()
    except Exception:
        _log.exception("更新托盘菜单失败")


def tray_notify(icon, item):
    """托盘菜单「今日用量气泡」：优先 sidecar 今日包，缺失回退 stats 直查
    （与 refresh_tooltip 同模式——桌面单一统计链的最后一条直查链改造）。"""
    sc = load_sidecar()
    if sc is not None:
        st = sc["today"]
        log("今日用量气泡（sidecar）")
    else:
        st = today_stats()
        log("sidecar 不可用，今日用量气泡回退直查")
    if "error" in st:
        notify_user("今日用量", "数据读取失败：%s" % st["error"])
    else:
        notify_user("今日用量", "今日请求 %d 次 · 估算 ¥%.2f" % (st["requests"], st["cost"]))


def tray_open_dir(icon, item):
    """托盘菜单「打开数据目录」：在资源管理器中打开 agent-monitor 目录。"""
    try:
        os.startfile(str(PARENT_DIR))
    except Exception:
        try:
            subprocess.Popen(["explorer", str(PARENT_DIR)])
        except Exception:
            _log.exception("打开数据目录失败")


def tray_open_usage(icon, item):
    """托盘菜单「官方用量页」：默认浏览器打开 Coding Plan 用量页（与贴纸 ↗ 同 URL）。"""
    try:
        os.startfile(USAGE_PAGE_URL)
    except Exception:
        _log.exception("打开官方用量页失败：%s" % USAGE_PAGE_URL)


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


def toggle_widget(icon, item):
    """托盘「桌面贴纸」开关：显隐贴纸窗口并持久化配置。"""
    TRAY.widget_visible = not TRAY.widget_visible
    child_send(("WIDGET_SHOW" if TRAY.widget_visible else "WIDGET_HIDE", None))
    _persist_widget_cfg()
    update_menu()
    log("桌面贴纸切换为 %s" % ("显示" if TRAY.widget_visible else "隐藏"))


def toggle_passthrough(icon, item):
    """托盘「鼠标穿透」开关：切换整窗点击穿透并持久化配置（开启时气泡提示）。"""
    TRAY.widget_passthrough = not TRAY.widget_passthrough
    child_send(("WIDGET_CFG", _widget_cfg_payload()))
    _persist_widget_cfg()
    update_menu()
    log("贴纸鼠标穿透切换为 %s" % ("开" if TRAY.widget_passthrough else "关"))
    if TRAY.widget_passthrough:
        notify_user("贴纸提示", "已开启鼠标穿透：贴纸不再响应点击，从托盘菜单「贴纸设置」关闭穿透后方可交互。")


def toggle_pinned(icon, item):
    """托盘「钉桌面模式」开关：请求子进程把贴纸 SetParent 到桌面 WorkerW 层。

    结果由子进程异步回传（WIDGET_PIN_RESULT）驱动 checked/持久化；
    与置顶互斥（钉住时 HWND_BOTTOM 清除 TOPMOST）。失败时子进程回传
    ok=False，本侧回滚并气泡提示。
    """
    child_send(("WIDGET_PIN", not TRAY.widget_pinned))


def set_widget_opacity(value):
    """设置贴纸透明度（托盘菜单三档单选）并持久化到 widget.json。"""
    TRAY.widget_opacity = value
    child_send(("WIDGET_CFG", _widget_cfg_payload()))
    _persist_widget_cfg()
    update_menu()
    log("贴纸透明度切换为 %d%%" % round(value * 100))


def _widget_cfg_payload():
    """构造主进程反馈给贴纸的完整可编辑设置集合。"""
    return {
        "opacity": TRAY.widget_opacity,
        "pinned": TRAY.widget_pinned,
        "passthrough": TRAY.widget_passthrough,
    }


def handle_widget_settings_request(settings):
    """处理窗口子进程的结构化设置请求。

    主进程在这里再次执行白名单验证并负责持久化；pinned 仍沿用原有
    WIDGET_PIN → WIDGET_PIN_RESULT 的异步原生确认链，失败时由结果分支
    回传当前真值。返回 False 表示请求被拒绝。
    """
    clean = validate_widget_settings(settings)
    if clean is None:
        log("拒绝非法贴纸设置请求：%r" % (settings,))
        child_send(("WIDGET_CFG", _widget_cfg_payload()))
        return False
    if "opacity" in clean:
        TRAY.widget_opacity = clean["opacity"]
    if "passthrough" in clean:
        TRAY.widget_passthrough = clean["passthrough"]
    if "opacity" in clean or "passthrough" in clean:
        _persist_widget_cfg()
    # 先同步已确认的配置；pinned 待原生确认后再反馈最终值。
    child_send(("WIDGET_CFG", _widget_cfg_payload()))
    if "pinned" in clean and clean["pinned"] != TRAY.widget_pinned:
        child_send(("WIDGET_PIN", clean["pinned"]))
    update_menu()
    log("已处理贴纸设置请求：%s" % clean)
    return True


def set_widget_tier(tier):
    """切换贴纸档位（Lite/Pro/Max，积分窗口额度随之变化）并持久化 plan_tier。

    走既有保存管线（_persist_widget_cfg 携带 plan_tier，任何后续保存不再丢档位），
    随后触发一次刷新：refresh.py 会按新档位重新生成 sidecar 的 plan 窗口字段，
    并经 refresh_once 的既有推送链把新数据注入贴纸。
    """
    TRAY.widget_plan_tier = tier
    _persist_widget_cfg()
    threading.Thread(target=refresh_once, args=("tier",), daemon=True,
                     name="tier-refresh").start()
    update_menu()
    log("贴纸档位切换为 %s" % tier)


def _tier_item(label, tier):
    """档位单选项工厂（radio 组；勾选态读 config.load_plan_tier()，与文件真值一致）。"""
    return pystray.MenuItem(
        label,
        lambda icon, item: set_widget_tier(tier),
        radio=True,
        checked=lambda item: load_plan_tier() == tier,
    )


def _opacity_item(label, value):
    """透明度单选项工厂（radio 组，checked 比较当前透明度档位）。"""
    return pystray.MenuItem(
        label,
        lambda icon, item: set_widget_opacity(value),
        radio=True,
        checked=lambda item: abs(TRAY.widget_opacity - value) < 0.01,
    )


def build_menu():
    """构建托盘菜单树（checked 回调均为单参 item，action 为双参 icon,item）。"""
    widget_submenu = pystray.Menu(
        pystray.MenuItem(
            "鼠标穿透",
            toggle_passthrough,
            checked=lambda item: TRAY.widget_passthrough,
        ),
        pystray.MenuItem(
            "钉桌面模式（实验）",
            toggle_pinned,
            checked=lambda item: TRAY.widget_pinned,
        ),
        pystray.MenuItem(
            "透明度",
            pystray.Menu(*[_opacity_item(label, v)
                           for label, v in WIDGET_OPACITY_CHOICES]),
        ),
        pystray.MenuItem(
            "档位",
            pystray.Menu(_tier_item("Lite", "lite"),
                         _tier_item("Pro", "pro"),
                         _tier_item("Max", "max")),
        ),
    )
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
        pystray.MenuItem("运行状态", tray_runtime_status),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("桌面贴纸", toggle_widget,
                         checked=lambda item: TRAY.widget_visible),
        pystray.MenuItem("贴纸设置", widget_submenu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("今日用量气泡", tray_notify),
        pystray.MenuItem("官方用量页", tray_open_usage),
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
    """构建托盘图标并预检菜单构建；成功返回 Icon 实例，失败返回 None（已弹窗/留日志）。"""
    icon_img = build_icon_image()
    if icon_img is None:
        log("托盘图标不可用，跳过托盘（窗口功能不受影响）")
        error_dialog("托盘图标生成失败（PIL 不可用），无法启动。\n详情见 desktop\\app.log")
        return None
    # 初始标题优先 sidecar：启动兜底刷新刚跑完时 sidecar 已存在且与网页同源
    sc0 = load_sidecar()
    TRAY.icon = pystray.Icon(
        "agent-monitor", icon=icon_img,
        title=tooltip_text(sc0["today"]) if sc0 is not None else tooltip_text(),
        menu=build_menu()
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


def child_watch_loop(proc):
    """监控窗口子进程：意外死亡时提示并停托盘，让整个应用干净退出。"""
    proc.join()
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
def child_msg_loop(pipe):
    """接收窗口子进程反向消息：贴纸位置回传 / 贴纸被页面侧隐藏。"""
    while True:
        try:
            if not pipe.poll(0.5):
                continue
            msg = pipe.recv()
        except (EOFError, OSError):
            return
        except Exception:
            _log.exception("子进程消息接收异常")
            continue
        if isinstance(msg, list) and msg[:1] == ["WIDGET_POS"] and len(msg) >= 3:
            try:
                TRAY.widget_pos = [int(msg[1]), int(msg[2])]
                _persist_widget_cfg()
                log("贴纸位置已保存：%d,%d" % (msg[1], msg[2]))
            except Exception:
                _log.exception("贴纸位置保存失败")
        elif msg == "WIDGET_STATE:hidden":
            if TRAY.widget_visible:
                TRAY.widget_visible = False
                _persist_widget_cfg()          # 页面侧隐藏也要持久化，重启后保持隐藏
                update_menu()
                log("贴纸被页面侧隐藏，托盘菜单状态已同步")
        elif isinstance(msg, list) and msg[:1] == ["WIDGET_PIN_RESULT"] and len(msg) >= 3:
            want, ok = bool(msg[1]), bool(msg[2])
            TRAY.widget_pinned = want if ok else False
            _persist_widget_cfg()
            child_send(("WIDGET_CFG", _widget_cfg_payload()))
            update_menu()
            if not ok:
                notify_user("钉桌面", "当前系统不支持钉桌面模式，已保持置顶悬浮。")
            log("钉桌面切换为 %s（结果 %s）" % ("开" if TRAY.widget_pinned else "关", ok))
        elif isinstance(msg, list) and msg[:1] == ["WIDGET_SETTINGS"] and len(msg) >= 2:
            handle_widget_settings_request(msg[1])
        elif isinstance(msg, list) and msg[:1] == ["WIDGET_CLOSED"]:
            # 贴纸窗口被销毁（Alt+F4 / WM_CLOSE）：同步显隐状态并落盘；
            # 下次托盘「桌面贴纸」开关经 WIDGET_SHOW 触发 winchild 重建窗口。
            # R4-P1-1 防御：应用退出序列中的销毁不落盘（复用既有 child_exiting/
            # stop_event 标志，未新增状态）。
            TRAY.widget_visible = False
            if TRAY.child_exiting or TRAY.stop_event.is_set():
                log("退出期间贴纸窗口关闭，跳过 widget.json 持久化")
            else:
                _persist_widget_cfg()
                update_menu()
                log("贴纸窗口已关闭，widget_visible=False 已落盘")
        elif msg == "WIDGET_REFRESH_NOW":
            # 贴纸 ↻ 按钮（winchild.WidgetApi.refresh_now）：走既有刷新链，成功后
            # refresh_once 会自动把新 sidecar/直查数据推送回贴纸。
            log("贴纸请求立即刷新（WIDGET_REFRESH_NOW）")
            threading.Thread(target=refresh_once, args=("widget",), daemon=True,
                             name="widget-refresh").start()


def run():
    """托盘主进程主流程：单实例锁 → 成品 HTML 兜底 → WebView2 预检 → 托盘初始化 →
        窗口子进程 → 初始数据推送 → 消息循环 → 退出清理。返回进程退出码。"""
    # 单实例锁（SHOW 指令经 child_send 转发给窗口子进程）
    srv = bind_single_instance(child_send)
    if srv is None:
        ok = notify_existing_instance()
        log("已有实例运行（端口 %d），已发送 SHOW=%s，本进程静默退出" % (SINGLE_PORT, ok))
        return 0
    TRAY.srv = srv
    log("单实例锁绑定 127.0.0.1:%d" % SINGLE_PORT)
    TRAY.start_monotonic = time.monotonic()

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
        # start_tray 的各失败分支已弹出准确原因（图标生成失败/菜单构建失败），此处只收口
        log("托盘初始化失败，应用退出")
        return 1

    # 窗口子进程（携带贴纸配置；主进程是 widget.json 唯一写者）。
    # spawn target 必须是可导入模块中的顶层函数（monitor.winchild.window_process_main），
    # frozen 形态下子进程按模块路径反序列化，定义在 __main__ 会导致引导卡住。
    cfg = load_widget_cfg()
    apply_widget_cfg(cfg)
    parent_conn, child_conn = multiprocessing.Pipe(duplex=True)
    proc = multiprocessing.Process(
        target=window_process_main, args=(child_conn, cfg),
        name="monitor-window", daemon=True,
    )
    proc.start()
    child_conn.close()               # 子进程端在子进程内使用，主进程及时关闭
    TRAY.pipe = parent_conn
    TRAY.proc = proc
    log("窗口子进程已启动（pid=%s，贴纸=%s）" % (proc.pid, "显示" if TRAY.widget_visible else "隐藏"))

    threading.Thread(target=child_msg_loop, args=(parent_conn,), daemon=True,
                     name="child-msg").start()
    # 初始推送：数据 + 配置（子进程 loaded 事件后由命令循环注入渲染）
    # 初始贴纸数据同样优先 sidecar（启动兜底刷新刚跑完时已存在）
    def _initial_widget_push():
        time.sleep(2)
        sc = load_sidecar()
        if sc is not None:
            child_send(("WIDGET_DATA", sc))
            log("初始贴纸数据已推送（sidecar）")
        else:
            child_send(("WIDGET_DATA", widget_stats()))
            log("初始贴纸数据：sidecar 不可用，回退直查")
        child_send(("WIDGET_CFG", {"passthrough": TRAY.widget_passthrough,
                                   "pinned": TRAY.widget_pinned,
                                   "opacity": TRAY.widget_opacity}))
        child_send(("WIDGET_PIN", TRAY.widget_pinned))

    threading.Thread(target=_initial_widget_push, daemon=True,
                     name="widget-init").start()

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
    """进程入口：解析 --smoke、初始化日志、分发主流程、写出冒烟结果并返回退出码。"""
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
