# -*- coding: utf-8 -*-
"""traycore — 托盘主进程的共享状态与托盘图标能力（批次3a 自 app.py 拆分）。

承载托盘侧状态对象（TRAY）、贴纸配置应用/持久化、托盘图标绘制、
气泡通知与窗口子进程命令发送。配置真值在本进程，widget.json 是持久化镜像
（主进程唯一写者，经 monitor.config 原子写）。
"""
import logging
import os
import threading
import time
from typing import Any, Dict, Tuple, Union

from monitor.appenv import ICO_PATH, NOTIFY_MAX, log
from monitor.config import (
    DEFAULT_ACTIVE_PLATFORM, PLATFORMS, WIDGET_DEFAULT_POS,
    norm_active_platform, norm_plan_tier, save_widget_cfg,
)

_log = logging.getLogger("agent_monitor")


class _TrayState:
    """托盘主进程状态。"""

    def __init__(self) -> None:
        self.icon: Any = None
        self.pipe: Any = None                # 与窗口子进程的通信端
        self.proc: Any = None                # 窗口子进程
        self.srv: Any = None                 # 单实例监听 socket
        self.stop_event = threading.Event()
        self.refresh_wake = threading.Event()
        self.refresh_lock = threading.Lock()
        self.pipe_lock = threading.Lock()    # Pipe.send 的跨线程互斥
        # 平台切换、缓存替换和 revision 快照共用一把锁；刷新线程不在锁内做扫描。
        self.platform_lock = threading.RLock()
        self.interval_secs = 300
        self.child_exiting = False
        self.widget_pinned = False
        self.start_monotonic = 0.0           # 运行状态统计用
        self.refresh_count = 0
        self.refresh_secs_total = 0.0
        # 桌面贴纸（配置真值在本进程；widget.json 是持久化镜像）
        self.widget_visible = True
        self.widget_passthrough = False
        self.widget_opacity = 0.75
        self.widget_pos = list(WIDGET_DEFAULT_POS)
        self.widget_plan_tier = "lite"       # 贴纸档位（lite|pro|max，贴纸批次 2 新增）
        self.widget_used = False             # 用户是否启用过贴纸（首次默认开）
        self.active_platform = DEFAULT_ACTIVE_PLATFORM
        self.platform_revision = 0
        self.platform_cache: Dict[str, Any] = {platform: None for platform in PLATFORMS}
        self.pending_task_id = None
        self.continuation_pending = False
        self.continuation_signature = None
        self.continuation_due = 0.0


TRAY = _TrayState()


def apply_widget_cfg(cfg: Dict[str, Any]) -> None:
    """把载入的贴纸配置字典应用到托盘状态（启动时与 widget.json 同步）。"""
    with TRAY.platform_lock:
        TRAY.widget_visible = bool(cfg.get("visible", True))
        TRAY.widget_passthrough = bool(cfg.get("passthrough", False))
        TRAY.widget_pinned = bool(cfg.get("pinned", False))
        TRAY.widget_opacity = float(cfg.get("opacity", 0.75))
        TRAY.widget_pos = [int(cfg.get("x", WIDGET_DEFAULT_POS[0])),
                           int(cfg.get("y", WIDGET_DEFAULT_POS[1]))]
        TRAY.widget_plan_tier = norm_plan_tier(cfg.get("plan_tier"))
        TRAY.active_platform = norm_active_platform(cfg.get("active_platform"))


def _persist_widget_cfg(active_platform: Any = None) -> bool:
    """把当前托盘贴纸状态持久化到 widget.json（经由 monitor.config 原子写）。

    批次 2 起携带 plan_tier：任何贴纸配置保存路径（位置回传/显隐/穿透/透明度）
    都会带上当前档位，保证切档位后不被后续保存覆盖丢失。
    （pinned 缺失为 v1 已知缺陷，本批次按授权保持原样。）
    """
    with TRAY.platform_lock:
        platform = (TRAY.active_platform if active_platform is None
                    else norm_active_platform(active_platform))
        ok = save_widget_cfg({
            "visible": TRAY.widget_visible,
            "passthrough": TRAY.widget_passthrough,
            "pinned": TRAY.widget_pinned,
            "opacity": TRAY.widget_opacity,
            "x": TRAY.widget_pos[0],
            "y": TRAY.widget_pos[1],
            "plan_tier": norm_plan_tier(TRAY.widget_plan_tier),
            "active_platform": platform,
        })
    if not ok:
        _log.error("widget.json 写入失败")
    return ok


def platform_state() -> Dict[str, Any]:
    """返回可跨 Pipe 发送的平台快照，不暴露可变缓存对象。"""
    with TRAY.platform_lock:
        return {
            "platform": TRAY.active_platform,
            "revision": int(TRAY.platform_revision),
        }


# ---------- 托盘图标（PIL 运行时生成） ----------
def build_icon_image() -> Any:
    """GAUGE 表盘 mark：碳黑圆角方底 + 琥珀 270° 表盘弧（开口朝正下）+ 浅灰指针与轴心；
    同步保存 monitor.ico 供快捷方式使用。异常时纯色兜底。

    几何换算（brand/logo-mark.svg viewBox 96 → 256，比例 8/3）：
    底板 rect 2.5→7、rx 10→27；弧心 (48,54)→(128,144)、半径 26→69、弧宽 6→16（arc
    后手动补端圆实现 stroke-linecap:round）；指针宽 4→11；轴心 r4→11。
    性能注记：PIL._typing 会连带 import numpy，而 numpy 2.x 的 OpenBLAS 线程池
    在多核机器（本机 24 逻辑核）按核预留提交内存，实测约 +740MB Private。
    本应用不用 numpy/BLAS 数值计算，固定单线程即可消除该预留（实测 773→约 20MB）。
    必须在首次 import PIL（进而 import numpy）之前设置。
    返回 PIL Image（不可用时 None，调用方据此降级退出）。
    """
    try:
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # 底板：碳黑圆角方 + 发丝描边（#0A0A0C / #26262E）
        d.rounded_rectangle((7, 7, 249, 249), radius=27, fill=(10, 10, 12, 255),
                            outline=(38, 38, 46, 255), width=2)
        # 表盘弧：270°、开口朝正下（PIL 角度系与 SVG y 向下一一致：0=3 点钟、顺时针）
        amber = (255, 179, 0, 255)                                              # #FFB300
        d.arc((59, 75, 197, 213), start=135, end=405, fill=amber, width=16)
        for ex, ey in ((79, 193), (177, 193)):                                  # 圆头端点
            d.ellipse((ex - 8, ey - 8, ex + 8, ey + 8), fill=amber)
        # 指针（48,54→58,36.68 换算）+ 轴心，浅灰 #F2F2F0
        light = (242, 242, 240, 255)
        d.line([(128, 144), (155, 98)], fill=light, width=11)
        for ex, ey in ((128, 144), (155, 98)):                                  # 圆头端点
            d.ellipse((ex - 5, ey - 5, ex + 5, ey + 5), fill=light)
        d.ellipse((117, 133, 139, 155), fill=light)                             # 轴心 r≈11
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
            return Image.new("RGBA", (256, 256), (10, 10, 12, 255))
        except Exception:
            _log.exception("PIL 不可用，无法生成托盘图标")
            return None


def notify_user(title: str, message: str) -> None:
    """托盘气泡通知（平台不支持或托盘不可用时静默降级为日志）。"""
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


def child_send(cmd: Union[str, Tuple[Any, ...]]) -> bool:
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


def tray_runtime_status(icon: Any, item: Any) -> None:
    """托盘「运行状态」气泡：运行时长/两进程内存/刷新统计（psutil）。"""
    import psutil
    lines = []
    if TRAY.start_monotonic:
        up = time.monotonic() - TRAY.start_monotonic
        lines.append("运行时长 %d 分 %02d 秒" % (up // 60, up % 60))
    try:
        me = psutil.Process(os.getpid()).memory_info()
        lines.append("托盘进程 %.1f MB" % (me.rss / 1048576.0))
        if TRAY.proc is not None and TRAY.proc.is_alive():
            parent = psutil.Process(TRAY.proc.pid)
            ch = parent.memory_info()
            lines.append("窗口进程 %.1f MB" % (ch.rss / 1048576.0))
            # WebView2 子进程树（MSWebView2.exe 及其渲染/GPU 子进程）RSS 合计
            try:
                total = 0
                n_kids = 0
                for kid in parent.children(recursive=True):
                    try:
                        total += kid.memory_info().rss
                        n_kids += 1
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue    # 单个子进程退出/受限时跳过，不影响其余合计
                if n_kids:
                    lines.append("WebView2 子进程 %d 个 · %.1f MB"
                                 % (n_kids, total / 1048576.0))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
                pass                # 子进程树枚举失败：省略该行，不影响其他行
    except Exception:
        lines.append("内存信息不可用")
    if TRAY.refresh_count:
        lines.append("刷新 %d 次 · 平均 %.2f 秒"
                     % (TRAY.refresh_count, TRAY.refresh_secs_total / TRAY.refresh_count))
    else:
        lines.append("尚未刷新")
    notify_user("运行状态", chr(10).join(lines))
