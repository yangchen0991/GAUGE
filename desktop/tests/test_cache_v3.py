# -*- coding: utf-8 -*-
"""Codex 缓存 v3 分片架构验收。

锁定三类契约：分片零写（稳态轮不产生任何无意义 IO）、stale 轮零写
（R-1 语义的写入端补充）、历史库/指纹/日志/node 四处条件化路径的
行为等价与短路生效。测试只创建临时 Codex home 和临时缓存。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import types
from pathlib import Path

import pytest

import gauge_data
from gauge_data import collect_codex


# ---- 合成语料工具（与 test_codex_data 同口径，自带一份避免跨测试文件耦合） ----


def _write_lines(path: Path, events: list[dict], *, trailing_newline: bool = True) -> None:
    text = "\n".join(json.dumps(item, ensure_ascii=False) for item in events)
    if trailing_newline:
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _usage_event(response_id: str, thread_id: str, turn_id: str, *, input_tokens: int, cached: int, output: int) -> dict:
    return {
        "type": "token_usage_record",
        "payload": {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "response_id": response_id,
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached,
                "output_tokens": output,
                "reasoning_output_tokens": 0,
                "total_tokens": input_tokens + output,
            },
        },
    }


def _make_state(home: Path, rows: list[tuple[str, str, str]]) -> None:
    conn = sqlite3.connect(home / "state_5.sqlite")
    conn.executescript(
        """
        CREATE TABLE threads(
            id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER,
            source TEXT, model_provider TEXT, cwd TEXT, title TEXT, tokens_used INTEGER,
            git_sha TEXT, git_branch TEXT, cli_version TEXT, agent_role TEXT, model TEXT,
            reasoning_effort TEXT, project_id TEXT, archived INTEGER
        );
        """
    )
    for thread_id, rollout, cwd in rows:
        conn.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (thread_id, rollout, 1, 2, "{}", "openai", cwd, f"任务 {thread_id}", 0,
             "sha", "main", "1.0", "worker", "gpt-test", "low", None, 0),
        )
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
    conn.execute("INSERT INTO thread_turns VALUES('t1','turn-1','completed',NULL,10,20,12)")
    conn.commit()
    conn.close()


def _corpus(home: Path, *, with_history: bool = False) -> Path:
    """单线程单 rollout 的标准语料，返回 rollout 路径。"""
    home.mkdir(parents=True, exist_ok=True)
    rollout = home / "run.jsonl"
    _write_lines(rollout, [
        _usage_event("r1", "t1", "u1", input_tokens=3, cached=0, output=1),
        _usage_event("r2", "t1", "u2", input_tokens=5, cached=1, output=2),
    ])
    _make_state(home, [("t1", str(rollout), str(home))])
    if with_history:
        _make_history(home)
    return rollout


def _cache_files(cache: Path) -> dict[str, tuple[int, int]]:
    """缓存目录内全部文件的 (mtime_ns, size) 指纹，用于零写断言。"""
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in cache.iterdir() if p.is_file()}


# ---- 1. 分片零写：稳态轮分片与索引逐字节不动，仅快照按语义更新 ----


def test_steady_round_writes_no_shard_or_index_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    _corpus(home)
    first = collect_codex(home=home, cache_dir=cache)
    shards = sorted(cache.glob("codex-shard-*.json"))
    assert len(shards) == 1
    index = cache / "codex-cache-index.json"
    snapshot = cache / "codex-snapshot.json"
    assert index.is_file() and snapshot.is_file()
    shard_before = (shards[0].read_bytes(), shards[0].stat().st_mtime_ns)
    index_before = index.stat().st_mtime_ns
    snapshot_before = snapshot.stat().st_mtime_ns

    second = collect_codex(home=home, cache_dir=cache)

    # 零新字节：分片内容与 mtime 不变，索引未重写；快照按"非 stale 轮都写"更新。
    assert (shards[0].read_bytes(), shards[0].stat().st_mtime_ns) == shard_before
    assert index.stat().st_mtime_ns == index_before
    assert snapshot.stat().st_mtime_ns != snapshot_before
    assert [item["id"] for item in second["usage"]] == [item["id"] for item in first["usage"]]
    assert second["diagnostics"]["stale"] is False
    assert second["coverage"]["complete"] is True


# ---- 2. stale 零写：源消失轮缓存目录逐字节不动（与既有 R-1 测试互补） ----


def test_stale_round_writes_no_cache_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    rollout = _corpus(home, with_history=True)
    first = collect_codex(home=home, cache_dir=cache)
    assert first["diagnostics"]["stale"] is False
    success_at = first["generated_at_ms"]
    before = _cache_files(cache)
    assert before  # 首轮成功必须已落 v3 文件

    (home / "state_5.sqlite").unlink()
    (home / "thread_history_1.sqlite").unlink()
    rollout.unlink()
    second = collect_codex(home=home, cache_dir=cache)

    assert _cache_files(cache) == before
    assert second["diagnostics"]["stale"] is True
    assert second["diagnostics"]["last_success_at"] == success_at
    assert second["status"] == "partial"
    assert second["available"] is True


# ---- 2b. 源不可用且无 previous：同样零写入，不得把空快照写进缓存污染恢复源 ----


def test_unavailable_without_previous_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir()
    snapshot = collect_codex(home=home, cache_dir=cache)
    assert snapshot["status"] == "unavailable"
    assert snapshot["diagnostics"]["stale"] is False
    # 零写语义连目录都不创建：任何 codex-* 落盘都算污染恢复源。
    assert not cache.exists() or not list(cache.iterdir())


# ---- 3. 指纹短路：mtime_ns/size 未变时不开文件、不算双指纹 ----


def test_fingerprint_skipped_when_stat_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    rollout = _corpus(home)
    collect_codex(home=home, cache_dir=cache)

    counts = {"fingerprint": 0, "content": 0}
    real_fingerprint = gauge_data._fingerprint
    real_content = gauge_data._content_fingerprint

    def counting_fingerprint(path: Path) -> str | None:
        counts["fingerprint"] += 1
        return real_fingerprint(path)

    def counting_content(path: Path) -> str | None:
        counts["content"] += 1
        return real_content(path)

    monkeypatch.setattr(gauge_data, "_fingerprint", counting_fingerprint)
    monkeypatch.setattr(gauge_data, "_content_fingerprint", counting_content)

    second = collect_codex(home=home, cache_dir=cache)
    assert counts == {"fingerprint": 0, "content": 0}
    assert len(second["usage"]) == 2

    # 短路不能永久失效：文件一旦变化必须回到指纹全流程并读到新数据。
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_usage_event("r3", "t1", "u3", input_tokens=1, cached=0, output=1)) + "\n")
    third = collect_codex(home=home, cache_dir=cache)
    assert counts["fingerprint"] >= 1 and counts["content"] >= 1
    assert [item["id"] for item in third["usage"]] == ["r1", "r2", "r3"]


# ---- 4. 历史复用：history mtime 未变时第二轮零 SQL 行读取 ----


class _CountingCursor:
    def __init__(self, cursor: sqlite3.Cursor, counts: dict[str, int]) -> None:
        self._cursor = cursor
        self._counts = counts

    def fetchall(self) -> list:
        rows = self._cursor.fetchall()
        self._counts["rows"] += len(rows)
        return rows

    def __iter__(self):
        for row in self._cursor:
            self._counts["rows"] += 1
            yield row

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _CountingConnection:
    def __init__(self, connection: sqlite3.Connection, counts: dict[str, int]) -> None:
        self._connection = connection
        self._counts = counts

    def execute(self, sql: str, *args):
        return _CountingCursor(self._connection.execute(sql, *args), self._counts)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def test_history_reuse_zero_sql_when_mtime_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    _corpus(home, with_history=True)

    counts = {"rows": 0}
    real_open = gauge_data._open_ro

    @contextlib.contextmanager
    def counting_open(path):
        with real_open(path) as connection:
            # 只统计历史库连接；状态库每轮照读，不在本契约范围内。
            if "thread_history" in str(path):
                yield _CountingConnection(connection, counts)
            else:
                yield connection

    monkeypatch.setattr(gauge_data, "_open_ro", counting_open)

    first = collect_codex(home=home, cache_dir=cache)
    assert counts["rows"] > 0
    after_first = counts["rows"]

    second = collect_codex(home=home, cache_dir=cache)
    assert counts["rows"] == after_first
    assert [item["id"] for item in second["turns"]] == [item["id"] for item in first["turns"]]


# ---- 5. 历史变化正确性：mtime 变化触发重扫，新 turn 与新 usage 都要可见 ----


def test_history_rescan_picks_up_new_rows_and_rollout_append(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    rollout = _corpus(home, with_history=True)
    first = collect_codex(home=home, cache_dir=cache)
    assert [item["id"] for item in first["turns"]] == ["turn-1"]

    conn = sqlite3.connect(home / "thread_history_1.sqlite")
    conn.execute("INSERT INTO thread_turns VALUES('t1','turn-2','completed',NULL,30,40,9)")
    conn.commit()
    conn.close()
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_usage_event("r3", "t1", "u3", input_tokens=1, cached=0, output=1)) + "\n")

    second = collect_codex(home=home, cache_dir=cache)
    assert {item["id"] for item in second["turns"]} == {"turn-1", "turn-2"}
    assert [item["id"] for item in second["usage"]] == ["r1", "r2", "r3"]


# ---- 6. v2→v3 迁移：旧单文件在场走全量重建，首轮成功落 v3 并删除旧文件 ----


def test_v2_single_file_cache_migrates_to_v3_layout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _corpus(home, with_history=True)
    reference = collect_codex(home=home, cache_dir=tmp_path / "ref-cache")

    cache = tmp_path / "cache"
    cache.mkdir()
    v2_name = "codex-cache-" + hashlib.sha256(str(home).encode("utf-8", "replace")).hexdigest()[:20] + ".json"
    (cache / v2_name).write_text(
        json.dumps({"schema_version": 2, "source_home": str(home), "files": {}, "last_snapshot": None}),
        encoding="utf-8",
    )

    second = collect_codex(home=home, cache_dir=cache)

    assert (cache / "codex-cache-index.json").is_file()
    assert (cache / "codex-snapshot.json").is_file()
    assert list(cache.glob("codex-shard-*.json"))
    # 旧文件删除断言按 v2 命名口径匹配，避免误伤 v3 索引名。
    assert not [p for p in cache.glob("codex-cache-*.json") if re.fullmatch(r"codex-cache-[0-9a-f]{8,40}\.json", p.name)]
    # F2 升版：合成 id 改字节偏移后 schema 升 4 触发干净重建（授权见修复契约）。
    assert second["diagnostics"]["cache_schema_version"] == 4
    core = ("usage", "threads", "turns", "tools", "edges", "projects", "goals", "quota", "coverage", "status", "available")
    assert {key: second[key] for key in core} == {key: reference[key] for key in core}


# ---- 7. 孤儿分片 GC：index 未引用的分片在保存阶段删除 ----


def test_orphan_shard_is_collected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    _corpus(home)
    collect_codex(home=home, cache_dir=cache)
    orphan = cache / "codex-shard-0123456789ab.json"
    orphan.write_text("{}", encoding="utf-8")
    real_shards = sorted(cache.glob("codex-shard-*.json"))
    assert orphan in real_shards

    collect_codex(home=home, cache_dir=cache)

    assert not orphan.exists()
    assert len([p for p in cache.glob("codex-shard-*.json")]) == len(real_shards) - 1


# ---- 8. 日志诊断下推等价：SQL 聚合与逐行参考实现逐字段一致 ----


def test_log_diagnostics_matches_row_by_row_reference(tmp_path: Path) -> None:
    db = tmp_path / "logs_2.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE log_rows(level TEXT, target TEXT, body TEXT);
        CREATE TABLE only_level(level TEXT);
        CREATE TABLE only_target(target TEXT);
        CREATE TABLE unrelated(foo TEXT);
        """
    )
    # 覆盖映射边界：大小写、None、空串、超长截断后同键（计数必须合并）。
    conn.executemany(
        "INSERT INTO log_rows VALUES(?,?,?)",
        [
            ("ERROR", "gauge::x", "b"),
            ("error", "gauge::x", "b"),
            (None, "gauge::y", "b"),
            ("WARN", None, "b"),
            ("WARN", "", "b"),
            ("info", "t" * 200 + "A", "b"),
            ("info", "t" * 200 + "B", "b"),
        ],
    )
    conn.executemany("INSERT INTO only_level VALUES(?)", [("fatal",), (None,)])
    conn.executemany("INSERT INTO only_target VALUES(?)", [("gauge::solo",)])
    conn.execute("INSERT INTO unrelated VALUES('x')")
    conn.commit()
    conn.close()

    # 参考实现 = 下推前的逐行口径，逐行经同一 _safe_text 映射后累加。
    reference: dict[str, int] = {}
    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    tables = [row[0] for row in reader.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        columns = {row[1] for row in reader.execute(f'PRAGMA table_info("{table}")')}
        if "level" not in columns and "target" not in columns:
            continue
        selected = [name for name in ("level", "target") if name in columns]
        quoted = ", ".join(f'"{name}"' for name in selected)
        for item in reader.execute(f'SELECT {quoted} FROM "{table}"'):
            level = gauge_data._safe_text(item["level"], 80) if "level" in selected else "unknown"
            target = gauge_data._safe_text(item["target"], 120) if "target" in selected else "unknown"
            key = f"{level or 'unknown'}:{target or 'unknown'}"
            reference[key] = reference.get(key, 0) + 1
    reader.close()

    assert gauge_data._read_log_diagnostics(db) == reference


# ---- 9. node 校验条件化：同输入只 spawn 一次，script 变化才再校验 ----


def test_node_check_spawns_once_per_script_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import refresh

    spawns = {"n": 0}

    def fake_run(cmd, **kwargs):
        spawns["n"] += 1
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(refresh, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(refresh, "OUTPUT_PATH", str(tmp_path / "out.html"))
    monkeypatch.setattr(refresh, "shutil", types.SimpleNamespace(which=lambda name: "node"))
    monkeypatch.setattr(refresh, "subprocess", types.SimpleNamespace(run=fake_run))

    html = "<html><body><script>var a = 1;</script></body></html>"
    assert refresh.node_syntax_check(html) is True
    assert refresh.node_syntax_check(html) is True
    assert spawns["n"] == 1

    changed = "<html><body><script>var a = 2;</script></body></html>"
    assert refresh.node_syntax_check(changed) is True
    assert spawns["n"] == 2
    assert refresh.node_syntax_check(changed) is True
    assert spawns["n"] == 2
    # 哈希缓存文件随成品路径落盘且记录的是成品 script 的 sha1。
    check_file = Path(str(tmp_path / "out.html") + ".nodecheck")
    assert check_file.is_file()
    stored = json.loads(check_file.read_text(encoding="utf-8"))
    assert stored == [hashlib.sha1(b"var a = 2;").hexdigest()]


# ---- 兜底：既有 v2 文件名正则不得误伤 v3 索引 ----


def test_legacy_cache_discovery_ignores_v3_index(tmp_path: Path) -> None:
    index = tmp_path / "codex-cache-index.json"
    index.write_text("{}", encoding="utf-8")
    legacy = tmp_path / ("codex-cache-" + "a" * 20 + ".json")
    legacy.write_text("{}", encoding="utf-8")
    found = gauge_data._find_legacy_cache_files(tmp_path)
    assert found == [legacy]


# ---- F4-1 注入回归：分片写失败时提交指针不得越过未持久化数据 ----


def test_shard_write_failure_rolls_back_index_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir()
    rollout_a = home / "a.jsonl"
    rollout_b = home / "b.jsonl"
    _write_lines(rollout_a, [_usage_event("ra1", "t1", "u1", input_tokens=1, cached=0, output=1)])
    _write_lines(rollout_b, [_usage_event("rb1", "t2", "u1", input_tokens=2, cached=0, output=1)])
    _make_state(home, [("t1", str(rollout_a), str(home)), ("t2", str(rollout_b), str(home))])
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(home))
    first = collect_codex(home=home, cache_dir=cache)
    assert first["coverage"]["complete"] is True
    index_path = cache / "codex-cache-index.json"
    entry_a_before = json.loads(index_path.read_text(encoding="utf-8"))["files"][str(rollout_a)]
    entry_b_before = json.loads(index_path.read_text(encoding="utf-8"))["files"][str(rollout_b)]

    with rollout_a.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_usage_event("ra2", "t1", "u2", input_tokens=1, cached=0, output=1)) + "\n")
    with rollout_b.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_usage_event("rb2", "t2", "u2", input_tokens=1, cached=0, output=1)) + "\n")

    real_write = gauge_data._atomic_write_text

    def failing_shard_write(path: Path, text: str, warnings: list, label: str) -> int:
        # 只让 a 的分片写失败（与 _atomic_write_text 失败路径同形：记警告返 0）。
        if path.name == gauge_data._shard_name(str(rollout_a)):
            warnings.append("注入：分片写失败")
            return 0
        return real_write(path, text, warnings, label)

    monkeypatch.setattr(gauge_data, "_atomic_write_text", failing_shard_write)
    second = collect_codex(home=home, cache_dir=cache)
    monkeypatch.undo()
    assert second["coverage"]["complete"] is True
    index_after = json.loads(index_path.read_text(encoding="utf-8"))["files"]
    # 未持久化的 a：index 原样保留磁盘旧元数据，指针不得越过分片数据。
    assert index_after[str(rollout_a)] == entry_a_before
    # 已持久化的 b：正常推进。
    assert index_after[str(rollout_b)] != entry_b_before

    third = collect_codex(home=home, cache_dir=cache)
    ids = [item["id"] for item in third["usage"]]
    assert sorted(ids) == ["ra1", "ra2", "rb1", "rb2"]
    assert len(ids) == len(set(ids))


# ---- F4-2 注入回归：索引写失败（分片超前）时重解析必须幂等无双计 ----


def _padded_legacy_event(thread_id: str, seq: int, timestamp: int) -> dict:
    # pad 撑大前 4KB 窗口之外的内容：追加新行不得改变 prefix 指纹，
    # 否则回退轮会走全量重建（clear）而绕开要测的增量重解析路径。
    return {
        "type": "event_msg",
        "timestamp_ms": timestamp,
        "payload": {
            "type": "token_count",
            "thread_id": thread_id,
            "pad": "x" * 1500,
            "info": {
                "total_token_usage": {
                    "input_tokens": seq * 10, "cached_input_tokens": 0,
                    "output_tokens": seq, "reasoning_output_tokens": 0,
                    "total_tokens": seq * 11,
                },
                "last_token_usage": {
                    "input_tokens": seq * 10, "cached_input_tokens": 0,
                    "output_tokens": seq, "reasoning_output_tokens": 0,
                    "total_tokens": seq * 11,
                },
            },
        },
    }


def test_index_write_failure_reparse_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir()
    rollout = home / "legacy.jsonl"
    events = [_padded_legacy_event("t1", seq, 1000 + seq) for seq in range(1, 5)]
    _write_lines(rollout, events)
    assert rollout.stat().st_size > 4096
    _make_state(home, [("t1", str(rollout), str(home))])
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(home))
    first = collect_codex(home=home, cache_dir=cache)
    assert len(first["usage"]) == 4

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_padded_legacy_event("t1", 5, 1005)) + "\n")

    real_write = gauge_data._atomic_write_text

    def failing_index_write(path: Path, text: str, warnings: list, label: str) -> int:
        # 分片成功、索引失败：磁盘出现"分片超前于索引"的中间态。
        if path.name == "codex-cache-index.json":
            warnings.append("注入：索引写失败")
            return 0
        return real_write(path, text, warnings, label)

    monkeypatch.setattr(gauge_data, "_atomic_write_text", failing_index_write)
    second = collect_codex(home=home, cache_dir=cache)
    monkeypatch.undo()
    assert len(second["usage"]) == 5

    # 单轮直扫参考：同一最终语料、全新缓存目录。
    reference = collect_codex(home=home, cache_dir=tmp_path / "ref-cache")
    third = collect_codex(home=home, cache_dir=cache)
    assert len(third["usage"]) == 5
    assert len({item["id"] for item in third["usage"]}) == 5
    assert third["usage"] == reference["usage"]

