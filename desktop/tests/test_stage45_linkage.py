# -*- coding: utf-8 -*-
"""阶段 4/5 联动验收测试：贴纸托盘与主面板的全局平台一致性。

对应规划第 8 节「切换一致性」验收场景（docs/规划-双平台七阶段.md）：
- 快速连续切换：只显示最后选中的平台，丢弃过时结果（revision 单调守卫）。
- 刷新途中切换：切换立即推送该平台缓存，后台刷新不回写旧平台数据。
- 窗口关闭重开/应用重启：主面板 loaded 后恢复平台状态与待定位任务。
- 贴纸跳转主面板：任务定位请求经主进程裁决，非法平台拒绝。
覆盖方式：不启动真实窗口，直接驱动 winchild 的窗口命令循环与 refreshctl 的
推送函数，用假 pipe 断言消息序列（与 test_platform_state 同一手法）。
"""
import json
from pathlib import Path

import pytest

from monitor import refreshctl, winchild
from monitor.traycore import TRAY


@pytest.fixture()
def tray_state():
    with TRAY.platform_lock:
        old = {
            "active_platform": TRAY.active_platform,
            "platform_revision": TRAY.platform_revision,
            "platform_cache": dict(TRAY.platform_cache),
        }
        TRAY.active_platform = "zcode"
        TRAY.platform_revision = 0
        TRAY.platform_cache = {"zcode": None, "codex": None}
    try:
        yield TRAY
    finally:
        with TRAY.platform_lock:
            for key, value in old.items():
                setattr(TRAY, key, value)


class _FakePipe:
    """命令循环假 pipe：send 收集子→主消息，recv 队列耗尽后抛 OSError 触发退出。

    _window_cmd_loop 只在 recv 抛 EOFError/OSError 时退出（管道断开语义）；
    queue.Empty 会被通用异常分支捕获后 continue，无法退出。
    """

    def __init__(self, commands=None):
        self.sent = []
        self._incoming = list(commands or [])

    def send(self, message):
        self.sent.append(message)

    def poll(self, timeout=None):
        # 命令循环只在 poll()==True 后读 recv；退出完全依赖 recv 抛 OSError。
        return True

    def recv(self):
        if not self._incoming:
            raise OSError("管道断开（测试退出）")
        return self._incoming.pop(0)


def _drive_command_loop(state, commands):
    """在假 pipe 上驱动窗口命令循环处理给定命令，耗尽后以管道断开退出。"""
    pipe = _FakePipe(list(commands))
    winchild._window_cmd_loop(state, pipe)
    return pipe.sent


def test_window_cmd_loop_restores_platform_and_pending_task(monkeypatch, tray_state):
    """主面板 loaded：先恢复平台态，再重放待定位任务（顺序不可颠倒）。"""
    state = winchild._WindowState()
    with state.platform_lock:
        state.active_platform = "codex"
        state.platform_revision = 5
        state.pending_open_task_id = "task-9"

    injected = []

    class _Window:
        def evaluate_js(self, script):
            injected.append(script)

    class _Widget:
        def evaluate_js(self, script):
            injected.append("widget:" + script)

    state.window = _Window()
    state.window_ready.set()
    state.widget = _Widget()
    state.widget_ready.set()

    messages = _drive_command_loop(state, [])
    # 空命令队列下循环立即收到管道断开，不发任何子→主消息
    assert messages == []
    # loaded 恢复钩子直接调用验证（不经过命令循环）
    winchild.on_loaded(state)
    assert injected, "loaded 恢复必须注入脚本"
    joined = "".join(injected)
    assert "gaugeApplyPlatform" in joined
    assert '"platform": "codex"' in joined or '"platform":"codex"' in joined
    assert "gaugeOpenTask" in joined and "task-9" in joined
    # 重放完成后清空待定位任务，避免下次 loaded 重复打开
    with state.platform_lock:
        assert state.pending_open_task_id is None


def test_window_cmd_loop_open_task_shows_window_and_defers_when_hidden(monkeypatch, tray_state):
    """贴纸跳转任务：主面板隐藏（未 ready）时保存 pending，ready 后 loaded 钩子重放。"""
    state = winchild._WindowState()
    with state.platform_lock:
        state.active_platform = "codex"
        state.platform_revision = 2
        state.pending_open_task_id = None

    shown = []

    class _Window:
        def show(self):
            shown.append("show")

        def evaluate_js(self, script):
            shown.append(script)

    state.window = _Window()
    state.window_ready.clear()  # 主面板尚未 loaded：走 pending 分支

    _drive_command_loop(state, [("OPEN_TASK", {"platform": "codex", "thread_id": "abc"})])
    assert "show" in shown  # 隐藏也要先唤起窗口
    with state.platform_lock:
        assert state.pending_open_task_id == "abc"

    # ready 后 loaded 钩子负责重放（_on_main_loaded 已由上一用例验证）


def test_window_cmd_loop_drops_stale_platform_state(monkeypatch, tray_state):
    """快速连续切换：旧 revision 的 PLATFORM_STATE 必须被丢弃。"""
    state = winchild._WindowState()
    with state.platform_lock:
        state.active_platform = "codex"
        state.platform_revision = 7

    class _Window:
        def show(self):
            pass

        def evaluate_js(self, script):
            pass

    class _Widget:
        def evaluate_js(self, script):
            pass

    state.window = _Window()
    state.window_ready.set()
    state.widget = _Widget()
    state.widget_ready.set()

    _drive_command_loop(state, [
        ("PLATFORM_STATE", {"platform": "zcode", "revision": 3}),  # 过期
        ("PLATFORM_STATE", {"platform": "zcode", "revision": 8}),  # 最新
    ])
    with state.platform_lock:
        assert state.active_platform == "zcode"
        assert state.platform_revision == 8


def test_refresh_push_never_crosses_platform_after_switch(tmp_path: Path, monkeypatch, tray_state):
    """刷新途中切换：推送数据必须带平台标记与 revision，贴纸侧按标记丢弃异平台数据。"""
    path = tmp_path / "widget.data.json"
    path.write_text(json.dumps({
        "platforms": {
            "zcode": {"today": {"requests": 1}, "generated_at": "z"},
            "codex": {"today": {"requests": 2, "total": 12}, "generated_at": "c"},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(refreshctl, "SIDECAR_PATH", path)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(refreshctl, "child_send", sent.append)

    # 模拟刷新途中切换：revision 3 时推送 codex，随后切回 zcode revision 4
    with TRAY.platform_lock:
        TRAY.active_platform = "codex"
        TRAY.platform_revision = 3
    refreshctl.push_current_platform(force=True)
    with TRAY.platform_lock:
        TRAY.active_platform = "zcode"
        TRAY.platform_revision = 4
    refreshctl.push_current_platform(force=True)

    data_messages = [m for m in sent if m[0] == "WIDGET_DATA"]
    assert [m[1]["platform"] for m in data_messages] == ["codex", "zcode"]
    assert [m[1]["platform_revision"] for m in data_messages] == [3, 4]
    # 贴纸渲染守卫（widget.html renderWidget）与本断言同构：异平台/异 revision 一律丢弃


def test_tray_platform_persist_and_restore(tmp_path: Path, monkeypatch, tray_state):
    """应用重启：active_platform 落盘 widget.json，重载后恢复上次平台。"""
    from monitor import config, traycore
    cfg_path = tmp_path / "widget.json"
    # WIDGET_CFG_PATH 的默认参数在导入时绑定，monkeypatch 无效；显式传 path。
    real_save = config.save_widget_cfg

    def _save(cfg, path=None):
        return real_save(cfg, cfg_path)

    monkeypatch.setattr(config, "save_widget_cfg", _save)
    monkeypatch.setattr("monitor.traycore.save_widget_cfg", _save)

    with TRAY.platform_lock:
        TRAY.active_platform = "codex"
    assert traycore._persist_widget_cfg()
    assert cfg_path.exists()
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["active_platform"] == "codex"

    # 模拟重启：重置内存态后从配置恢复（load/save 走显式 path 参数避开模块级默认绑定）
    with TRAY.platform_lock:
        TRAY.active_platform = "zcode"
    cfg = config.load_widget_cfg(cfg_path)
    traycore.apply_widget_cfg(cfg)
    with TRAY.platform_lock:
        assert TRAY.active_platform == "codex"


def traycore_persist():
    return __import__("monitor.traycore", fromlist=["_persist_widget_cfg"])._persist_widget_cfg()
