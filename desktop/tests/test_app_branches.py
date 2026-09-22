# -*- coding: utf-8 -*-
"""desktop/app.py 可无 GUI 直接测的纯分支：设置归一、平台/档位门控、子进程消息状态机。

边界：只测消息处理与状态翻转，托盘/窗口/后台刷新全部分支打桩隔离；
pinned 的原生确认、贴纸渲染等需要 WinForms 的路径不在本文件领地（smoke 已覆盖）。
"""
import pytest

import app
from monitor.traycore import TRAY


class _FakePipe:
    """以预定消息序列驱动 child_msg_loop；排空后 recv 抛 EOF 让循环自然收口。"""

    def __init__(self, msgs):
        self._msgs = list(msgs)

    def poll(self, _timeout):
        return True

    def recv(self):
        if not self._msgs:
            raise EOFError()
        return self._msgs.pop(0)


@pytest.fixture()
def tray_state():
    """快照并重置 TRAY 相关字段：本文件只改内存状态，退出时逐项还原防串味。"""
    fields = ("widget_opacity", "widget_passthrough", "widget_pinned", "widget_visible",
              "widget_plan_tier", "active_platform", "platform_revision",
              "child_exiting", "interval_secs", "continuation_pending", "continuation_due")
    with TRAY.platform_lock:
        old = {f: getattr(TRAY, f) for f in fields}
        TRAY.active_platform = "zcode"
        TRAY.platform_revision = 0
        TRAY.widget_opacity = 0.75
        TRAY.widget_passthrough = False
        TRAY.widget_pinned = False
        TRAY.widget_visible = True
        TRAY.widget_plan_tier = "lite"
        TRAY.child_exiting = False
    try:
        yield
    finally:
        with TRAY.platform_lock:
            for key, value in old.items():
                setattr(TRAY, key, value)
        TRAY.refresh_wake.clear()


@pytest.fixture()
def hooked(monkeypatch):
    """隔离主进程对外副作用：命令发送/持久化/气泡改记录桩，后台刷新钉死不真跑。"""
    calls = {"sent": [], "persist": 0, "notify": []}

    def fake_child_send(cmd):
        calls["sent"].append(cmd)
        return True

    def fake_persist(*_args, **_kwargs):
        calls["persist"] += 1
        return True

    def fake_notify(title, message):
        calls["notify"].append((title, message))

    monkeypatch.setattr(app, "child_send", fake_child_send)
    monkeypatch.setattr(app, "_persist_widget_cfg", fake_persist)
    monkeypatch.setattr(app, "notify_user", fake_notify)
    monkeypatch.setattr(app, "refresh_once", lambda *args: None)
    monkeypatch.setattr(app, "push_current_platform", lambda *args, **kwargs: None)
    return calls


# 默认贴纸配置（与 config.DEFAULTS 对齐），供回显 payload 的精确断言复用
_DEFAULT_PAYLOAD = {"opacity": 0.75, "pinned": False, "passthrough": False}


class TestWidgetSettingsRequest:
    def test_rejects_invalid_payload(self, tray_state, hooked):
        """非法设置：拒绝并回显当前真值，让贴纸页与主进程状态对齐，不落任何盘。"""
        assert app.handle_widget_settings_request({"opacity": 0.5, "junk": 1}) is False
        assert hooked["persist"] == 0
        assert hooked["sent"] == [("WIDGET_CFG", _DEFAULT_PAYLOAD)]

    def test_applies_normalized_partial_update(self, tray_state, hooked):
        """合法部分更新：只接受白名单字段，更新后立即持久化并回显全量配置。"""
        assert app.handle_widget_settings_request({"opacity": 0.6, "passthrough": True}) is True
        assert TRAY.widget_opacity == 0.6
        assert TRAY.widget_passthrough is True
        assert hooked["persist"] == 1
        assert hooked["sent"] == [("WIDGET_CFG", {"opacity": 0.6, "pinned": False,
                                                  "passthrough": True})]

    def test_pins_only_via_native_confirmation_chain(self, tray_state, hooked):
        """仅改 pinned：不立即落盘（等 WIDGET_PIN_RESULT 原生确认），先同步配置再发钉。"""
        assert app.handle_widget_settings_request({"pinned": True}) is True
        assert hooked["persist"] == 0
        assert hooked["sent"] == [("WIDGET_CFG", _DEFAULT_PAYLOAD), ("WIDGET_PIN", True)]


class TestGates:
    def test_tier_switch_gated_by_active_platform(self, tray_state, hooked):
        """档位是 ZCode 专属：Codex 活动时拒切且不落盘，切回后才能换档。"""
        with TRAY.platform_lock:
            TRAY.active_platform = "codex"
        assert app.set_widget_tier("pro") is False
        assert TRAY.widget_plan_tier == "lite"
        assert hooked["persist"] == 0
        with TRAY.platform_lock:
            TRAY.active_platform = "zcode"
        assert app.set_widget_tier("max") is True
        assert TRAY.widget_plan_tier == "max"
        assert hooked["persist"] == 1

    def test_set_interval_zero_clears_continuation(self, tray_state):
        """间隔关断（0）必须清掉续读计划并唤醒计时线程；正值间隔则原样保留。"""
        with TRAY.platform_lock:
            TRAY.continuation_pending = True
            TRAY.continuation_due = 5.0
        app.set_interval(0)
        with TRAY.platform_lock:
            assert TRAY.interval_secs == 0
            assert TRAY.continuation_pending is False
            assert TRAY.continuation_due == 0.0
        assert TRAY.refresh_wake.is_set()
        with TRAY.platform_lock:
            TRAY.refresh_wake.clear()
            TRAY.continuation_pending = True
            TRAY.continuation_due = 5.0
        app.set_interval(300)
        with TRAY.platform_lock:
            assert TRAY.continuation_pending is True
            assert TRAY.continuation_due == 5.0
        assert TRAY.refresh_wake.is_set()

    def test_set_active_platform_rejects_invalid(self, tray_state, hooked):
        """非法平台值（网页桥可能被注入）：拒绝、推回当前真值纠正贴纸、不动配置。"""
        assert app.set_active_platform("claude") is False
        assert TRAY.active_platform == "zcode"
        assert hooked["persist"] == 0
        assert hooked["sent"] == [("PLATFORM_STATE", {"platform": "zcode", "revision": 0})]


class TestChildMessages:
    def test_pin_result_adopts_or_rolls_back(self, tray_state, hooked):
        """WIDGET_PIN_RESULT：ok=True 才采纳请求值；失败一律回滚 False 并气泡提示。"""
        app.child_msg_loop(_FakePipe([["WIDGET_PIN_RESULT", True, False]]))
        assert TRAY.widget_pinned is False            # 原生确认失败：请求 True 也回滚
        assert hooked["persist"] == 1                 # 回滚结果要落盘
        assert any(title == "钉桌面" for title, _msg in hooked["notify"])
        with TRAY.platform_lock:
            TRAY.widget_pinned = False
        hooked["sent"].clear()
        hooked["persist"] = 0
        hooked["notify"].clear()
        app.child_msg_loop(_FakePipe([["WIDGET_PIN_RESULT", True, True]]))
        assert TRAY.widget_pinned is True
        assert hooked["notify"] == []                 # 成功路径不打扰用户

    def test_widget_closed_persists_unless_exiting(self, tray_state, hooked):
        """贴纸窗口销毁：正常运行要落盘保持隐藏；退出序列中跳过（R4-P1-1 防御）。"""
        app.child_msg_loop(_FakePipe([["WIDGET_CLOSED"]]))
        assert TRAY.widget_visible is False
        assert hooked["persist"] == 1
        with TRAY.platform_lock:
            TRAY.widget_visible = True
            TRAY.child_exiting = True
        hooked["persist"] = 0
        app.child_msg_loop(_FakePipe([["WIDGET_CLOSED"]]))
        assert TRAY.widget_visible is False           # 状态仍要同步
        assert hooked["persist"] == 0                 # 但退出期间禁止写 widget.json
