# -*- coding: utf-8 -*-
"""config — 贴纸配置文件（desktop/widget.json）的读写（纯函数，pytest 覆盖）。

主进程是唯一写者（tmp+os.replace 原子写）；损坏文件自愈为默认值。
"""
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 贴纸配置路径必须在「模块导入时」确定：load_widget_cfg/save_widget_cfg 的默认
# 参数在此刻绑定到 WIDGET_CFG_PATH，运行时 monkeypatch 无法修正。
#
# 源码形态：desktop/widget.json（本文件在 desktop/monitor/ 下，parent.parent=desktop）。
# 冻结形态：PyInstaller 把 PYZ 内模块的 __file__ 指到 _MEIPASS（临时解包目录，退出即删），
#   若沿用 __file__ 派生，配置每次重启都会丢失。故冻结时改用 exe 所在目录
#   （与 monitor.appenv.APP_DIR 同源），保证位置/透明度/档位/显隐持久化。
if getattr(sys, "frozen", False):
    WIDGET_CFG_PATH: Path = Path(sys.executable).resolve().parent / "widget.json"
else:
    WIDGET_CFG_PATH = Path(__file__).resolve().parent.parent / "widget.json"
WIDGET_DEFAULT_POS = (1400, 140)

# 贴纸内设置 popover 与托盘菜单共用的合法档位。配置文件历史版本仍可
# 读取 0.3~1.0 的任意透明度；来自网页桥的更新必须经过本白名单，避免
# 子进程把任意字段或不可序列化值带入主进程配置写入链。
WIDGET_SETTING_FIELDS = frozenset(("opacity", "pinned", "passthrough"))
WIDGET_OPACITY_VALUES = (0.60, 0.75, 0.90)

DEFAULTS: Dict[str, Any] = {
    "visible": True, "passthrough": False, "pinned": False, "opacity": 0.75,
    "x": WIDGET_DEFAULT_POS[0], "y": WIDGET_DEFAULT_POS[1],
}

# ---------- 贴纸档位（GAUGE 窗口积分口径，2026-09-20 冻结） ----------
# 合法档位；额度见 monitor.stats.PLAN_QUOTAS（lite=(2000, 10000)）。
PLAN_TIERS: Tuple[str, ...] = ("lite", "pro", "max")
DEFAULT_PLAN_TIER = "lite"


def validate_widget_settings(settings: Any) -> Optional[Dict[str, Any]]:
    """验证网页桥传入的贴纸设置并返回净化后的部分更新。

    只接受 ``opacity``、``pinned``、``passthrough`` 三个字段。透明度必须
    是 UI 暴露的三个档位，两个开关必须是 JSON boolean；未知字段、空的
    非字典 payload、NaN/Infinity 和 Python 中 ``bool`` 冒充数字均拒绝。
    ``None`` 表示拒绝，空字典表示合法的 no-op。
    """
    if not isinstance(settings, dict):
        return None
    if any(key not in WIDGET_SETTING_FIELDS for key in settings):
        return None
    out: Dict[str, Any] = {}
    if "opacity" in settings:
        value = settings["opacity"]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not any(abs(float(value) - choice) < 1e-9
                           for choice in WIDGET_OPACITY_VALUES)):
            return None
        out["opacity"] = float(value)
    for key in ("pinned", "passthrough"):
        if key in settings:
            if not isinstance(settings[key], bool):
                return None
            out[key] = settings[key]
    return out


def widget_settings_from_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """取出可经 WIDGET_CFG/设置反馈通道传给贴纸页面的三个字段。"""
    return {
        "opacity": float(cfg.get("opacity", 0.75)),
        "pinned": bool(cfg.get("pinned", False)),
        "passthrough": bool(cfg.get("passthrough", False)),
    }


def norm_plan_tier(v: Any) -> str:
    """档位归一化：合法值原样返回；缺失/非字符串/非法值一律回退 "lite"（自愈语义）。"""
    return v if isinstance(v, str) and v in PLAN_TIERS else DEFAULT_PLAN_TIER


def load_widget_cfg(path: Path = WIDGET_CFG_PATH) -> Dict[str, Any]:
    """读取贴纸配置；缺失/损坏时返回默认值（自愈）。"""
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg["visible"] = bool(raw.get("visible", True))
            cfg["passthrough"] = bool(raw.get("passthrough", False))
            cfg["pinned"] = bool(raw.get("pinned", False))
            o = raw.get("opacity", 0.75)
            cfg["opacity"] = min(1.0, max(0.3, float(o))) if isinstance(o, (int, float)) else 0.75
            x, y = raw.get("x"), raw.get("y")
            if isinstance(x, int) and isinstance(y, int):
                cfg["x"], cfg["y"] = x, y
            # 档位：仅当文件携带合法值时透出（缺省/非法不落键，读方经 load_plan_tier
            # /norm_plan_tier 兜底 "lite"；不透出可保持旧键集向后兼容）
            pt = raw.get("plan_tier")
            if isinstance(pt, str) and pt in PLAN_TIERS:
                cfg["plan_tier"] = pt
    except FileNotFoundError:
        pass
    except Exception:
        pass  # 损坏文件自愈为默认值（调用方负责日志）
    return cfg


def save_widget_cfg(cfg: Dict[str, Any], path: Path = WIDGET_CFG_PATH) -> bool:
    """原子写（tmp+os.replace）贴纸配置。返回是否成功。"""
    try:
        payload: Dict[str, Any] = {
            "visible": bool(cfg.get("visible", True)),
            "passthrough": bool(cfg.get("passthrough", False)),
            "pinned": bool(cfg.get("pinned", False)),
            "opacity": float(cfg.get("opacity", 0.75)),
            "x": int(cfg.get("x", WIDGET_DEFAULT_POS[0])),
            "y": int(cfg.get("y", WIDGET_DEFAULT_POS[1])),
        }
        # 档位：cfg 携带才落盘（主进程唯一写者；非法值归一为 "lite" 自愈）。
        # traycore._persist_widget_cfg 等所有保存路径均已随 TRAY.widget_plan_tier
        # 携带档位（贴纸批次 2 接线），切档位后不会被任何托盘/位置保存覆盖。
        if "plan_tier" in cfg:
            payload["plan_tier"] = norm_plan_tier(cfg.get("plan_tier"))
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load_plan_tier(path: Optional[Path] = None) -> str:
    """读取贴纸档位；文件缺失/损坏/键缺失/非法一律返回 "lite"（默认/自愈）。

    默认路径在调用时解析（而非默认参数绑定），保证测试可 monkeypatch WIDGET_CFG_PATH。
    """
    p = Path(path) if path is not None else WIDGET_CFG_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return DEFAULT_PLAN_TIER
    if isinstance(raw, dict):
        return norm_plan_tier(raw.get("plan_tier"))
    return DEFAULT_PLAN_TIER
