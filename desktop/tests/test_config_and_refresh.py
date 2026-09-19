# -*- coding: utf-8 -*-
"""monitor.config（widget.json 读写）与 refresh.py 映射函数的单元测试。"""
from pathlib import Path

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
