# -*- coding: utf-8 -*-
"""用 node 运行 Codex 交互套件（test_codex_ui.mjs），纳入 pytest 统一入口。

设计说明：
- mjs 套件用最小 DOM 驱动公开交互，覆盖 vm 沙箱里的筛选/平台确认顺序；
  pytest 侧只负责拉起 node 并透传结果，不重复实现断言。
- node 不在 PATH 的机器（如部分 CI）跳过而非失败，避免误报红灯。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("test_codex_ui.mjs")


def test_codex_ui_suite() -> None:
    if shutil.which("node") is None:
        pytest.skip("node 不可用，跳过 Codex 交互套件")
    result = subprocess.run(
        ["node", str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, (
        "Codex 交互套件失败：\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (result.stdout, result.stderr)
    )
