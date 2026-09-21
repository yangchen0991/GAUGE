# -*- coding: utf-8 -*-
import sys
from pathlib import Path
# 同时把两个源码目录放进 sys.path，保证双口径可跑：
# - desktop\（parents[1]）：monitor 包所在，缺失时从仓库根跑
#   `python -m pytest desktop/tests -q` 收集失败（No module named monitor）
# - 仓库根（parents[2]）：refresh.py 所在，缺失时从 desktop\ 跑
#   `python -m pytest tests -q` 报 No module named refresh
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE.parents[2]))

import pytest


@pytest.fixture(autouse=True)
def isolate_codex_source(tmp_path, monkeypatch):
    """回归测试不得扫描使用者的会话或写入真实统计缓存。"""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("GAUGE_DATA_DIR", str(tmp_path / "gauge-cache"))
