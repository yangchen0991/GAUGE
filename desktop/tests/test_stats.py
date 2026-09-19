# -*- coding: utf-8 -*-
"""monitor.stats 纯函数与聚合逻辑的单元测试（不触真实数据库）。"""
import sqlite3
from pathlib import Path

import pytest

from monitor import stats


# ---------- 成本公式 ----------
class TestCostOf:
    def test_flash_typical(self):
        # Flash：非缓存 100万×0.8 + 缓存 90万×0.23 + 输出 10万×2.8
        v = stats._cost_of("GLM-5.3-Flash", 1_000_000, 100_000, 900_000)
        assert round(v, 4) == round((100_000 * 0.8 + 900_000 * 0.23 + 100_000 * 2.8) / 1e6, 4)

    def test_cache_read_exceeds_input_clamped_to_input(self):
        # 脏数据防御：cache_read > input 时按 input 钳制（不产生负成本）
        v = stats._cost_of("GLM-5.3-Flash", 1_000_000, 100_000, 9_000_000)
        assert v == pytest.approx((0 * 0.8 + 1_000_000 * 0.23 + 100_000 * 2.8) / 1e6)

    def test_unknown_model_zero_price(self):
        assert stats._cost_of("不存在模型", 1_000_000, 1_000_000, 1_000_000) == 0.0

    def test_cache_read_exceeds_input_clamped(self):
        # 缓存读取 > 输入：负的 input 价贡献应被钳制为 0（cr 按 input 上限截断的语义
        # 由调用方保证；此处仅验证公式本身在 cr<=it 时正确）
        v = stats._cost_of("GLM-5.3", 1_000, 100, 1_000)
        assert v == pytest.approx((0 * 8 + 1_000 * 2 + 100 * 28) / 1e6)

    def test_all_zero(self):
        assert stats._cost_of("GLM-5.3", 0, 0, 0) == 0.0


# ---------- tooltip ----------
class TestTooltip:
    def test_normal_and_truncation(self):
        t = stats.tooltip_text({"requests": 5, "cost": 1.25})
        assert t.startswith("AI Agent 监控台\n今日请求 5 · 估算 ¥1.25")
        assert len(t) <= stats.TOOLTIP_MAX

    def test_error_text(self):
        assert "数据读取失败" in stats.tooltip_text({"error": "boom"})

    def test_truncation_extreme(self):
        big = {"requests": 10**12, "cost": 10**8}
        assert len(stats.tooltip_text(big)) <= stats.TOOLTIP_MAX


# ---------- 聚合（mini 假库，monkeypatch DB_PATH） ----------
@pytest.fixture()
def mini_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """构造含 2 天×2 模型+1 条错误的迷你会话库。"""
    db = tmp_path / "mini.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE model_usage(
            id INTEGER PRIMARY KEY, session_id TEXT, provider_id TEXT, model_id TEXT,
            agent TEXT, status TEXT, started_at INTEGER, duration_ms INTEGER,
            time_to_first_token_ms INTEGER, finish_reason TEXT, retry_count INTEGER,
            cancelled_by_user INTEGER, error_type TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_input_tokens INTEGER);
        """
    )

    def day(h: int) -> int:
        return 1789800000000 + h * 3600 * 1000  # 同一基准日不同小时
    rows = [
        # (sid, model, status, started_at, it, ot, cr, retry, cbu, err)
        ("s1", "GLM-5.3-Flash", "completed", day(1), 1_000_000, 100_000, 900_000, 0, 0, None),
        ("s1", "GLM-5.3", "completed", day(2), 500_000, 50_000, 0, 1, 0, None),
        ("s2", "GLM-5.3-Flash", "error", day(3), 10_000, 1_000, 5_000, 0, 0, "rate_limited"),
        ("s2", "GLM-5.3-Flash", "cancelled", day(3), 1_000, 100, 0, 0, 1, None),
    ]
    for i, (sid, m, st, ts, it, ot, cr, rt, cbu, et) in enumerate(rows):
        conn.execute(
            "INSERT INTO model_usage(session_id, provider_id, model_id, agent, status, "
            "started_at, duration_ms, time_to_first_token_ms, finish_reason, retry_count, "
            "cancelled_by_user, error_type, input_tokens, output_tokens, "
            "cache_read_input_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "builtin:bigmodel-coding-plan", m, "zcode-agent", st, ts, 1000, 100,
             "stop", rt, cbu, et, it, ot, cr))
    conn.commit()
    conn.close()
    monkeypatch.setattr(stats, "DB_PATH", db)
    return db


class TestTodayStats:
    def test_aggregation(self, mini_db):
        st = stats.today_stats()
        assert st["requests"] == 4
        assert st["in"] == 1_000_000 + 500_000 + 10_000 + 1_000
        assert st["out"] == 100_000 + 50_000 + 1_000 + 100
        assert st["cr"] == 900_000 + 5_000
        # 成本：Flash (100_000×0.8+900_000×0.23+100_000×2.8 + 10_000×0.8+5_000×0.23+1_000×2.8)
        #      + 5.3  (500_000×8+0+50_000×28) / 1e6
        expect = ((100_000 * 0.8 + 900_000 * 0.23 + 100_000 * 2.8)
                  + (500_000 * 8 + 0 + 50_000 * 28)
                  + (10_000 * 0.8 + 5_000 * 0.23 + 1_000 * 2.8)
                  + (1_000 * 0.8 + 0 + 100 * 2.8)) / 1e6
        assert st["cost"] == pytest.approx(round(expect, 2))


class TestWidgetStats:
    def test_week_and_error(self, mini_db):
        w = stats.widget_stats()
        assert "error" not in w
        assert len(w["week"]) == 7
        total_req = sum(d["req"] for d in w["week"])
        assert total_req == 4
        assert w["last_error"] is not None
        assert w["last_error"]["type"] == "rate_limited"

    def test_missing_db_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stats, "DB_PATH", tmp_path / "nope.db")
        w = stats.widget_stats()
        assert "error" in w
        assert stats.today_stats()["error"]
