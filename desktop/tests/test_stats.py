# -*- coding: utf-8 -*-
"""monitor.stats 纯函数与聚合逻辑的单元测试（不触真实数据库）。

另含：GAUGE 官方积分口径（peak_factor/credits_of/积分窗口）、config 档位
自愈、refresh/stats 双链常数漂移锁（2026-09-20 冻结批次 1）。
"""
import json
import sqlite3
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from monitor import config, stats


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


# ---------- 成本公式双链对拍（锁未来漂移；2026-09-22 质检 O2） ----------
def test_cost_formula_matches_between_chains():
    """refresh._row_cost 与 stats._cost_of 数值必须一致（固定输入逐组对拍）。

    两链此前各自实现公式、无数值互证，一旦漂移会出现网页成本与托盘/贴纸
    回退成本分叉且无报警。本测试锁现状而非修 bug：改任一侧公式必须双链
    同步并保持本测试绿色。
    """
    from refresh import PRICING_CNY, _row_cost

    prices = {r["model"]: {"input": r["input"], "cache_read": r["cache_read"],
                           "output": r["output"]} for r in PRICING_CNY["rows"]}
    # (model, 输入, 输出, 缓存读取)：覆盖两档系数表、钳制边界与零值
    cases = [
        ("GLM-5.3-Flash", 1_000_000, 100_000, 900_000),   # 常规：缓存读取 < 输入
        ("GLM-5.3", 1_000_000, 100_000, 1_000_000),       # 边界：缓存读取 == 输入
        ("GLM-5.3", 500_000, 50_000, 2_000_000),          # 钳制：缓存读取 > 输入按输入截断
        ("GLM-5.3-Flash", 0, 0, 0),                       # 零值：全零 token
        ("不存在模型", 1_000_000, 100_000, 900_000),       # 未配价模型按 0 计
    ]
    for model, it, ot, cr in cases:
        assert _row_cost(model, it, ot, cr, prices) == pytest.approx(
            stats._cost_of(model, it, ot, cr)), (model, it, ot, cr)


# ---------- tooltip ----------
class TestTooltip:
    def test_normal_and_truncation(self):
        t = stats.tooltip_text({"requests": 5, "cost": 1.25})
        assert t.startswith("GAUGE 衡 · AI Agent 监控台\n今日请求 5 · 估算 ¥1.25")
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

    # 动态基准日（今天本地零点），避免固定时间戳跨日后与 stats.today_stats()
    # 按系统当前日期圈定的窗口错开而假失败
    base = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    def day(h: int) -> int:
        return base + h * 3600 * 1000
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


# ---------- GAUGE 积分：峰谷系数（官方口径，2026-09-20 冻结） ----------
class TestPeakFactor:
    """边界：周一 13:59→0.5、14:00→1.0、周五 17:59→1.0、18:00→0.5、周六任意→0.5。

    固定历史日期即可：peak_factor 是 ts 的纯函数、与当前时间无关，不会过期；
    所选日期（2024-01-15=周一…01-21=周日）不在任何常见时区的 DST 切换点上，
    本地钟面时间与断言在任意时区自洽。
    """

    @staticmethod
    def _ms(y: int, m: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
        return int(datetime(y, m, d, h, mi, s).timestamp() * 1000)

    def test_boundaries(self):
        assert stats.peak_factor(self._ms(2024, 1, 15, 13, 59)) == 0.5   # 周一 13:59
        assert stats.peak_factor(self._ms(2024, 1, 15, 14, 0)) == 1.0    # 周一 14:00 含头
        assert stats.peak_factor(self._ms(2024, 1, 15, 14, 0, 1)) == 1.0
        assert stats.peak_factor(self._ms(2024, 1, 19, 17, 59)) == 1.0   # 周五 17:59
        assert stats.peak_factor(self._ms(2024, 1, 19, 18, 0)) == 0.5    # 周五 18:00 不含尾
        assert stats.peak_factor(self._ms(2024, 1, 20, 15, 0)) == 0.5    # 周六任意
        assert stats.peak_factor(self._ms(2024, 1, 20, 10, 30)) == 0.5
        assert stats.peak_factor(self._ms(2024, 1, 21, 16, 0)) == 0.5    # 周日
        assert stats.peak_factor(self._ms(2024, 1, 15, 9, 0)) == 0.5     # 工作日非高峰
        assert stats.peak_factor(self._ms(2024, 1, 17, 14, 30)) == 1.0   # 周中高峰中段


# ---------- GAUGE 积分：credits_of 已知数值 ----------
class TestCreditsOf:
    # 高峰/非高峰锚点：2024-01-15（周一）14:00 / 13:59
    PEAK = int(datetime(2024, 1, 15, 14, 0).timestamp() * 1000)
    OFFPEAK = int(datetime(2024, 1, 15, 13, 59).timestamp() * 1000)

    def test_glm53_peak_known_value(self):
        # (1e6×6.9 + 0.9e6×1.7 + 1e5×24) / 1e6 = 10.83（高峰 ×1.0）
        assert stats.credits_of("GLM-5.3", 1_000_000, 100_000, 900_000, self.PEAK) \
            == pytest.approx(10.83)

    def test_offpeak_halved(self):
        assert stats.credits_of("GLM-5.3", 1_000_000, 100_000, 900_000, self.OFFPEAK) \
            == pytest.approx(5.415)

    def test_flash_known_value(self):
        # (1e6×2.3 + 0.9e6×0.56 + 1e5×8) / 1e6 = 3.604
        assert stats.credits_of("GLM-5.3-Flash", 1_000_000, 100_000, 900_000, self.PEAK) \
            == pytest.approx(3.604)

    def test_unknown_model_zero(self):
        assert stats.credits_of("不存在模型", 1_000_000, 1_000_000, 1_000_000, self.PEAK) == 0.0

    def test_zero_tokens(self):
        assert stats.credits_of("GLM-5.3", 0, 0, 0, self.PEAK) == 0.0


# ---------- GAUGE 积分：双链常数漂移锁 ----------
def test_credit_constants_match_between_chains():
    """refresh.py 与 stats.py 双链的积分常数/除数/峰谷函数必须一致（防两链数值分叉）。"""
    from refresh import CREDIT_COEFFS as refresh_coeffs
    from refresh import CREDIT_DIVISOR as refresh_divisor
    from refresh import PLAN_QUOTAS as refresh_quotas
    from refresh import _peak_factor as refresh_peak

    assert refresh_coeffs == stats.CREDIT_COEFFS
    assert refresh_quotas == stats.PLAN_QUOTAS
    assert refresh_divisor == stats.CREDIT_DIVISOR
    # 默认档位此前在 refresh/stats/config 三处各自定义且无锁（2026-09-22 质检
    # 发现），任一处单独改动都会造成贴纸档位回退口径分裂，一并锁住。
    from refresh import DEFAULT_PLAN_TIER as refresh_default_tier
    assert refresh_default_tier == stats.DEFAULT_PLAN_TIER == config.DEFAULT_PLAN_TIER
    # 档位命名漂移锁：config 的合法档位与 stats 的额度键集必须一致
    from monitor import config as widget_config
    assert set(widget_config.PLAN_TIERS) == set(stats.PLAN_QUOTAS)
    base = int(datetime(2024, 1, 15, 0, 0).timestamp() * 1000)   # 周一起一整周
    for step in range(0, 7 * 24 * 60, 37):                       # 每 37 分钟采样
        ts = base + step * 60 * 1000
        assert refresh_peak(ts) == stats.peak_factor(ts)


# ---------- GAUGE 积分：widget_stats 的 plan/window5h/thisweek ----------
_CREDIT_SCHEMA = """
    CREATE TABLE model_usage(
        id INTEGER PRIMARY KEY, session_id TEXT, provider_id TEXT, model_id TEXT,
        agent TEXT, status TEXT, started_at INTEGER, duration_ms INTEGER,
        time_to_first_token_ms INTEGER, finish_reason TEXT, retry_count INTEGER,
        cancelled_by_user INTEGER, error_type TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cache_read_input_tokens INTEGER);
"""


def _make_credit_db(path: Path, rows) -> None:
    """rows: (sid, model, status, started_at, it, ot, cr)。"""
    conn = sqlite3.connect(path)
    conn.executescript(_CREDIT_SCHEMA)
    for sid, model, st, ts, it, ot, cr in rows:
        conn.execute(
            "INSERT INTO model_usage(session_id, provider_id, model_id, agent, status, "
            "started_at, duration_ms, time_to_first_token_ms, finish_reason, retry_count, "
            "cancelled_by_user, error_type, input_tokens, output_tokens, "
            "cache_read_input_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "builtin:bigmodel-coding-plan", model, "zcode-agent", st, ts, 1000, 100,
             "stop", 0, 0, None, it, ot, cr))
    conn.commit()
    conn.close()


def _frozen_datetime_cls(frozen_now: datetime) -> type:
    """datetime 替身：仅 now() 冻结到指定时刻，其余行为与真 datetime 一致。"""
    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(frozen_now.year, frozen_now.month, frozen_now.day,
                       frozen_now.hour, frozen_now.minute, frozen_now.second)
    return _FrozenDT


class TestWidgetCreditWindows:
    """widget_stats 积分窗口：冻结"本自然周周三 04:00"为当前时刻，使窗口断言
    与测试运行日是星期几无关（周一 01:00 行永远在本周内且在 5h 窗外）。
    时间锚点动态取自当前自然周，不会随日期推移过期。"""

    @staticmethod
    def _anchors() -> tuple:
        real_now = datetime.now()
        monday0 = real_now.replace(hour=0, minute=0, second=0, microsecond=0) \
            - timedelta(days=real_now.weekday())
        return monday0, monday0 + timedelta(days=2, hours=4)     # (周一 00:00, 周三 04:00)

    @staticmethod
    def _rows(monday0: datetime) -> list:
        # token 量按 /1e6 除数放大（×100 于 /1e4 时代的行量），使期望值与除数修正前
        # 完全同值：credits = tokens×系数/1e6，tokens×100 恰好抵消除数 ×100 的变化，
        # 百分比断言保持有效区分度（不会全部跌成 0.0）。
        mon01 = int(monday0.timestamp() * 1000) + 3600 * 1000    # 周一 01:00（周内、窗外）
        wed01 = mon01 + 2 * 86400 * 1000                         # 周三 01:00（窗内）
        return [
            ("s1", "GLM-5.3", "completed", mon01, 50_000_000, 4_000_000, 0),
            ("s1", "GLM-5.3-Flash", "completed", wed01, 100_000_000, 10_000_000, 90_000_000),
            ("s1", "GLM-5.3", "completed", wed01 + 3600 * 1000, 50_000_000, 4_000_000, 0),
        ]

    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
             rows, tier_value: str) -> dict:
        monday0, frozen_now = self._anchors()
        db = tmp_path / "credit.db"
        _make_credit_db(db, rows)
        cfg = tmp_path / "widget.json"
        cfg.write_text(json.dumps({"plan_tier": tier_value}), encoding="utf-8")
        monkeypatch.setattr(stats, "DB_PATH", db)
        monkeypatch.setattr(config, "WIDGET_CFG_PATH", cfg)
        frozen_ms = int(frozen_now.timestamp() * 1000)
        monkeypatch.setattr(stats, "datetime", _frozen_datetime_cls(frozen_now))
        monkeypatch.setattr(stats, "time", types.SimpleNamespace(time=lambda: frozen_ms / 1000))
        return stats.widget_stats()

    def test_pro_tier_windows(self, tmp_path, monkeypatch):
        monday0, _frozen = self._anchors()
        w = self._run(tmp_path, monkeypatch, self._rows(monday0), tier_value="pro")
        assert w["plan"] == {"tier": "pro", "window5h_limit": 12000, "week_limit": 60000}
        # 5h 窗口 = 周二 23:00 起：周三两行入窗，周一 01:00 行在窗外但在本周内；
        # 三行均在 01:00/02:00 → 非高峰 ×0.5：
        #   Flash (1e8×2.3+9e7×0.56+1e7×8)/1e6 = 360.4 → 180.2
        #   GLM   (5e7×6.9 + 4e6×24)/1e6 = 441.0 → 220.5
        assert w["window5h"]["credits"] == 400.7
        assert w["window5h"]["used_pct"] == 3.3          # 400.7/12000×100=3.339…
        assert w["window5h"]["reset_eta_min"] == 120     # 最早周三 01:00 +5h − 周三 04:00
        # 窗口内原始 token 累计（与 refresh sidecar 同键）
        assert w["window5h"]["tokens"] == {"in": 150_000_000, "cr": 90_000_000,
                                           "out": 14_000_000}
        # 本自然周（周一 00:00 起）额外含周一 GLM 行 220.5
        assert w["thisweek"]["credits"] == 621.2
        assert w["thisweek"]["used_pct"] == 1.0          # 621.2/60000×100=1.035…
        assert w["thisweek"]["tokens"] == {"in": 200_000_000, "cr": 90_000_000,
                                           "out": 18_000_000}

    def test_invalid_tier_falls_back_lite(self, tmp_path, monkeypatch):
        monday0, _frozen = self._anchors()
        w = self._run(tmp_path, monkeypatch, self._rows(monday0), tier_value="guru")
        assert w["plan"] == {"tier": "lite", "window5h_limit": 2000, "week_limit": 10000}
        assert w["window5h"]["used_pct"] == 20.0         # 400.7/2000×100=20.035…

    def test_empty_5h_window_null_eta(self, tmp_path, monkeypatch):
        monday0, _frozen = self._anchors()
        mon01 = int(monday0.timestamp() * 1000) + 3600 * 1000
        rows = [("s1", "GLM-5.3", "completed", mon01, 50_000_000, 4_000_000, 0)]
        w = self._run(tmp_path, monkeypatch, rows, tier_value="lite")
        assert w["window5h"] == {"credits": 0.0, "used_pct": 0.0, "reset_eta_min": None,
                                 "tokens": {"in": 0, "cr": 0, "out": 0}}
        assert w["thisweek"]["credits"] == 220.5
        assert w["thisweek"]["tokens"] == {"in": 50_000_000, "cr": 0, "out": 4_000_000}

    def test_used_pct_can_exceed_100(self, tmp_path, monkeypatch):
        _monday0, frozen_now = self._anchors()
        wed01 = int(frozen_now.timestamp() * 1000) - 3 * 3600 * 1000   # 周三 01:00
        rows = [("s1", "GLM-5.3", "completed", wed01, 5_000_000_000, 0, 0)]
        w = self._run(tmp_path, monkeypatch, rows, tier_value="lite")
        # (5e9×6.9)/1e6 = 34500，×0.5 = 17250 → 17250/2000×100 = 862.5（后端不钳制）
        assert w["window5h"]["credits"] == 17250.0
        assert w["window5h"]["used_pct"] == 862.5

    def test_reset_eta_half_minute_granularity(self, tmp_path, monkeypatch):
        """四舍五入粒度：eta 20s→0、45s→1（整数毫秒半进位 (diff+30s)//60s 的边界）。"""
        _monday0, frozen_now = self._anchors()
        for eta_s, want in ((20, 0), (45, 1)):
            sub = tmp_path / ("eta%d" % eta_s)
            sub.mkdir()
            ts = int(frozen_now.timestamp() * 1000) - (5 * 3600 * 1000 - eta_s * 1000)
            rows = [("s1", "GLM-5.3", "completed", ts, 50_000_000, 4_000_000, 0)]
            w = self._run(sub, monkeypatch, rows, tier_value="lite")
            assert w["window5h"]["reset_eta_min"] == want, eta_s


# ---------- refresh 管线 sidecar 的积分字段（与 stats 链同数值，冻结时钟） ----------
class TestRefreshSidecarCredits:
    def test_sidecar_credits_chain(self, tmp_path, monkeypatch):
        import refresh
        monday0, frozen_now = TestWidgetCreditWindows._anchors()
        db = tmp_path / "db.sqlite"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT,"
            " time_created INTEGER, time_updated INTEGER);" + _CREDIT_SCHEMA +
            "CREATE TABLE tool_usage (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT, tool_name TEXT, status TEXT, duration_ms INTEGER,"
            " read_only INTEGER, destructive INTEGER, cancelled_by_user INTEGER);")
        conn.execute("INSERT INTO session VALUES ('s1','会话一','C:/p1',1000,2000)")
        for sid, model, st, ts, it, ot, cr in TestWidgetCreditWindows._rows(monday0):
            conn.execute(
                "INSERT INTO model_usage(session_id, provider_id, model_id, agent, status,"
                " started_at, duration_ms, finish_reason, input_tokens, output_tokens,"
                " cache_read_input_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "builtin:bigmodel-coding-plan", model, "zcode-agent", st, ts, 1000,
                 "stop", it, ot, cr))
        conn.commit()
        conn.close()
        tpl = tmp_path / "template.html"
        tpl.write_text(Path(refresh.TEMPLATE_PATH).read_text(encoding="utf-8"),
                       encoding="utf-8")
        out = tmp_path / "out.html"
        frozen_ms = int(frozen_now.timestamp() * 1000)
        monkeypatch.setattr(refresh, "DB_PATH", str(db))
        monkeypatch.setattr(refresh, "TEMPLATE_PATH", str(tpl))
        monkeypatch.setattr(refresh, "OUTPUT_PATH", str(out))
        monkeypatch.setattr(refresh, "datetime", _frozen_datetime_cls(frozen_now))
        monkeypatch.setattr(refresh, "time", types.SimpleNamespace(time=lambda: frozen_ms / 1000))
        # 档位读取路径 mock 为 lite：消除对仓库 desktop/widget.json 实际内容的环境依赖
        monkeypatch.setattr(refresh, "_sidecar_plan_tier", lambda: "lite")
        assert refresh.main() == 0
        sc = json.loads((tmp_path / "out.data.json").read_text(encoding="utf-8"))
        # 档位为 mock 的 lite（见上方 _sidecar_plan_tier patch）
        assert sc["plan"] == {"tier": "lite", "window5h_limit": 2000, "week_limit": 10000}
        # 与 TestWidgetCreditWindows.test_pro_tier_windows 同一组行 → 同数值（lite 分母）
        assert sc["window5h"]["credits"] == 400.7
        assert sc["window5h"]["used_pct"] == 20.0
        assert sc["window5h"]["reset_eta_min"] == 120
        assert sc["window5h"]["tokens"] == {"in": 150_000_000, "cr": 90_000_000,
                                            "out": 14_000_000}
        assert sc["thisweek"]["credits"] == 621.2
        assert sc["thisweek"]["used_pct"] == 6.2
        assert sc["thisweek"]["tokens"] == {"in": 200_000_000, "cr": 90_000_000,
                                            "out": 18_000_000}


# ---------- config：贴纸档位（默认/自愈 lite；向后兼容键集） ----------
class TestConfigPlanTier:
    def test_norm_plan_tier(self):
        assert config.norm_plan_tier("lite") == "lite"
        assert config.norm_plan_tier("pro") == "pro"
        assert config.norm_plan_tier("max") == "max"
        assert config.norm_plan_tier("guru") == "lite"
        assert config.norm_plan_tier(None) == "lite"
        assert config.norm_plan_tier(7) == "lite"

    def test_load_plan_tier_missing_file(self, tmp_path: Path):
        assert config.load_plan_tier(tmp_path / "no.json") == "lite"

    def test_load_plan_tier_valid(self, tmp_path: Path):
        p = tmp_path / "w.json"
        p.write_text(json.dumps({"plan_tier": "max", "visible": False}), encoding="utf-8")
        assert config.load_plan_tier(p) == "max"

    def test_load_plan_tier_invalid_self_heal(self, tmp_path: Path):
        p = tmp_path / "w.json"
        for bad in ('{"plan_tier": "guru"}', '{"plan_tier": 3}', "不是JSON", "{}"):
            p.write_text(bad, encoding="utf-8")
            assert config.load_plan_tier(p) == "lite"

    def test_save_roundtrip_and_heal(self, tmp_path: Path):
        p = tmp_path / "w.json"
        assert config.save_widget_cfg({"visible": True, "plan_tier": "max"}, p)
        assert config.load_plan_tier(p) == "max"
        assert config.save_widget_cfg({"plan_tier": "guru"}, p)   # 非法值落盘自愈为 lite
        assert config.load_plan_tier(p) == "lite"

    def test_load_widget_cfg_tier_passthrough_and_old_keyset(self, tmp_path: Path):
        p = tmp_path / "w.json"
        config.save_widget_cfg({"plan_tier": "pro", "x": 1, "y": 2}, p)
        cfg = config.load_widget_cfg(p)
        assert cfg["plan_tier"] == "pro" and cfg["visible"] is True and cfg["x"] == 1
        q = tmp_path / "old.json"
        config.save_widget_cfg({}, q)                # cfg 不携带档位 → 文件不落该键
        assert "plan_tier" not in config.load_widget_cfg(q)
