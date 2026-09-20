"""Regression tests for widget lifetime, client geometry and region ownership."""
from unittest.mock import Mock

import ctypes

from monitor import winchild as wc


def test_layout_toggle_keeps_window_and_data(monkeypatch):
    state = wc._WindowState()
    widget = state.widget = Mock()
    state.last_widget_data = {'window5h': {'credits': 1780}}
    geometry = Mock()
    monkeypatch.setattr(wc, '_sync_widget_geometry', geometry)
    assert wc.WidgetApi(state).toggle_layout()
    assert state.layout == 'wide'
    assert state.widget is widget
    widget.destroy.assert_not_called()
    geometry.assert_called_once_with(state, widget, 'wide')
    widget.evaluate_js.assert_called_once_with('renderWidgetLayout("wide");')
    assert state.last_widget_data['window5h']['credits'] == 1780
    assert wc.WidgetApi(state).toggle_layout()
    assert state.layout == 'compact'


def test_failed_layout_restores_size_and_releases_lock(monkeypatch):
    state = wc._WindowState()
    state.widget = Mock()
    geometry = Mock(side_effect=[RuntimeError('unavailable'), None])
    monkeypatch.setattr(wc, '_sync_widget_geometry', geometry)
    assert not wc.WidgetApi(state).toggle_layout()
    assert state.layout == 'compact'
    assert geometry.call_args_list[-1].args[-1] == 'compact'
    assert state._layout_lock.acquire(blocking=False)
    state._layout_lock.release()
    state.widget.destroy.assert_not_called()


def test_duplicate_layout_request_is_not_interleaved(monkeypatch):
    state = wc._WindowState()
    state.widget = Mock()
    geometry = Mock()
    monkeypatch.setattr(wc, '_sync_widget_geometry', geometry)
    state._layout_lock.acquire()
    try:
        assert not wc.WidgetApi(state).toggle_layout()
        geometry.assert_not_called()
    finally:
        state._layout_lock.release()


def test_stale_closed_event_cannot_clear_new_window(monkeypatch):
    state = wc._WindowState()
    old, current, pipe = Mock(), Mock(), Mock()
    state.widget = current
    state.widget_ready.set()
    monkeypatch.setattr(wc, '_CHILD_PIPE', [pipe])
    wc.on_widget_closed(state, old)
    assert state.widget is current
    assert state.widget_ready.is_set()
    pipe.send.assert_not_called()
    wc.on_widget_closed(state, current)
    assert state.widget is None
    pipe.send.assert_called_once_with(['WIDGET_CLOSED'])


def test_shutdown_does_not_persist_widget_hidden(monkeypatch):
    state = wc._WindowState()
    widget = state.widget = Mock()
    state.exiting = True
    pipe = Mock()
    monkeypatch.setattr(wc, '_CHILD_PIPE', [pipe])
    wc.on_widget_closed(state, widget)
    pipe.send.assert_not_called()


def test_lost_parent_pipe_closes_both_windows():
    state = wc._WindowState()
    state.window, state.widget = Mock(), Mock()
    pipe = Mock()
    pipe.poll.side_effect = EOFError
    wc._window_cmd_loop(state, pipe)
    assert state.exiting
    state.window.destroy.assert_called_once()
    state.widget.destroy.assert_called_once()


def test_rounded_region_matches_css_radius_and_releases_only_on_failure(monkeypatch):
    state = wc._WindowState()
    widget = state.widget = Mock()
    user, gdi = Mock(), Mock()
    user.GetDpiForWindow.return_value = 144
    user.SetWindowRgn.return_value = 1
    gdi.CreateRoundRectRgn.return_value = 123

    def rect(_handle, ptr):
        ptr._obj.right, ptr._obj.bottom = 570, 690
        return 1

    user.GetClientRect.side_effect = rect
    monkeypatch.setattr(ctypes.windll, 'user32', user)
    monkeypatch.setattr(ctypes.windll, 'gdi32', gdi)
    monkeypatch.setattr(wc, '_win_hwnd', lambda _widget: 456)
    monkeypatch.setattr(wc, '_on_widget_ui', lambda _widget, callback: callback(None))
    wc._apply_rounded_region(widget, state)
    gdi.CreateRoundRectRgn.assert_called_once_with(0, 0, 571, 691, 84, 84)
    gdi.DeleteObject.assert_not_called()
    user.SetWindowRgn.return_value = 0
    wc._apply_rounded_region(widget, state)
    gdi.DeleteObject.assert_called_once_with(123)


def test_acrylic_failure_is_not_reported_as_supported(monkeypatch):
    dwm = Mock()
    dwm.DwmSetWindowAttribute.return_value = -1
    dwm.DwmExtendFrameIntoClientArea.return_value = 0
    monkeypatch.setattr(ctypes.windll, 'dwmapi', dwm)
    monkeypatch.setattr(wc.sys, 'getwindowsversion', lambda: Mock(build=22631))
    assert not wc.apply_acrylic_backdrop(123)
    dwm.DwmSetWindowAttribute.return_value = 0
    assert wc.apply_acrylic_backdrop(123)


def test_round_window_rgn_derives_clip_from_shared_geometry(monkeypatch):
    # pythonnet (System.Drawing) is unavailable in this test process, so the
    # full _sync_widget_geometry read-back path cannot execute here; the
    # shared builder's parameter derivation is verified directly instead:
    # diameter = 2 * WIDGET_RADIUS_CSS * scale, rect expanded to w+1 / h+1.
    user, gdi = Mock(), Mock()
    user.SetWindowRgn.return_value = 1
    gdi.CreateRoundRectRgn.return_value = 7
    monkeypatch.setattr(ctypes.windll, 'user32', user)
    monkeypatch.setattr(ctypes.windll, 'gdi32', gdi)
    wc._round_window_rgn(456, 570, 690, 1.0)
    gdi.CreateRoundRectRgn.assert_called_once_with(0, 0, 571, 691, 56, 56)
    user.SetWindowRgn.assert_called_once_with(456, 7, True)
    gdi.DeleteObject.assert_not_called()
    wc._round_window_rgn(456, 570, 690, 1.5)
    gdi.CreateRoundRectRgn.assert_called_with(0, 0, 571, 691, 84, 84)
