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
from monitor.config import DEFAULT_ACTIVE_PLATFORM, is_valid_platform
from monitor.traycore import TRAY, child_send, notify_user, platform_state

_log = logging.getLogger("agent_monitor")

# 连续续读上限：Codex 活跃使用时 rollout 持续增长、签名每轮都有进展，曾导致
# 每 ~11 秒一次的无限续读链（架空 300 秒正常间隔，真机日志证实）。达到上限后
# 不再排队短间隔续读，回落正常刷新间隔，剩余 pending 由后续常规周期消化。
_CONTINUATION_MAX_ROUNDS = 3


def _unavailable_widget(platform: str, detail: str) -> Dict[str, Any]:
    """为缺失的平台数据生成明确错误包；绝不以另一平台数据冒充。"""
    return {
        "platform": platform,
        "status": "unavailable",
        "generated_at": None,
        "error": detail,
        "warnings": [detail],
    }


def _decorate_widget(data: Dict[str, Any], platform: str) -> Dict[str, Any]:
    out = dict(data)
    out["platform"] = platform
    out.setdefault("generated_at", None)
    status = out.get("status")
    if not isinstance(status, str) or status not in (
            "ok", "ready", "partial", "stale", "error", "unavailable"):
        out["status"] = "error" if out.get("error") else "ok"
    if out.get("status") in ("error", "unavailable") and not out.get("error"):
        out["error"] = "%s 数据不可用" % platform
    if out.get("error") and not isinstance(out.get("warnings"), list):
        out["warnings"] = [str(out["error"])]
    if out.get("status") == "partial" and not isinstance(out.get("warnings"), list):
        out["warnings"] = ["数据索引尚未完整"]
    return out


def _read_sidecar_raw() -> Optional[Dict[str, Any]]:
    try:
        with open(SIDECAR_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return data


def load_sidecar(platform: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """读取当前平台 sidecar；旧格式只允许作为 ZCode 数据。

    ZCode 保留缺失 sidecar 后由调用方直查的兼容路径；Codex 缺失、损坏或
    未导出时返回明确 ``unavailable`` 包，禁止静默回退到 ZCode。
    """
    if platform is None:
        with TRAY.platform_lock:
            platform = TRAY.active_platform
    if not is_valid_platform(platform):
        return _unavailable_widget(DEFAULT_ACTIVE_PLATFORM, "非法平台标识")
    raw = _read_sidecar_raw()
    if raw is None:
        return (None if platform == "zcode"
                else _unavailable_widget(platform, "Codex sidecar 缺失或无法读取"))

    bundles = raw.get("platforms")
    selected: Any = None
    if isinstance(bundles, dict):
        selected = bundles.get(platform)
        # 新 bundle 仍保留原 ZCode 顶层字段，兼容只写入 zcode 的过渡版本。
        if not isinstance(selected, dict) and platform == "zcode" and "today" in raw:
            selected = raw
    elif platform == "zcode" and "today" in raw:
        # v1 sidecar：整份内容就是 ZCode widget。
        selected = raw

    if not isinstance(selected, dict):
        if platform == "zcode":
            # v1 契约：sidecar 是合法 dict 但既无 platforms 又无 today 时，
            # 返回 None 让 current_widget 回退 widget_stats() 直查，
            # 不以 unavailable 错误包顶替直查数据。
            return None
        return _unavailable_widget(platform, "%s sidecar 未导出" % platform)
    return _decorate_widget(selected, platform)


def current_widget(platform: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """取得一个平台的缓存/sidecar/兼容回退数据包。"""
    if platform is None:
        with TRAY.platform_lock:
            platform = TRAY.active_platform
    if not is_valid_platform(platform):
        return _unavailable_widget(DEFAULT_ACTIVE_PLATFORM, "非法平台标识")
    with TRAY.platform_lock:
        cached = TRAY.platform_cache.get(platform)
    if cached is not None and not force:
        return dict(cached)

    data = load_sidecar(platform)
    if data is None and platform == "zcode":
        try:
            data = _decorate_widget(widget_stats(), platform)
        except Exception as exc:  # noqa: BLE001
            data = _unavailable_widget(platform, "ZCode 直查失败：%r" % (exc,))
    elif data is None:
        data = _unavailable_widget(platform, "Codex 数据不可用")
    with TRAY.platform_lock:
        TRAY.platform_cache[platform] = dict(data)
    return dict(data)


def push_current_platform(force: bool = False) -> Optional[Dict[str, Any]]:
    """串行发送平台快照和对应数据，防止切换期间旧结果覆盖新平台。"""
    for _ in range(2):
        with TRAY.platform_lock:
            platform = TRAY.active_platform
            revision = int(TRAY.platform_revision)
        data = current_widget(platform, force=force)
        with TRAY.platform_lock:
            if platform != TRAY.active_platform or revision != TRAY.platform_revision:
                continue
            payload = dict(data)
            payload["platform"] = platform
            payload["platform_revision"] = revision
            # 持有平台锁完成两个有序 IPC，切换不会插入旧平台数据。
            child_send(("PLATFORM_STATE", {"platform": platform, "revision": revision}))
            child_send(("WIDGET_DATA", payload))
            return payload
    return None


def platform_tooltip(data: Dict[str, Any], platform: str) -> str:
    """托盘/启动标题的无副作用提示；Codex 严禁复用 ZCode 费用文案。"""
    if platform != "codex":
        today = data.get("today") if isinstance(data, dict) else None
        if isinstance(today, dict) and not data.get("error"):
            return tooltip_text(today)
        return tooltip_text()
    if data.get("status") in ("error", "unavailable"):
        return "Codex · 数据不可用"
    raw_today = data.get("today")
    today = raw_today if isinstance(raw_today, dict) else {}
    requests = today.get("requests")
    total = today.get("total")
    parts = ["Codex"]
    if isinstance(requests, (int, float)) and not isinstance(requests, bool):
        parts.append("今日 %d 次" % requests)
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        parts.append("%d tokens" % total)
    return " · ".join(parts)


def _schedule_continuation(data: Optional[Dict[str, Any]]) -> None:
    """索引未完整时请求短间隔续读；相同 coverage 不忙循环。"""
    with TRAY.platform_lock:
        if TRAY.interval_secs <= 0 or TRAY.active_platform != "codex":
            TRAY.continuation_pending = False
            TRAY.continuation_due = 0.0
            return
        coverage = data.get("coverage") if isinstance(data, dict) else None
        if not isinstance(coverage, dict):
            TRAY.continuation_pending = False
            TRAY.continuation_due = 0.0
            TRAY.continuation_signature = None
            return
        pending = coverage.get("pending_bytes") or 0
        complete = coverage.get("complete") is True
        try:
            pending = int(pending)
        except (TypeError, ValueError):
            pending = 0
        signature = (pending, coverage.get("processed_files"), coverage.get("total_files"))
        if pending > 0 and not complete:
            if TRAY.continuation_rounds >= _CONTINUATION_MAX_ROUNDS:
                # 连续续读上限已达：清 pending/due/signature，回落到正常刷新
                # 间隔，防止 Codex 活跃写入（rollout 持续增长）造成的无限续读
                # 链。已捕捉的进展不丢，剩余 pending 等下个常规周期消化。
                TRAY.continuation_pending = False
                TRAY.continuation_due = 0.0
                TRAY.continuation_signature = None
                return
            progressed = signature != TRAY.continuation_signature
            TRAY.continuation_signature = signature
            if progressed:
                TRAY.continuation_pending = True
                TRAY.continuation_due = time.monotonic() + 7.0
                TRAY.continuation_rounds += 1
                TRAY.refresh_wake.set()
        else:
            TRAY.continuation_pending = False
            TRAY.continuation_due = 0.0
            TRAY.continuation_signature = None
            TRAY.continuation_rounds = 0


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
    # pythonw（GUI 子系统）无控制台父进程，Windows 会为控制台子系统子进程分配
    # 新控制台窗口——真机上每次刷新黑窗闪烁约 3.5 秒的根因。CREATE_NO_WINDOW
    # 抑制该窗口；capture_output 已通过管道接管子进程输出，无窗口不影响日志
    # 采集。旗标常量仅 Windows 的 subprocess 存在；非 Windows 传 0 即无任何
    # 创建旗标，行为与原先完全一致。
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(
            [sys.executable, str(REFRESH_PY)],
            cwd=str(PARENT_DIR),
            capture_output=True,
            timeout=REFRESH_TIMEOUT,
            env=env,
            creationflags=creationflags,
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
    """数据刷新成功后更新托盘提示；Codex 不显示 ZCode CNY 估算。"""
    icon = TRAY.icon
    if icon is None:
        return
    try:
        with TRAY.platform_lock:
            platform = TRAY.active_platform
        data = current_widget(platform)
        if platform == "codex":
            icon.title = platform_tooltip(data, platform)
            log("托盘提示已更新（Codex）")
        elif data.get("today") is not None and not data.get("error"):
            icon.title = tooltip_text(data["today"])
            log("托盘提示已更新（sidecar/直查）")
        else:
            icon.title = tooltip_text()
            log("sidecar 不可用，托盘提示回退直查")
    except Exception:
        _log.exception("更新托盘提示失败")


def _notify_widget_refresh_state(state: str, detail: str = "") -> None:
    """把刷新生命周期推给贴纸，页面据此保留旧数据并显式标记状态。"""
    payload: Dict[str, Any] = {"state": state}
    payload.update(platform_state())
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
        if reason != "codex-continuation":
            # 常规/手动刷新执行即复位续读轮数：上限只约束一次常规刷新之后的
            # 连续追帧，新一轮常规刷新重新获得完整续读预算。
            with TRAY.platform_lock:
                TRAY.continuation_rounds = 0
        _notify_widget_refresh_state("refreshing")
        ok, detail = run_refresh()
        elapsed = time.monotonic() - t0
        if ok:
            log("刷新成功（%s，耗时 %.1fs）" % (reason, elapsed))
            TRAY.refresh_count += 1
            TRAY.refresh_secs_total += elapsed
            # 一次刷新同时更新两个平台；不能让另一平台继续显示上一轮快照。
            with TRAY.platform_lock:
                for cached_platform in list(TRAY.platform_cache):
                    TRAY.platform_cache[cached_platform] = None
            if child_send("RELOAD"):
                log("已通知窗口重载最新数据")
            pushed = push_current_platform(force=True)
            if pushed is not None:
                _schedule_continuation(pushed)
                log("已推送当前平台贴纸数据（%s/r%d）" %
                    (pushed.get("platform"), pushed.get("platform_revision", 0)))
            refresh_tooltip()
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
        with TRAY.platform_lock:
            continuation = bool(TRAY.continuation_pending and TRAY.interval_secs > 0)
            due = TRAY.continuation_due if continuation else 0.0
            iv = max(0.0, due - time.monotonic()) if continuation else TRAY.interval_secs
        if iv <= 0 and not continuation:
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
        if continuation:
            with TRAY.platform_lock:
                # 到点后才消费续读标志；相同 coverage 不会再次排队。
                TRAY.continuation_pending = False
                TRAY.continuation_due = 0.0
        refresh_once("codex-continuation" if continuation else "auto")
    log("自动刷新线程退出")
