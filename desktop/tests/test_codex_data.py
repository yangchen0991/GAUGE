# -*- coding: utf-8 -*-
"""Codex 数据层的合成 fixture 验收。

测试只创建临时 Codex home 和临时缓存，不触碰用户真实 Codex 数据。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from gauge_data import codex_widget, collect_codex


def _write_lines(path: Path, events: list[dict], *, trailing_newline: bool = True) -> None:
    text = "\n".join(json.dumps(item, ensure_ascii=False) for item in events)
    if trailing_newline:
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _usage_event(response_id: str, thread_id: str, turn_id: str, *, input_tokens: int, cached: int, output: int, reasoning: int = 0, timestamp: int | None = None) -> dict:
    payload = {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "response_id": response_id,
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
            "total_tokens": input_tokens + output,
        },
    }
    if timestamp is not None:
        payload["timestamp_ms"] = timestamp
    return {"type": "token_usage_record", "payload": payload}


def _make_state(home: Path, rows: list[tuple[str, str, str]], *, projects: bool = False) -> None:
    db = home / "state_5.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE threads(
            id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER,
            source TEXT, model_provider TEXT, cwd TEXT, title TEXT, tokens_used INTEGER,
            git_sha TEXT, git_branch TEXT, cli_version TEXT, agent_role TEXT, model TEXT,
            reasoning_effort TEXT, project_id TEXT, archived INTEGER
        );
        CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE project_roots(project_id TEXT, path TEXT);
        CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT);
        """
    )
    now = int(time.time())
    for thread_id, rollout, cwd in rows:
        conn.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                thread_id,
                rollout,
                now - 2,
                now - 1,
                json.dumps({"parent_thread_id": "parent"} if thread_id == "child" else {}),
                "openai",
                cwd,
                f"任务 {thread_id}",
                99,
                "sha",
                "main",
                "1.0",
                "worker",
                "gpt-test",
                "low",
                None,
                0,
            ),
        )
    if projects:
        conn.execute("INSERT INTO projects VALUES('p1','测试项目')")
        conn.execute("INSERT INTO project_roots VALUES('p1',?)", (str(home / "project"),))
    if any(thread_id == "child" for thread_id, _, _ in rows):
        conn.execute("INSERT INTO thread_spawn_edges VALUES('parent','child','completed')")
    conn.commit()
    conn.close()


def _make_history(home: Path) -> None:
    conn = sqlite3.connect(home / "thread_history_1.sqlite")
    conn.executescript(
        """
        CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT, status TEXT,
            error_json TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER);
        CREATE TABLE thread_items(thread_id TEXT, turn_id TEXT, item_id TEXT,
            created_at_ms INTEGER, item_json TEXT, item_type TEXT);
        """
    )
    now = int(time.time() * 1000)
    conn.execute("INSERT INTO thread_turns VALUES('t1','turn-1','completed',NULL,?,?,12)", (now - 20, now - 8))
    conn.execute(
        "INSERT INTO thread_items VALUES(?,?,?,?,?,?)",
        ("t1", "turn-1", "tool-1", now - 10, json.dumps({"status": "completed", "tool": "read_file", "exitCode": 0}), "mcpToolCall"),
    )
    conn.commit()
    conn.close()


def _make_old_event(thread_id: str, total_input: int, total_output: int, timestamp: int, *, last_input: int = 0, last_output: int = 0, last_cached: int = 0) -> dict:
    return {
        "type": "event_msg",
        "timestamp_ms": timestamp,
        "payload": {
            "type": "token_count",
            "thread_id": thread_id,
            "info": {
                "total_token_usage": {
                    "input_tokens": total_input,
                    "cached_input_tokens": 1,
                    "output_tokens": total_output,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total_input + total_output,
                },
                "last_token_usage": {
                    "input_tokens": last_input,
                    "cached_input_tokens": last_cached,
                    "output_tokens": last_output,
                    "reasoning_output_tokens": 0,
                    "total_tokens": last_input + last_output,
                },
                "quota": {
                    "primary": {"used_percent": 20, "window_minutes": 300, "resets_at": 1},
                    "secondary": {"used_percent": 30, "window_minutes": 10080, "resets_at": 2},
                    "plan_type": "test",
                },
            },
        },
    }


def test_usage_record_is_deduplicated_and_keeps_payload_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex-home"
    cache = tmp_path / "gauge-cache"
    home.mkdir()
    rollout_a = home / "a.jsonl"
    rollout_b = home / "b.jsonl"
    _write_lines(rollout_a, [_usage_event("r1", "t1", "u1", input_tokens=10, cached=2, output=3, reasoning=1)])
    # fork 文件复制了 r1，但 payload.thread_id 仍明确属于 t1。
    _write_lines(rollout_b, [
        _usage_event("r1", "t1", "u1", input_tokens=10, cached=2, output=3),
        _usage_event("r2", "child", "u2", input_tokens=4, cached=1, output=1),
    ])
    _make_state(home, [("t1", str(rollout_a), str(home)), ("child", str(rollout_b), str(home))])
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(home))
    snapshot = collect_codex(home=home, cache_dir=cache)
    by_id = {item["id"]: item for item in snapshot["usage"]}
    assert set(by_id) == {"r1", "r2"}
    assert by_id["r1"]["thread_id"] == "t1"
    assert by_id["r2"]["thread_id"] == "child"
    assert snapshot["coverage"]["usage_records"] == 2
    assert snapshot["status"] in {"ready", "partial"}


def test_incremental_partial_line_append_and_truncation_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir()
    rollout = home / "run.jsonl"
    event1 = _usage_event("r1", "t1", "u1", input_tokens=1, cached=0, output=1)
    _write_lines(rollout, [event1], trailing_newline=False)
    _make_state(home, [("t1", str(rollout), str(home))])
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(home))
    first = collect_codex(home=home, cache_dir=cache)
    assert first["usage"] == []
    assert first["coverage"]["complete"] is False
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    second = collect_codex(home=home, cache_dir=cache)
    assert [item["id"] for item in second["usage"]] == ["r1"]
    _write_lines(rollout, [_usage_event("r2", "t1", "u2", input_tokens=2, cached=0, output=2)])
    third = collect_codex(home=home, cache_dir=cache)
    assert [item["id"] for item in third["usage"]] == ["r2"]


def test_legacy_token_count_derives_usage_from_last_token_usage_and_quota_keeps_expired_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 旧格式 rollout（约 2026-09 之前）没有 token_usage_record；契约 1.2 规则 3
    # 裁定 per-response 用量直接取 token_count.info.last_token_usage（实测与
    # usage_record 为同一数据的两种投影，逐字段相等），逐事件取值即可。
    # 前两个事件的 total_token_usage 累计值相同，用来证明实现不做增量运算。
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir()
    now = int(time.time() * 1000)
    events = [
        _make_old_event("t1", 10, 2, now - 1000, last_input=1, last_output=1, last_cached=0),
        _make_old_event("t1", 10, 2, now - 900, last_input=0, last_output=1, last_cached=1),
        _make_old_event("t1", 15, 4, now - 800, last_input=4, last_output=0, last_cached=2),
    ]
    rollout = home / "old.jsonl"
    _write_lines(rollout, events)
    _make_state(home, [("t1", str(rollout), str(home))])
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(home))
    snapshot = collect_codex(home=home, cache_dir=cache)
    usage = snapshot["usage"]
    assert len(usage) == 3
    assert all(item["derived"] is True for item in usage)
    # usage 按时间升序排列，逐条对应三个事件的 last_token_usage。
    expected = [(1, 0, 1), (0, 1, 1), (4, 2, 0)]
    for item, (input_value, cached_value, output_value) in zip(usage, expected):
        assert item["input_tokens"] == input_value
        assert item["cached_input_tokens"] == cached_value
        assert item["output_tokens"] == output_value
        assert item["total_tokens"] == input_value + output_value
    synthetic_ids = [item["id"] for item in usage]
    assert len(set(synthetic_ids)) == 3
    assert all(item.startswith("legacy-") for item in synthetic_ids)
    # 契约冻结形状：resets_at 源为 epoch 秒，输出统一换算为 resets_at_ms。
    assert snapshot["quota"]["primary"]["resets_at_ms"] == 1000
    assert snapshot["quota"]["secondary"]["resets_at_ms"] == 2000
    assert snapshot["quota"]["secondary"]["window_minutes"] == 10080


def test_modern_file_with_token_count_does_not_double_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 实测 133/136 现代文件同时含 token_usage_record 与 token_count：旧实现
    # 无条件从 token_count 派生导致每响应计两次。修复后现代文件唯一来源是
    # record；增量读取早期（见到 record 前）误派生的记录由文件级兜底过滤丢弃。
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir()
    now = int(time.time() * 1000)
    modern = home / "modern.jsonl"
    gate = home / "gate.jsonl"
    legacy = home / "legacy.jsonl"
    _write_lines(modern, [
        # token_count 在 record 之前：派生只可能来自增量早期，最终必须被过滤。
        _make_old_event("t1", 100, 30, now - 2000, last_input=7, last_output=3, last_cached=0),
        _usage_event("r1", "t1", "u1", input_tokens=7, cached=0, output=3),
    ])
    _write_lines(gate, [
        # record 在 token_count 之前：文件级 modern 门直接阻止后续派生。
        _usage_event("r2", "t3", "u2", input_tokens=5, cached=0, output=1),
        _make_old_event("t3", 50, 10, now - 500, last_input=5, last_output=1, last_cached=0),
    ])
    _write_lines(legacy, [
        _make_old_event("t2", 10, 2, now - 1000, last_input=1, last_output=1, last_cached=0),
    ])
    _make_state(home, [("t1", str(modern), str(home)), ("t3", str(gate), str(home)), ("t2", str(legacy), str(home))])
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(home))
    snapshot = collect_codex(home=home, cache_dir=cache)
    by_thread: dict[str, list[dict]] = {}
    for item in snapshot["usage"]:
        by_thread.setdefault(item["thread_id"], []).append(item)
    assert len(by_thread["t1"]) == 1
    assert by_thread["t1"][0]["id"] == "r1"
    assert by_thread["t1"][0]["derived"] is False
    assert by_thread["t1"][0]["input_tokens"] == 7
    assert by_thread["t1"][0]["output_tokens"] == 3
    assert len(by_thread["t3"]) == 1
    assert by_thread["t3"][0]["id"] == "r2"
    assert by_thread["t3"][0]["derived"] is False
    # 纯旧格式文件仍按契约 1.2 规则 3 派生 derived 记录。
    assert len(by_thread["t2"]) == 1
    assert by_thread["t2"][0]["derived"] is True
    assert snapshot["coverage"]["usage_records"] == 3


def test_quota_frozen_shape_credits_and_iso_envelope_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 阶段 0 实测形态：envelope 顶层 timestamp 为 ISO 8601 UTC 字符串（含毫秒）；
    # rate_limits 携带 credits 与 plan_type；limit_id=premium 变体 primary/secondary
    # 为 null，读取层必须整体跳过而不是产出半残快照。
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir()
    rollout = home / "run.jsonl"
    record_event = {
        "type": "token_usage_record",
        "timestamp": "2026-09-20T16:17:10.035Z",
        "payload": {
            "thread_id": "t1",
            "turn_id": "u1",
            "root_turn_id": "rt-1",
            "response_id": "r1",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 10,
                "cache_write_input_tokens": 0,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 120,
            },
        },
    }
    quota_event = {
        "type": "event_msg",
        "timestamp": "2026-09-20T16:17:10.035Z",
        "payload": {
            "type": "token_count",
            "thread_id": "t1",
            "info": {
                "quota": {
                    "primary": {"used_percent": 71.0, "window_minutes": 300, "resets_at": 1789936301},
                    "secondary": {"used_percent": 74.0, "window_minutes": 10080, "resets_at": 1790433485},
                    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                    "plan_type": "plus",
                },
            },
        },
    }
    premium_event = {
        "type": "event_msg",
        "timestamp_ms": int(time.time() * 1000),
        "payload": {
            "type": "token_count",
            "thread_id": "t1",
            "info": {
                "quota": {"limit_id": "premium", "primary": None, "secondary": None, "plan_type": None},
            },
        },
    }
    _write_lines(rollout, [record_event, quota_event, premium_event])
    _make_state(home, [("t1", str(rollout), str(home))])
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(home))
    snapshot = collect_codex(home=home, cache_dir=cache)
    usage = snapshot["usage"]
    assert len(usage) == 1
    assert usage[0]["id"] == "r1"
    assert usage[0]["response_id"] == "r1"
    assert usage[0]["root_turn_id"] == "rt-1"
    assert usage[0]["cache_write_input"] == 0
    expected_ms = int(datetime.fromisoformat("2026-09-20T16:17:10.035+00:00").timestamp() * 1000)
    assert usage[0]["timestamp"] == expected_ms
    quota = snapshot["quota"]
    assert quota is not None
    assert quota["source"] == "rollout_snapshot"
    assert quota["plan_type"] == "plus"
    assert quota["captured_at"] is not None
    assert quota["primary"]["resets_at_ms"] == 1789936301000
    assert quota["secondary"]["resets_at_ms"] == 1790433485000
    assert quota["credits"] == {"has_credits": False, "unlimited": False, "balance": "0"}


def test_metadata_tools_project_inference_and_readonly_schema_degrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    project = home / "project"
    project.mkdir(parents=True)
    rollout = home / "run.jsonl"
    _write_lines(rollout, [])
    _make_state(home, [("t1", str(rollout), str(project / "nested"))], projects=True)
    _make_history(home)
    conn = sqlite3.connect(home / "goals_1.sqlite")
    conn.execute("CREATE TABLE thread_goals(thread_id TEXT, goal_id TEXT, status TEXT, token_budget INTEGER, tokens_used INTEGER, time_used_seconds INTEGER)")
    conn.execute("INSERT INTO thread_goals VALUES('t1','g1','active',100,4,2)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(home))
    snapshot = collect_codex(home=home, cache_dir=cache)
    assert snapshot["threads"][0]["project_id"] == "p1"
    assert snapshot["threads"][0]["project_inferred"] is True
    assert snapshot["tools"][0]["name"] == "read_file"
    assert snapshot["goals"][0]["id"] == "g1"
    assert snapshot["tools"][0].get("command") is None
    assert snapshot["edges"] == []

    # 删除历史库不是失败整个平台的理由，状态库元数据仍可读。
    (home / "thread_history_1.sqlite").unlink()
    degraded = collect_codex(home=home, cache_dir=cache)
    assert degraded["threads"]
    assert degraded["status"] in {"ready", "partial"}


def test_home_isolation_and_widget_local_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "shared-cache"
    homes = []
    for index in (1, 2):
        home = tmp_path / f"home-{index}"
        home.mkdir()
        rollout = home / "run.jsonl"
        _write_lines(rollout, [_usage_event(f"r{index}", "t1", "u1", input_tokens=index, cached=0, output=1)])
        _make_state(home, [("t1", str(rollout), str(home))])
        homes.append(home)
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(homes[0]))
    first = collect_codex(home=homes[0], cache_dir=cache)
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(homes[1]))
    second = collect_codex(home=homes[1], cache_dir=cache)
    assert [item["id"] for item in first["usage"]] == ["r1"]
    assert [item["id"] for item in second["usage"]] == ["r2"]
    now = int(datetime.now().timestamp() * 1000)
    widget = codex_widget({**second, "usage": [{**second["usage"][0], "timestamp": now}]}, now=now)
    assert widget["today"]["requests"] == 1
    assert widget["today"]["in"] == 2
    assert widget["today"]["out"] == 1
    assert widget["week"][-1]["full"] == datetime.fromtimestamp(now / 1000).date().isoformat()


def test_stale_snapshot_exposes_last_success_at_after_sources_vanish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 产品要求：读取失败时保留最后一次成功数据并显示"最后成功时间"。
    # 曾因 _compact_snapshot 缓存白名单缺 generated_at_ms，stale 路径取值恒
    # 为 None；本用例锁定 stale=True 且 last_success_at 等于首轮成功时点。
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir()
    rollout = home / "run.jsonl"
    _write_lines(rollout, [_usage_event("r1", "t1", "u1", input_tokens=3, cached=0, output=1)])
    _make_state(home, [("t1", str(rollout), str(home))])
    _make_history(home)
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(home))
    first = collect_codex(home=home, cache_dir=cache)
    assert first["diagnostics"]["stale"] is False
    assert first["status"] in {"ready", "partial"}
    success_at = first["generated_at_ms"]
    assert isinstance(success_at, int)

    # 等效"源整体不可用"：移除 state/history 库与 rollout 文件后，
    # source_ok=False 而缓存 last_snapshot（previous）仍在 → 进入 stale 路径。
    (home / "state_5.sqlite").unlink()
    (home / "thread_history_1.sqlite").unlink()
    rollout.unlink()
    second = collect_codex(home=home, cache_dir=cache)
    assert second["diagnostics"]["stale"] is True
    assert second["diagnostics"]["last_success_at"] == success_at
    # 实现：source_ok=False 但有 previous → status="partial"。
    assert second["status"] == "partial"
    assert second["available"] is True
    assert any("stale" in item for item in second["warnings"])

