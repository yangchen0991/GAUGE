# -*- coding: utf-8 -*-
"""Static UI-contract checks for the self-contained desktop widget page.

Browser behavior is exercised separately with the Playwright CLI. These checks
keep the frozen bridge names, data semantics, and accessibility guardrails
visible to the normal project test collection without adding a test dependency.
"""
from pathlib import Path


WIDGET = Path(__file__).resolve().parents[1] / "widget.html"


def widget_text() -> str:
    return WIDGET.read_text(encoding="utf-8")


def test_public_bridge_contract_is_present() -> None:
    source = widget_text()
    for export in (
        "window.renderWidget=renderWidget",
        "window.renderWeek",  # the named function remains callable by the bridge
        "window.renderWidgetLayout",
        "window.renderWidgetSettings",
        "window.renderRefreshState",
        "window.renderWidgetMaterial",
    ):
        assert export in source
    for method in (
        "open_main",
        "open_usage_page",
        "toggle_layout",
        "get_widget_settings",
        "update_widget_settings",
        "refresh_now",
        "hide_widget",
        "move_widget_by",
    ):
        assert "callApi('" + method + "'" in source


def test_surface_layout_and_motion_constraints_are_explicit() -> None:
    source = widget_text()
    assert "#widget.material-glass{background:var(--bg)}" in source
    assert "background:var(--surface)" in source
    assert "style.opacity" not in source
    assert "linear-gradient" not in source
    assert "box-shadow" not in source
    assert "grid-template-columns:minmax(0,1fr) minmax(0,1fr)" in source
    assert "min-width:0" in source
    assert "body.wx-expanded #week{height:auto" in source
    assert "prefers-reduced-motion:reduce" in source


def test_missing_stale_failure_and_svg_tooltip_semantics_are_present() -> None:
    source = widget_text()
    assert "数据时间未知" in source
    assert "超过 15 分钟，标记为旧数据" in source
    assert "已保留上次读数" in source
    assert "预计即将开始释放" in source
    assert "后开始释放" in source
    assert "var title=svgIn(rect,'title')" in source
    assert 'id="settings" role="dialog"' in source
    assert 'aria-hidden="true" hidden' in source
    assert "event.key==='Escape'" in source
    assert "focusables[0].focus()" in source


def test_usage_unit_toggle_contract_is_present() -> None:
    """用量卡 积分/Token 双模式（默认积分，页面局部状态不落盘）。"""
    source = widget_text()
    assert "用量单位" in source
    assert 'id="seg-unit"' in source
    assert 'data-unit="tokens"' in source
    assert "wx-tokens" in source
    assert "body.wx-tokens .bar{visibility:hidden}" in source
    assert "fmtTokens" in source
    assert "setUsageUnit" in source
    assert "_lastWidgetData" in source
