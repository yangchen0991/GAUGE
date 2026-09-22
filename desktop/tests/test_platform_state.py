# -*- coding: utf-8 -*-
"""双平台桌面层契约：配置、sidecar 选择、桥接白名单与 revision 数据包。"""
import json
from pathlib import Path

import pytest

from monitor import config, refreshctl, winchild
from monitor import traycore
from monitor.traycore import TRAY


@pytest.fixture()
def tray_state():
    with TRAY.platform_lock:
        old = {
            "active_platform": TRAY.active_platform,
            "platform_revision": TRAY.platform_revision,
            "platform_cache": dict(TRAY.platform_cache),
            "interval_secs": TRAY.interval_secs,
            "continuation_pending": TRAY.continuation_pending,
            "continuation_signature": TRAY.continuation_signature,
            "continuation_due": TRAY.continuation_due,
            "widget_pos": list(TRAY.widget_pos),
            "widget_plan_tier": TRAY.widget_plan_tier,
        }
        TRAY.active_platform = "zcode"
        TRAY.platform_revision = 0
        TRAY.platform_cache = {"zcode": None, "codex": None}
        TRAY.interval_secs = 300
        TRAY.continuation_pending = False
        TRAY.continuation_signature = None
        TRAY.continuation_due = 0.0
    try:
        yield
    finally:
        with TRAY.platform_lock:
            for key, value in old.items():
                setattr(TRAY, key, value)


def test_platform_config_roundtrip_keeps_old_default_keyset(tmp_path: Path):
    old = config.load_widget_cfg(tmp_path / "missing.json")
    assert "active_platform" not in old
    path = tmp_path / "widget.json"
    assert config.save_widget_cfg({"active_platform": "codex", "plan_tier": "max",
                                   "x": 7, "y": 8}, path)
    saved = config.load_widget_cfg(path)
    assert saved["active_platform"] == "codex"
    assert saved["plan_tier"] == "max"
    assert saved["x"] == 7 and saved["y"] == 8
    assert config.is_valid_platform("codex")
    assert not config.is_valid_platform("Codex")
    assert not config.is_valid_platform(1)


def test_persist_candidate_platform_keeps_platform_with_other_settings(monkeypatch, tray_state):
    seen = []
    monkeypatch.setattr(traycore, "save_widget_cfg",
                        lambda cfg: seen.append(dict(cfg)) or True)
    TRAY.widget_pos = [12, 34]
    TRAY.widget_plan_tier = "pro"
    assert traycore._persist_widget_cfg("codex")
    assert seen[0]["active_platform"] == "codex"
    assert seen[0]["x"] == 12 and seen[0]["y"] == 34
    assert seen[0]["plan_tier"] == "pro"


def test_load_sidecar_selects_bundle_and_old_format_is_zcode_only(tmp_path: Path, monkeypatch,
                                                                  tray_state):
    path = tmp_path / "widget.data.json"
    path.write_text(json.dumps({
        "today": {"requests": 1, "cost": 2.0},
        "platforms": {
            "zcode": {"today": {"requests": 2}, "generated_at": "z"},
            "codex": {"today": {"requests": 3, "total": 99}, "generated_at": "c"},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(refreshctl, "SIDECAR_PATH", path)
    zcode = refreshctl.load_sidecar("zcode")
    codex = refreshctl.load_sidecar("codex")
    assert zcode is not None and codex is not None
    assert zcode["platform"] == "zcode" and zcode["today"]["requests"] == 2
    assert codex["platform"] == "codex" and codex["today"]["total"] == 99

    path.write_text(json.dumps({"today": {"requests": 4}}), encoding="utf-8")
    old = refreshctl.load_sidecar("zcode")
    missing_codex = refreshctl.load_sidecar("codex")
    assert old is not None and missing_codex is not None
    assert old["platform"] == "zcode"
    assert missing_codex["platform"] == "codex"
    assert missing_codex["status"] == "unavailable"


def test_codex_missing_never_uses_zcode_fallback(tmp_path: Path, monkeypatch, tray_state):
    monkeypatch.setattr(refreshctl, "SIDECAR_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(refreshctl, "widget_stats",
                        lambda: {"today": {"requests": 8}, "generated_at": "z"})
    zcode = refreshctl.current_widget("zcode", force=True)
    codex = refreshctl.current_widget("codex", force=True)
    assert zcode["status"] == "ok" and zcode["platform"] == "zcode"
    assert codex["status"] == "unavailable" and codex["platform"] == "codex"
    assert codex.get("today") is None


def test_push_current_platform_attaches_revision_and_order(tmp_path: Path, monkeypatch, tray_state):
    path = tmp_path / "widget.data.json"
    path.write_text(json.dumps({
        "platforms": {
            "zcode": {"today": {"requests": 1}, "generated_at": "z"},
            "codex": {"today": {"requests": 2, "total": 12}, "generated_at": "c"},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(refreshctl, "SIDECAR_PATH", path)
    messages: list[tuple[str, dict]] = []
    monkeypatch.setattr(refreshctl, "child_send", messages.append)
    with TRAY.platform_lock:
        TRAY.active_platform = "codex"
        TRAY.platform_revision = 4
    payload = refreshctl.push_current_platform(force=True)
    assert payload is not None
    assert payload["platform"] == "codex" and payload["platform_revision"] == 4
    assert messages[0] == ("PLATFORM_STATE", {"platform": "codex", "revision": 4})
    assert messages[1][0] == "WIDGET_DATA"
    assert messages[1][1]["platform"] == "codex"
    assert messages[1][1]["platform_revision"] == 4


def test_pending_codex_coverage_uses_short_progressive_continuation(tray_state):
    TRAY.refresh_wake.clear()
    TRAY.active_platform = "codex"
    data = {"coverage": {"pending_bytes": 100, "processed_files": 2,
                          "total_files": 4, "complete": False}}
    refreshctl._schedule_continuation(data)
    first_due = TRAY.continuation_due
    assert TRAY.continuation_pending and first_due > 0
    assert TRAY.refresh_wake.is_set()
    TRAY.refresh_wake.clear()
    refreshctl._schedule_continuation(data)
    assert TRAY.continuation_due == first_due
    assert not TRAY.refresh_wake.is_set()
    refreshctl._schedule_continuation({"coverage": {
        "pending_bytes": 90, "processed_files": 3, "total_files": 4, "complete": False,
    }})
    assert TRAY.continuation_pending and TRAY.refresh_wake.is_set()
    TRAY.refresh_wake.clear()


def test_platform_bridge_rejects_invalid_and_limits_task_to_codex(monkeypatch, tray_state):
    sent = []

    class Pipe:
        def send(self, message):
            sent.append(message)

    monkeypatch.setattr(winchild, "_CHILD_PIPE", [Pipe()])
    state = winchild._WindowState()
    main_api = winchild.MainApi(state)
    widget_api = winchild.WidgetApi(state)
    assert not main_api.set_platform("Codex")
    assert main_api.set_platform("codex")
    assert not widget_api.open_task("thread-1")
    state.active_platform = "codex"
    assert widget_api.open_task("thread-1")
    assert sent == [["SET_PLATFORM", "codex"], ["OPEN_TASK", "codex", "thread-1"]]


def test_continuation_chain_caps_after_three_rounds_then_resets(tray_state):
    """无限续读链收敛上限：Codex 活跃写入时 rollout 持续增长、签名每轮都有
    进展，曾导致每 ~11 秒连轴续读、架空 300 秒正常间隔。契约：签名每次
    进展的续读最多排队 3 轮，之后不再调度（回落正常刷新间隔）；complete
    coverage 驱动一次即清零轮数。continuation_rounds 不在既有 fixture 快照
    集内，按该 fixture 手法在本测试内快照/恢复。
    """
    with TRAY.platform_lock:
        saved_rounds = TRAY.continuation_rounds
    TRAY.refresh_wake.clear()
    TRAY.active_platform = "codex"
    with TRAY.platform_lock:
        TRAY.continuation_rounds = 0
    try:
        for round_no in range(1, 4):
            refreshctl._schedule_continuation({"coverage": {
                "pending_bytes": 100 - round_no * 10, "processed_files": round_no,
                "total_files": 9, "complete": False,
            }})
            assert TRAY.continuation_pending is True      # 前 3 轮照常排队追帧
            assert TRAY.continuation_rounds == round_no   # 轮数递增至 3
            TRAY.refresh_wake.clear()
        # 第 4 次（签名仍有进展）已达上限：不再调度，回落正常刷新间隔
        refreshctl._schedule_continuation({"coverage": {
            "pending_bytes": 50, "processed_files": 4, "total_files": 9,
            "complete": False,
        }})
        assert TRAY.continuation_pending is False
        assert TRAY.continuation_due == 0.0
        assert TRAY.continuation_signature is None
        assert not TRAY.refresh_wake.is_set()             # 未再次唤醒续读
        assert TRAY.continuation_rounds == 3              # 达上限后不再递增
        # complete coverage：清 pending/due/signature 的同时轮数清零
        refreshctl._schedule_continuation({"coverage": {
            "pending_bytes": 0, "processed_files": 9, "total_files": 9,
            "complete": True,
        }})
        assert TRAY.continuation_pending is False
        assert TRAY.continuation_rounds == 0
    finally:
        with TRAY.platform_lock:
            TRAY.continuation_rounds = saved_rounds
        TRAY.refresh_wake.clear()
