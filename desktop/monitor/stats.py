# -*- coding: utf-8 -*-
"""stats — 今日统计与贴纸数据包（只读直查数据库，pytest 覆盖）。

纯函数范围：tooltip_text 等格式化函数为纯函数；today_stats/widget_stats 为
只读数据库直查（无副作用）。自桌面单一统计链改造（refresh.py sidecar）起，
二者作为桌面主路径 sidecar 缺失/损坏时的回退路径；--smoke 冒烟验证的就是
本直查链。

成本口径（与 Web 版 refresh.py/template.html 一致，元/百万 token）：
  cost = (input - cache_read) * 输入价 + cache_read * 缓存读取价 + output * 输出价
未在 PRICES 中列出的模型按 0 计（免费/未配价）。

积分口径（GAUGE 官方积分，2026-09-20 检索冻结；与 refresh.py 侧常数逐字一致，
一致性由 desktop/tests/test_stats.py 的双链漂移锁测试守护）：
  credits = (input_tokens×输入系数 + cache_read×缓存系数 + output_tokens×输出系数)
            / 1_000_000 × 峰谷系数（周一至周五本地 14:00–18:00 ×1.0，其余 ×0.5）
  系数（积分/百万 token）：GLM-5.3 → (6.9, 1.7, 24)；GLM-5.3-Flash → (2.3, 0.56, 8)。
  档位额度 lite=(2000, 10000)、pro=(12000, 60000)、max=(28000, 140000)，默认 lite。
"""
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from monitor import config

# 冻结价目（元/百万 token）：model_id -> (输入, 缓存读取, 输出)；未列出的模型按 0 计。
# 桌面主路径价目 = refresh 管线 sidecar（与网页默认价目一致）；
# 本表仅在回退时刻使用，可能滞后于网页当前价目。
PRICES: Dict[str, Tuple[float, float, float]] = {
    "GLM-5.3-Flash": (0.8, 0.23, 2.8),
    "GLM-5.3": (8.0, 2.0, 28.0),
}

DB_PATH: Path = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
TOOLTIP_MAX = 128              # Windows 托盘 tooltip 上限约 128 字符

# ---------- GAUGE 官方积分口径（2026-09-20 检索冻结） ----------
# 积分系数（积分/百万 token）：model_id -> (输入, 缓存读取, 输出)；未列出模型按 0 计。
CREDIT_COEFFS: Dict[str, Tuple[float, float, float]] = {
    "GLM-5.3": (6.9, 1.7, 24.0),
    "GLM-5.3-Flash": (2.3, 0.56, 8.0),
}
# 积分换算除数：系数语义=积分/百万 token；官方文档字面为 /10000，与官方 V2→V3
# 迁移等价关系及实测数据矛盾，工程判定取 /1e6（2026-09-20 研究判定，非官方确认）。
CREDIT_DIVISOR = 1_000_000
# 套餐档位额度（积分）：tier -> (5h 窗口额度, 周额度)；默认 lite（用户确认）。
PLAN_QUOTAS: Dict[str, Tuple[int, int]] = {
    "lite": (2000, 10000),
    "pro": (12000, 60000),
    "max": (28000, 140000),
}
DEFAULT_PLAN_TIER = "lite"
_WINDOW5H_MS = 5 * 3600 * 1000   # 5h 窗口长度（毫秒）


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


def peak_factor(ts_ms: int) -> float:
    """峰谷系数：周一至周五本地 14:00–18:00（含 14:00、不含 18:00）×1.0，其余 ×0.5。

    官方高峰定义为 UTC+8；本产品假设本机时区=UTC+8。
    """
    local = datetime.fromtimestamp(ts_ms / 1000.0)
    if local.weekday() >= 5:          # weekday(): 周一=0 … 周六=5、周日=6
        return 0.5
    return 1.0 if 14 <= local.hour < 18 else 0.5


def credits_of(model_id: str, it: int, ot: int, cr: int, ts_ms: int) -> float:
    """单请求积分 =（输入×输入系数 + 缓存读取×缓存系数 + 输出×输出系数）/1e6 ×峰谷系数。

    按官方公式逐行计；与 _cost_of 不同，不做 cache_read 钳制（积分口径与官方
    计费公式逐字对齐，含脏数据时的放大）；未列出模型按 0 计。系数语义=积分/
    百万 token（除数取值的工程判定依据见 CREDIT_DIVISOR 注释）。
    """
    ci, cc, co = CREDIT_COEFFS.get(model_id, (0.0, 0.0, 0.0))
    return (it * ci + cr * cc + ot * co) / CREDIT_DIVISOR * peak_factor(ts_ms)


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
    """贴纸数据包：今日 KPI + 近 7 天每日聚合 + 最近 24h 错误 + GAUGE 积分窗口。

    全部只读；失败返回 {"error": "..."}（贴纸侧显示占位）。
    返回末尾追加 plan/window5h/thisweek（官方积分口径，与 refresh.py sidecar
    同契约）：credits/used_pct 保留 1 位小数，used_pct 可 >100（前端钳制显示），
    reset_eta_min 为 int 或 null（窗口为空）；window5h/thisweek 各含 tokens
    {"in","cr","out"}（窗口内原始 token 累计，键与 refresh 侧完全一致）。
    """
    try:
        t = today_stats()
        if "error" in t:
            return {"error": t["error"]}
        # ---- 档位与积分窗口（时间锚点全部取自本地时钟）----
        tier = config.load_plan_tier()
        if tier not in PLAN_QUOTAS:       # 防御：配置层已归一，此处兜底
            tier = DEFAULT_PLAN_TIER
        w5h_limit, week_limit = PLAN_QUOTAS[tier]
        now_ms = int(time.time() * 1000)
        win5_start_ms = now_ms - _WINDOW5H_MS            # now-5h
        monday0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        monday0 = monday0 - timedelta(days=monday0.weekday())   # 本自然周周一 00:00（本地）
        week_start_ms = int(monday0.timestamp() * 1000)
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
            # 积分窗口：逐行取原始 token（peak 系数按行判定，不能先聚合）
            plan_rows = conn.execute(
                "SELECT model_id, started_at, input_tokens, output_tokens, "
                "cache_read_input_tokens FROM model_usage WHERE started_at >= ?",
                (min(win5_start_ms, week_start_ms),),
            ).fetchall()
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
        # ---- GAUGE 积分窗口聚合（逐行计，peak 系数按行判定）----
        win5_credits = 0.0
        week_credits = 0.0
        win5_tin = win5_tcr = win5_tout = 0
        week_tin = week_tcr = week_tout = 0
        earliest5: Optional[int] = None
        for mid, ts, it, ot, cr in plan_rows:
            it, ot, cr = it or 0, ot or 0, cr or 0
            c = credits_of(mid, it, ot, cr, ts)
            if ts >= week_start_ms:
                week_credits += c
                week_tin += it
                week_tcr += cr
                week_tout += ot
            if ts >= win5_start_ms:
                win5_credits += c
                win5_tin += it
                win5_tcr += cr
                win5_tout += ot
                if earliest5 is None or ts < earliest5:
                    earliest5 = ts
        reset_eta_min: Optional[int] = None
        if earliest5 is not None:
            # 窗口内最早请求 + 5h 距现在的分钟数；四舍五入取整（半进位，整数毫秒运算避浮点边界）
            diff_ms = earliest5 + _WINDOW5H_MS - now_ms
            reset_eta_min = (diff_ms + 30_000) // 60_000
        return {
            "today": t,
            "week": week,
            "last_error": last_error,
            "generated_at": datetime.now().strftime("%H:%M:%S"),
            "plan": {"tier": tier, "window5h_limit": w5h_limit, "week_limit": week_limit},
            "window5h": {"credits": round(win5_credits, 1),
                         "used_pct": round(win5_credits / w5h_limit * 100, 1),
                         "reset_eta_min": reset_eta_min,
                         "tokens": {"in": win5_tin, "cr": win5_tcr, "out": win5_tout}},
            "thisweek": {"credits": round(week_credits, 1),
                         "used_pct": round(week_credits / week_limit * 100, 1),
                         "tokens": {"in": week_tin, "cr": week_tcr, "out": week_tout}},
        }
    except Exception as e:  # noqa: BLE001
        return {"error": "%s: %s" % (type(e).__name__, e)}


def tooltip_text(stats: Optional[Dict[str, Any]] = None) -> str:
    """生成托盘悬停提示：今日请求与估算成本；查询失败显示占位文案。"""
    st = stats if stats is not None else today_stats()
    if "error" in st:
        return "GAUGE 衡 · AI Agent 监控台\n数据读取失败"
    text = "GAUGE 衡 · AI Agent 监控台\n今日请求 %d · 估算 ¥%.2f" % (st["requests"], st["cost"])
    return text[:TOOLTIP_MAX]
