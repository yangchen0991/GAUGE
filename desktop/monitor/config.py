# -*- coding: utf-8 -*-
"""config — 贴纸配置文件（desktop/widget.json）的读写（纯函数，pytest 覆盖）。

主进程是唯一写者（tmp+os.replace 原子写）；损坏文件自愈为默认值。
"""
import json
import os
from pathlib import Path
from typing import Any, Dict

WIDGET_CFG_PATH: Path = Path(__file__).resolve().parent.parent / "widget.json"
WIDGET_DEFAULT_POS = (1400, 140)

DEFAULTS: Dict[str, Any] = {
    "visible": True, "passthrough": False, "opacity": 0.75,
    "x": WIDGET_DEFAULT_POS[0], "y": WIDGET_DEFAULT_POS[1],
}


def load_widget_cfg(path: Path = WIDGET_CFG_PATH) -> Dict[str, Any]:
    """读取贴纸配置；缺失/损坏时返回默认值（自愈）。"""
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg["visible"] = bool(raw.get("visible", True))
            cfg["passthrough"] = bool(raw.get("passthrough", False))
            o = raw.get("opacity", 0.75)
            cfg["opacity"] = min(1.0, max(0.3, float(o))) if isinstance(o, (int, float)) else 0.75
            x, y = raw.get("x"), raw.get("y")
            if isinstance(x, int) and isinstance(y, int):
                cfg["x"], cfg["y"] = x, y
    except FileNotFoundError:
        pass
    except Exception:
        pass  # 损坏文件自愈为默认值（调用方负责日志）
    return cfg


def save_widget_cfg(cfg: Dict[str, Any], path: Path = WIDGET_CFG_PATH) -> bool:
    """原子写（tmp+os.replace）贴纸配置。返回是否成功。"""
    try:
        data = json.dumps({
            "visible": bool(cfg.get("visible", True)),
            "passthrough": bool(cfg.get("passthrough", False)),
            "opacity": float(cfg.get("opacity", 0.75)),
            "x": int(cfg.get("x", WIDGET_DEFAULT_POS[0])),
            "y": int(cfg.get("y", WIDGET_DEFAULT_POS[1])),
        }, ensure_ascii=False, indent=2)
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
        return True
    except Exception:
        return False
