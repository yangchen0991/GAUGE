# -*- coding: utf-8 -*-
"""Codex 缓存 v3 分片架构收益探针（手动运行，不进 pytest 收集）。

合成 300 文件 × 40 records 语料，冷/稳态/活跃三轮各跑一次，打印每轮
总耗时、阶段耗时与写盘字节数。验收口径：
  1. 稳态轮分片写入字节 = 0（零新字节时分片逐字节不动）；
  2. 稳态轮总耗时 <= 冷轮 1/3（指纹短路 + 历史复用 + 零写入的收益下限）。

运行：py -3.13 desktop/tests/bench_codex_cache.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

import gauge_data
from gauge_data import collect_codex

FILES = 300
RECORDS = 40
ACTIVE_FILES = 30


def _usage_event(response_id: str, thread_id: str, turn_id: str, seq: int) -> dict:
    return {
        "type": "token_usage_record",
        "payload": {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "response_id": response_id,
            "usage": {
                "input_tokens": 100 + seq,
                "cached_input_tokens": 10,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 120 + seq,
            },
        },
    }


def build_home(home: Path) -> None:
    """合成语料：state 库 300 个线程各挂一个 rollout，历史库放少量回合。"""
    home.mkdir(parents=True)
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
    for index in range(FILES):
        rollout = home / f"run-{index:04d}.jsonl"
        events = [_usage_event(f"r-{index:04d}-{seq}", f"t{index:04d}", f"u{seq}", seq) for seq in range(RECORDS)]
        rollout.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n", encoding="utf-8"
        )
        conn.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"t{index:04d}", str(rollout), 1, 2, "{}", "openai", str(home), f"任务 {index}", 0,
             "sha", "main", "1.0", "worker", "gpt-test", "low", None, 0),
        )
    conn.commit()
    conn.close()
    history = sqlite3.connect(home / "thread_history_1.sqlite")
    history.executescript(
        """
        CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT, status TEXT,
            error_json TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER);
        CREATE TABLE thread_items(thread_id TEXT, turn_id TEXT, item_id TEXT,
            created_at_ms INTEGER, item_json TEXT, item_type TEXT);
        """
    )
    history.execute("INSERT INTO thread_turns VALUES('t0000','u0','completed',NULL,1,2,10)")
    history.commit()
    history.close()


class Meter:
    """包装 gauge_data 内部读写函数，按轮统计耗时与写盘字节。"""

    def __init__(self) -> None:
        self.write_bytes: dict[str, int] = {}
        self.phase_ms: dict[str, float] = {}
        self._real_write = gauge_data._atomic_write_text
        self._real_load = gauge_data._load_cache
        self._real_process = gauge_data._process_rollout_files

    def arm(self) -> None:
        gauge_data._atomic_write_text = self._metered_write  # type: ignore[assignment]
        gauge_data._load_cache = self._metered_load  # type: ignore[assignment]
        gauge_data._process_rollout_files = self._metered_process  # type: ignore[assignment]

    def disarm(self) -> None:
        gauge_data._atomic_write_text = self._real_write  # type: ignore[assignment]
        gauge_data._load_cache = self._real_load  # type: ignore[assignment]
        gauge_data._process_rollout_files = self._real_process  # type: ignore[assignment]

    def _metered_write(self, path: Path, text: str, warnings: list, label: str) -> int:
        started = time.perf_counter()
        written = self._real_write(path, text, warnings, label)
        self.phase_ms["write"] = self.phase_ms.get("write", 0.0) + (time.perf_counter() - started) * 1000
        name = path.name
        kind = "shard" if name.startswith("codex-shard-") else name.removesuffix(".json")
        self.write_bytes[kind] = self.write_bytes.get(kind, 0) + written
        return written

    def _metered_load(self, *args, **kwargs):
        started = time.perf_counter()
        result = self._real_load(*args, **kwargs)
        self.phase_ms["read"] = self.phase_ms.get("read", 0.0) + (time.perf_counter() - started) * 1000
        return result

    def _metered_process(self, *args, **kwargs):
        started = time.perf_counter()
        result = self._real_process(*args, **kwargs)
        self.phase_ms["parse"] = self.phase_ms.get("parse", 0.0) + (time.perf_counter() - started) * 1000
        return result

    def reset(self) -> None:
        self.write_bytes = {}
        self.phase_ms = {}


def run_round(meter: Meter, title: str, home: Path, cache: Path) -> float:
    meter.reset()
    started = time.perf_counter()
    snapshot = collect_codex(home=home, cache_dir=cache, max_bytes=1 << 30, max_seconds=120)
    total_ms = (time.perf_counter() - started) * 1000
    phases = " ".join(f"{name}={ms:.0f}ms" for name, ms in sorted(meter.phase_ms.items()))
    writes = " ".join(f"{name}={value}B" for name, value in sorted(meter.write_bytes.items())) or "无写入"
    print(
        f"{title}: total={total_ms:.0f}ms {phases} | 写盘 {writes} | "
        f"usage={len(snapshot['usage'])} complete={snapshot['coverage']['complete']}"
    )
    return total_ms


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="gauge-bench-") as tmp:
        root = Path(tmp)
        home = root / "home"
        build_home(home)
        cache = root / "cache"
        meter = Meter()
        meter.arm()
        try:
            cold = run_round(meter, "冷轮  ", home, cache)
            steady = run_round(meter, "稳态轮", home, cache)
            # 稳态分片字节必须在稳态轮后立即读取，活跃轮会真实重写分片；
            # 未写过分片时计 0。
            steady_shard_bytes = meter.write_bytes.get("shard", 0)
            for index in range(ACTIVE_FILES):
                rollout = home / f"run-{index:04d}.jsonl"
                with rollout.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(_usage_event(f"a-{index:04d}", f"t{index:04d}", "ua", 7)) + "\n")
            active = run_round(meter, "活跃轮", home, cache)
        finally:
            meter.disarm()
        shard_bytes = steady_shard_bytes
    print(f"验收: 稳态分片写入字节={shard_bytes}（要求 0）；稳态/冷轮={steady / cold:.2f}（要求 <= 0.33）")
    ok = shard_bytes == 0 and steady <= cold / 3
    print("结论: " + ("通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
