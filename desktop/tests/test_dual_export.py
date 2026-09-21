# -*- coding: utf-8 -*-
"""双平台导出的隔离、缺失来源和不可信标题回归。"""
import json
from pathlib import Path

import pytest

import refresh


def snapshot(title="示例任务"):
    """仅提供本测试所需的脱敏来源。"""
    return {
        "platform": "codex", "available": True, "status": "ready",
        "generated_at": "2026-09-21T00:00:00+08:00", "generated_at_ms": 1789920000000,
        "threads": [{"id": "task-1", "title": title}], "turns": [], "usage": [],
        "tools": [], "edges": [], "projects": [], "goals": [], "quota": None,
        "coverage": {"complete": True}, "warnings": [], "environment": {},
        "diagnostics": {}, "sources": {},
    }


def test_codex_survives_missing_zcode_source(tmp_path, monkeypatch):
    import gauge_data
    sample = snapshot()
    monkeypatch.setattr(gauge_data, "collect_codex", lambda: sample)
    monkeypatch.setattr(refresh, "DB_PATH", str(tmp_path / "missing.sqlite"))
    monkeypatch.setattr(refresh, "OUTPUT_PATH", str(tmp_path / "dual.html"))
    assert refresh.main() == 0
    html = (tmp_path / "dual.html").read_text(encoding="utf-8")
    assert 'window.GAUGE_CODEX=' in html
    assert '/*__DATA_PLACEHOLDER__*/' not in html
    assert '/*__CODEX_SCRIPT__*/' not in html
    data = json.loads((tmp_path / "dual.data.json").read_text(encoding="utf-8"))
    assert data["platforms"]["zcode"]["status"] == "unavailable"
    assert data["platforms"]["codex"]["platform"] == "codex"
    assert data["platforms"]["codex"]["status"] == "ready"


def test_codex_title_cannot_escape_script():
    sample = snapshot('</script><script>alert("private")</script>')
    payload = {"sessions": [], "requests": [], "tools": [], "meta": {}, "codex": sample}
    tpl = '<script>const DATA = ' + refresh.PLACEHOLDER + ';</script><script>/*__PLATFORM_DATA__*/</script>'
    html = refresh.serialize_and_inject(tpl, payload, 0)
    assert html.count("<script>") == 2
    assert html.count("</script>") == 2
    assert "\\u003c/script>" in html
    # 大载荷只有一个副本，旧 DATA 不携带 Codex 私有结构。
    legacy, _ = json.JSONDecoder().raw_decode(html.split("const DATA = ", 1)[1])
    assert "codex" not in legacy


def test_zcode_failure_does_not_replace_previous_artifact(tmp_path, monkeypatch):
    """现有数据库损坏时保留旧成品，不静默发布全零 ZCode。"""
    db = tmp_path / "broken.sqlite"
    db.write_bytes(b"not a sqlite database")
    out = tmp_path / "dual.html"
    out.write_text("last-good", encoding="utf-8")
    monkeypatch.setattr(refresh, "DB_PATH", str(db))
    monkeypatch.setattr(refresh, "OUTPUT_PATH", str(out))
    with pytest.raises(Exception):
        refresh.main()
    assert out.read_text(encoding="utf-8") == "last-good"
