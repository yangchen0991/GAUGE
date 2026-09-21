# -*- coding: utf-8 -*-
"""monitor.config（widget.json 读写）与 refresh.py 映射函数/全管线的单元测试。"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from monitor import config


# ---------- load ----------
class TestLoadWidgetCfg:
    def test_missing_file_defaults(self, tmp_path: Path):
        cfg = config.load_widget_cfg(tmp_path / "no.json")
        assert cfg == {"visible": True, "passthrough": False, "pinned": False,
                       "opacity": 0.75,
                       "x": config.WIDGET_DEFAULT_POS[0], "y": config.WIDGET_DEFAULT_POS[1]}

    def test_normal_roundtrip(self, tmp_path: Path):
        p = tmp_path / "w.json"
        assert config.save_widget_cfg({"visible": False, "passthrough": True,
                                       "opacity": 0.6, "x": 100, "y": 200}, p)
        cfg = config.load_widget_cfg(p)
        assert cfg == {"visible": False, "passthrough": True, "pinned": False,
                       "opacity": 0.6, "x": 100, "y": 200}

    def test_corrupted_file_self_heal(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("{ 损坏内容", encoding="utf-8")
        cfg = config.load_widget_cfg(p)
        assert cfg["visible"] is True and cfg["opacity"] == 0.75

    def test_opacity_clamped(self, tmp_path: Path):
        p = tmp_path / "w.json"
        config.save_widget_cfg({"opacity": 5.0, "x": 1, "y": 2}, p)
        cfg = config.load_widget_cfg(p)
        assert cfg["opacity"] == 1.0   # 越界收敛到合法区间

    def test_no_tmp_leftover(self, tmp_path: Path):
        p = tmp_path / "w.json"
        config.save_widget_cfg({"visible": True}, p)
        assert not (tmp_path / "w.json.tmp").exists()   # 原子写不留临时文件


# ---------- refresh.py 映射函数（导入模块级，无副作用） ----------
class TestRefreshMappings:
    def test_st_of(self):
        from refresh import st_of
        assert st_of("completed", 0) == 0
        assert st_of("error", 0) == 1
        assert st_of("cancelled", 0) == 2
        assert st_of("completed", 1) == 2      # 用户取消优先于状态
        assert st_of(None, 0) == 0             # NULL 按完成
        assert st_of(None, None) == 0

    def test_fin_of(self):
        from refresh import fin_of
        assert fin_of("stop") == 0
        assert fin_of("tool-calls") == 1
        assert fin_of(None) == 2
        assert fin_of("奇怪值") == 2

    def test_norm_and_clean(self):
        from refresh import norm, clean
        assert norm(None) == "(未知)"
        assert norm("  ") == "(未知)"
        assert norm("GLM") == "GLM"
        assert clean(None) is None
        # 契约：输入应为 str；bytes 会被 str() 化（不抛错），非法代理对被替换
        assert clean(bytes([0xFF])) == str(bytes([0xFF]))
        assert clean("bad\udcff") == "bad?"


# ---------- refresh.py 全管线（临时库）：孤儿请求统一归入「未知会话」合成行 ----------
class TestRefreshOrphanPipeline:
    """临时建库跑完整管线，验证孤儿请求的明细/总计口径一致。"""

    @staticmethod
    def _make_db(path: Path, n_orphans: int) -> None:
        """2 个正常会话 + 3 条正常请求行 + n_orphans 条孤儿行（session_id 不存在）。"""
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                                  time_created INTEGER, time_updated INTEGER);
            CREATE TABLE model_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, provider_id TEXT, model_id TEXT, agent TEXT,
                status TEXT, started_at INTEGER, duration_ms INTEGER,
                time_to_first_token_ms INTEGER, finish_reason TEXT,
                retry_count INTEGER DEFAULT 0, cancelled_by_user INTEGER DEFAULT 0,
                error_type TEXT, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, cache_read_input_tokens INTEGER DEFAULT 0);
            CREATE TABLE tool_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, tool_name TEXT, status TEXT, duration_ms INTEGER,
                read_only INTEGER, destructive INTEGER, cancelled_by_user INTEGER);
            """
        )
        conn.executemany("INSERT INTO session VALUES (?,?,?,?,?)",
                         [("s1", "会话一", "C:/p1", 1000, 2000),
                          ("s2", "会话二", "C:/p2", 3000, 4000)])
        mu = ("INSERT INTO model_usage (session_id,provider_id,model_id,agent,status,"
              "started_at,duration_ms,finish_reason,input_tokens,output_tokens,"
              "cache_read_input_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
        rows = [("s1", "prov", "GLM-5.3", "ag", "completed", 1500, 100, "stop", 10, 20, 5),
                ("s1", "prov", "GLM-5.3", "ag", "completed", 1600, 110, "stop", 11, 21, 6),
                ("s2", "prov", "GLM-5.3", "ag", "error", 1700, 120, None, 12, 22, 7)]
        rows += [("ghost%d" % k, "prov", "GLM-5.3", "ag", "completed", 1800 + k, 90,
                  "stop", 7, 3, 0) for k in range(n_orphans)]
        conn.executemany(mu, rows)
        conn.commit()
        conn.close()

    def _run_pipeline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                      n_orphans: int) -> dict:
        import refresh
        db = tmp_path / "db.sqlite"
        self._make_db(db, n_orphans)
        # 模板/成品均指向临时目录，不触碰仓库产物
        tpl = tmp_path / "template.html"
        tpl.write_text(Path(refresh.TEMPLATE_PATH).read_text(encoding="utf-8"),
                       encoding="utf-8")
        out = tmp_path / "out.html"
        monkeypatch.setattr(refresh, "DB_PATH", str(db))
        monkeypatch.setattr(refresh, "TEMPLATE_PATH", str(tpl))
        monkeypatch.setattr(refresh, "OUTPUT_PATH", str(out))
        assert refresh.main() == 0
        # 从成品 HTML 提取注入的 DATA JSON（占位符整体被 JSON 替换，前缀唯一）
        html = out.read_text(encoding="utf-8")
        raw = html.split("const DATA = ", 1)[1]
        data, _ = json.JSONDecoder().raw_decode(raw)
        return data

    def test_orphans_merged_into_synthetic_session(self, tmp_path, monkeypatch):
        data = self._run_pipeline(tmp_path, monkeypatch, n_orphans=2)
        assert data["meta"]["orphans"] == 2
        # 合成行固定在 sessions 末尾：title/id/dir/时间按冻结契约输出
        last = data["sessions"][-1]
        assert last["title"] == "（未知会话）"
        assert last["id"] == "" and last["dir"] == ""
        assert last["t0"] is None and last["t1"] is None
        # 明细 = 总计：5 条请求（3 正常 + 2 孤儿）全部进入 DATA.requests
        assert data["meta"]["counts"]["requests"] == 5
        assert len(data["requests"]) == data["meta"]["counts"]["requests"]
        # 孤儿请求聚合到合成行（r=2），正常会话计数不受影响
        assert last["r"] == 2
        assert data["sessions"][0]["r"] == 2 and data["sessions"][1]["r"] == 1

    def test_no_orphans_no_synthetic_row(self, tmp_path, monkeypatch):
        data = self._run_pipeline(tmp_path, monkeypatch, n_orphans=0)
        assert data["meta"]["orphans"] == 0
        assert len(data["sessions"]) == 2
        assert all(s["title"] != "（未知会话）" for s in data["sessions"])
        assert len(data["requests"]) == 3


# ---------- refresh.py 步骤 8：桌面 sidecar（单一统计链） ----------
class TestRefreshSidecar:
    """临时建库跑完整管线，验证 sidecar 落盘位置与内容口径。"""

    @staticmethod
    def _make_db(path: Path, with_error: bool) -> None:
        """1 个会话 + 跨 2 天的 3 条请求；with_error 控制是否含 24h 内 error 行。

        时间基准取动态"今日本地零点"，避免固定时间戳跨日后与
        write_sidecar 按系统当前日期圈定的窗口错开而假失败。
        """
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                                  time_created INTEGER, time_updated INTEGER);
            CREATE TABLE model_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, provider_id TEXT, model_id TEXT, agent TEXT,
                status TEXT, started_at INTEGER, duration_ms INTEGER,
                time_to_first_token_ms INTEGER, finish_reason TEXT,
                retry_count INTEGER DEFAULT 0, cancelled_by_user INTEGER DEFAULT 0,
                error_type TEXT, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, cache_read_input_tokens INTEGER DEFAULT 0);
            CREATE TABLE tool_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, tool_name TEXT, status TEXT, duration_ms INTEGER,
                read_only INTEGER, destructive INTEGER, cancelled_by_user INTEGER);
            """
        )
        conn.execute("INSERT INTO session VALUES ('s1','会话一','C:/p1',1000,2000)")
        base = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                   .timestamp() * 1000)

        def yesterday(h: int) -> int:
            return base - 86400 * 1000 + h * 3600 * 1000

        def today(h: int) -> int:
            return base + h * 3600 * 1000

        mu = ("INSERT INTO model_usage (session_id,provider_id,model_id,agent,status,"
              "started_at,duration_ms,finish_reason,input_tokens,output_tokens,"
              "cache_read_input_tokens,error_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        # finish_reason 与 error_type 在库内均可为 NULL，按真实表结构声明
        rows: list[tuple[str, str, str, str, str, int, int, "str | None",
                         int, int, int, "str | None"]] = [
            # 昨日 10:00：GLM-5.3 正常（不进 today，进 week 的昨日桶）
            ("s1", "prov", "GLM-5.3", "ag", "completed", yesterday(10), 100, "stop",
             500_000, 50_000, 0, None),
            # 今日 01:00：GLM-5.3-Flash 正常（进 today）
            ("s1", "prov", "GLM-5.3-Flash", "ag", "completed", today(1), 110, "stop",
             1_000_000, 100_000, 900_000, None),
        ]
        if with_error:
            # 今日 03:00：24h 内错误行（进 today 且成为 last_error）
            rows.append(("s1", "prov", "GLM-5.3-Flash", "ag", "error", today(3), 120,
                         None, 10_000, 1_000, 5_000, "rate_limited"))
        conn.executemany(mu, rows)
        conn.commit()
        conn.close()

    @staticmethod
    def _expected_today_cost() -> float:
        """按管线价目（refresh.PRICING_CNY）独立重算 today 两条今日行的成本。

        Flash 非缓存 100_000×0.8 + 缓存 900_000×0.23 + 输出 100_000×2.8
            + 错误行 非缓存 5_000×0.8 + 缓存 5_000×0.23 + 输出 1_000×2.8
        """
        return ((100_000 * 0.8 + 900_000 * 0.23 + 100_000 * 2.8)
                + (5_000 * 0.8 + 5_000 * 0.23 + 1_000 * 2.8)) / 1e6

    def _run_pipeline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                      with_error: bool) -> tuple:
        import refresh
        db = tmp_path / "db.sqlite"
        self._make_db(db, with_error)
        # 模板/成品均指向临时目录，不触碰仓库产物
        tpl = tmp_path / "template.html"
        tpl.write_text(Path(refresh.TEMPLATE_PATH).read_text(encoding="utf-8"),
                       encoding="utf-8")
        out = tmp_path / "out.html"
        monkeypatch.setattr(refresh, "DB_PATH", str(db))
        monkeypatch.setattr(refresh, "TEMPLATE_PATH", str(tpl))
        monkeypatch.setattr(refresh, "OUTPUT_PATH", str(out))
        assert refresh.main() == 0
        sc_path = tmp_path / "out.data.json"      # 与成品同目录同名主干
        sc = json.loads(sc_path.read_text(encoding="utf-8"))
        # 从成品 HTML 提取 DATA（核对 generated_at 与 meta 同源）
        html = out.read_text(encoding="utf-8")
        raw = html.split("const DATA = ", 1)[1]
        data, _ = json.JSONDecoder().raw_decode(raw)
        assert not (tmp_path / "out.data.json.tmp").exists()   # 原子写不留临时文件
        return sc, data, refresh

    def test_sidecar_content(self, tmp_path, monkeypatch):
        sc, data, refresh = self._run_pipeline(tmp_path, monkeypatch, with_error=True)
        # generated_at 与 DATA.meta 同源同值
        assert sc["generated_at"] == data["meta"]["generated_at"]
        assert sc["db_display"] == refresh.DB_DISPLAY
        # today：库内今日 2 行独立求和
        assert sc["today"]["requests"] == 2
        assert sc["today"]["in"] == 1_000_000 + 10_000
        assert sc["today"]["out"] == 100_000 + 1_000
        assert sc["today"]["cr"] == 900_000 + 5_000
        assert sc["today"]["cost"] == pytest.approx(self._expected_today_cost(), abs=0.006)
        # week：恰 7 项，末项==今日且 req==today.requests；倒数第二项==昨日且 req==1
        today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        assert len(sc["week"]) == 7
        assert sc["week"][-1]["full"] == today0.strftime("%Y-%m-%d")
        assert sc["week"][-1]["req"] == sc["today"]["requests"] == 2
        assert sc["week"][-2]["full"] == (today0 - timedelta(days=1)).strftime("%Y-%m-%d")
        assert sc["week"][-2]["req"] == 1
        # last_error：与库内 error 行一致（at 为该行本地 HH:MM）
        base = int(today0.timestamp() * 1000)
        err_ts = base + 3 * 3600 * 1000
        assert sc["last_error"] == {
            "type": "rate_limited",
            "at": datetime.fromtimestamp(err_ts / 1000).strftime("%H:%M"),
        }
        # 积分窗口 tokens：5h 窗随运行时刻变化（今日 01:00/03:00 行可能出窗），
        # 本周窗是否含昨日行取决于今天星期几——按当前时刻独立重算期望值
        base = int(today0.timestamp() * 1000)
        now_ms = int(datetime.now().timestamp() * 1000)
        # (input, output, cache_read, started_at)，与 _make_db 插入行一致
        tok_rows = [(1_000_000, 100_000, 900_000, base + 3600 * 1000),
                    (10_000, 1_000, 5_000, base + 3 * 3600 * 1000)]
        week_rows = tok_rows + [(500_000, 50_000, 0, base - 86400 * 1000 + 10 * 3600 * 1000)]
        monday_ms = int((today0 - timedelta(days=today0.weekday())).timestamp() * 1000)

        def _sum_in_window(rows, since_ms):
            picked = [(i, o, cr) for i, o, cr, t in rows if t >= since_ms]
            if not picked:
                return {"in": 0, "cr": 0, "out": 0}
            it, ot, cr = (sum(v) for v in zip(*picked))
            return {"in": it, "cr": cr, "out": ot}

        assert sc["window5h"]["tokens"] == _sum_in_window(tok_rows, now_ms - 5 * 3600 * 1000)
        assert sc["thisweek"]["tokens"] == _sum_in_window(week_rows, monday_ms)
        # pricing：含库中出现的模型，值与管线价目快照一致
        assert set(sc["pricing"]) == {"GLM-5.3", "GLM-5.3-Flash"}
        for row in refresh.PRICING_CNY["rows"]:
            assert sc["pricing"][row["model"]] == {
                "input": row["input"], "cache_read": row["cache_read"],
                "output": row["output"],
            }

    def test_no_recent_error_last_error_null(self, tmp_path, monkeypatch):
        sc, _data, _refresh = self._run_pipeline(tmp_path, monkeypatch, with_error=False)
        assert sc["last_error"] is None
        assert sc["today"]["requests"] == 1
