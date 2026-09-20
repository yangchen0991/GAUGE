# -*- coding: utf-8 -*-
"""config.validate_widget_settings / widget_settings_from_cfg 校验用例（审查批 R1）。

validate_widget_settings 是网页桥/设置通道的唯一入口：只接受
opacity（0.60/0.75/0.90 白名单，±1e-9 容差）与 pinned/passthrough（必须
JSON boolean）；未知字段、非 dict、NaN/Infinity、bool 冒充数字一律拒绝。
"""
import pytest

from monitor import config


class TestValidateWidgetSettings:
    def test_valid_full_payload(self):
        out = config.validate_widget_settings(
            {"opacity": 0.9, "pinned": True, "passthrough": False})
        assert out == {"opacity": 0.9, "pinned": True, "passthrough": False}

    def test_opacity_whitelist_with_tolerance(self):
        # 三档白名单；±1e-9 容差内的近似值放行
        for v, ok in ((0.6, True), (0.75, True), (0.9, True),
                      (0.6 + 1e-12, True), (0.75 - 1e-12, True),
                      (0.7, False), (0.5, False), (1.0, False), (0.0, False)):
            out = config.validate_widget_settings({"opacity": v})
            assert (out is not None) == ok, v
            if ok:
                assert out["opacity"] == pytest.approx(v, abs=1e-9)

    def test_bool_masquerading_as_number_rejected(self):
        # bool 是 int 子类：opacity 位置上必须显式拒绝
        assert config.validate_widget_settings({"opacity": True}) is None
        assert config.validate_widget_settings({"opacity": False}) is None

    def test_nan_and_infinity_rejected(self):
        for v in (float("nan"), float("inf"), float("-inf")):
            assert config.validate_widget_settings({"opacity": v}) is None

    def test_unknown_field_rejected(self):
        assert config.validate_widget_settings({"color": "red"}) is None
        assert config.validate_widget_settings({"opacity": 0.75, "extra": 1}) is None

    def test_non_bool_switch_rejected(self):
        assert config.validate_widget_settings({"pinned": 1}) is None
        assert config.validate_widget_settings({"passthrough": "yes"}) is None
        assert config.validate_widget_settings({"pinned": None}) is None

    def test_empty_dict_is_noop(self):
        assert config.validate_widget_settings({}) == {}

    def test_non_dict_rejected(self):
        for bad in (None, [], "opacity", 7):
            assert config.validate_widget_settings(bad) is None


class TestWidgetSettingsFromCfg:
    def test_defaults(self):
        assert config.widget_settings_from_cfg({}) == {
            "opacity": 0.75, "pinned": False, "passthrough": False}

    def test_values_and_shaping(self):
        # 缺失字段补默认；opacity 强制 float（int 0 也归一为 0.0）
        out = config.widget_settings_from_cfg({"opacity": 0.9, "pinned": True})
        assert out == {"opacity": 0.9, "pinned": True, "passthrough": False}
        assert isinstance(out["opacity"], float)
