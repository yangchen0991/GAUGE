# -*- coding: utf-8 -*-
"""stats — 今日统计与贴纸数据包（全部只读数据库；纯函数，pytest 覆盖）。

成本口径（与 Web 版 refresh.py/template.html 一致，元/百万 token）：
  cost = (input - cache_read) * 输入价 + cache_read * 缓存读取价 + output * 输出价
未在 PRICES 中列出的模型按 0 计（免费/未配价）。
"""
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 冻结价目（元/百万 token）：model_id -> (输入, 缓存读取, 输出)；未列出的模型按 0 计
PRICES: Dict[str, Tuple[float, float, float]] = {
    "GLM-5.3-Flash": (0.8, 0.23, 2.8),
    "GLM-5.3": (8.0, 2.0, 28.0),
}

DB_PATH: Path = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
TOOLTIP_MAX = 128              # Windows 托盘 tooltip 上限约 128 字符


def _open_ro() -> sqlite3.Connection:
    """只读打开会话库（mode=ro + query_only + busy_timeout，绝不写入）。"""
    uri = DB_PATH.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=1")
    return conn


def _cost_of(model_id: str, it: int, ot: int, cr: int) -> float:
    """单模型成本（元）。cache_read 超出 input 时按 input 上限钳制（防御脏数据）。"""
    cr = min(cr, it)
    pin, pcr, pout = PRICES.get(model_id, (0.0, 0.0, 0.0))
    return ((it - cr) * pin + cr * pcr + ot * pout) / 1000000.0


def today_stats() -> Dict[str, Any]:
    """今日请求量、Token 与估算成本。

    返回 {"requests": N, "cost": X, "in": A, "out": B, "cr": C}；
    查询失败返回 {"error": "..."}。
    """
    try:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since_ms = int(midnight.timestamp() * 1000)   # 今日本地 00:00 的 epoch 毫秒
        conn = _open_ro()
        try:
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
        tin = tout = tcr = 0
        for model_id, cnt, it, ot, cr in rows:
            n += cnt
            tin += it
            tout += ot
            tcr += cr
            cost += _cost_of(model_id, it, ot, cr)
        return {"requests": n, "cost": round(cost, 2), "in": tin, "out": tout, "cr": tcr}
    except Exception as e:  # noqa: BLE001
        return {"error": "%s: %s" % (type(e).__name__, e)}


def widget_stats() -> Dict[str, Any]:
    """贴纸数据包：今日 KPI + 近 7 天每日聚合 + 最近 24h 错误。

    全部只读；失败返回 {"error": "..."}（贴纸侧显示占位）。
    """
    try:
        t = today_stats()
        if "error" in t:
            return {"error": t["error"]}
        conn = _open_ro()
        try:
            week0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            week0 = week0.fromtimestamp(week0.timestamp() - 6 * 86400)
            since_ms = int(week0.timestamp() * 1000)
            rows = conn.execute(
                "SELECT date(started_at/1000,'unixepoch','localtime') AS d, model_id, "
                "COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                "COALESCE(SUM(cache_read_input_tokens),0) "
                "FROM model_usage WHERE started_at >= ? GROUP BY d, model_id",
                (since_ms,),
            ).fetchall()
            err = conn.execute(
                "SELECT error_type, started_at FROM model_usage "
                "WHERE status='error' AND error_type IS NOT NULL AND TRIM(error_type)<>'' "
                "AND started_at >= ? "
                "ORDER BY started_at DESC LIMIT 1",
                (int((time.time() - 86400) * 1000),),
            ).fetchone()
        finally:
            conn.close()
        agg: Dict[str, Dict[str, float]] = {}
        for d, model_id, cnt, it, ot, cr in rows:
            e = agg.setdefault(d, {"req": 0, "cost": 0.0})
            e["req"] += cnt
            e["cost"] += _cost_of(model_id, it, ot, cr)
        week: List[Dict[str, Any]] = []
        for i in range(7):
            day = week0.fromtimestamp(week0.timestamp() + i * 86400)
            key = day.strftime("%Y-%m-%d")
            e = agg.get(key, {"req": 0, "cost": 0.0})
            week.append({
                "d": day.strftime("%m-%d"),
                "full": key,
                "req": e["req"],
                "cost": round(e["cost"], 2),
            })
        last_error: Optional[Dict[str, Any]] = None
        if err and err[0]:
            et, ets = err
            last_error = {
                "type": str(et),
                "at": datetime.fromtimestamp((ets or 0) / 1000).strftime("%H:%M"),
            }
        return {
            "today": t,
            "week": week,
            "last_error": last_error,
            "generated_at": datetime.now().strftime("%H:%M:%S"),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": "%s: %s" % (type(e).__name__, e)}


def tooltip_text(stats: Optional[Dict[str, Any]] = None) -> str:
    """生成托盘悬停提示：今日请求与估算成本；查询失败显示占位文案。"""
    st = stats if stats is not None else today_stats()
    if "error" in st:
        return "AI Agent 监控台\n数据读取失败"
    text = "AI Agent 监控台\n今日请求 %d · 估算 ¥%.2f" % (st["requests"], st["cost"])
    return text[:TOOLTIP_MAX]
