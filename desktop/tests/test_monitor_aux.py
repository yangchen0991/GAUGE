# -*- coding: utf-8 -*-
"""monitor 四个辅助模块的最小直接测试：appenv / singleinst / dialogs / pin_desktop。

边界：不真弹窗（Win32 调用点全部打桩）、不真枚举桌面窗口（user32 换替身）、
不占固定端口（singleinst 的端口经 monkeypatch 注入空闲临时端口）、不真打包
（appenv 的 frozen 分支用独立模块对象重放求值，不污染真实 monitor.appenv）。
"""
import importlib.util
import logging
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from monitor import appenv, dialogs, pin_desktop, singleinst
from monitor.appenv import WINDOW_TITLE
from monitor.dialogs import WEBVIEW2_URL


# ---------- appenv：路径解析与冻结形态判定 ----------
class TestAppenvPaths:
    def test_source_mode_path_constants(self):
        """源码形态常量回归锁：desktop 与仓库根的落点一旦漂移即红。"""
        desktop_dir = Path(appenv.__file__).resolve().parent.parent
        assert appenv.APP_DIR == desktop_dir
        assert appenv.RESOURCE_DIR == desktop_dir        # 源码形态：资源与可写目录同域
        assert appenv.PARENT_DIR == desktop_dir.parent
        assert appenv.HTML_PATH == desktop_dir.parent / "AI-Agent监控台.html"
        # sidecar 命名规则：成品 HTML 去扩展名后接 .data.json，且必须与 HTML 同目录
        assert appenv.SIDECAR_PATH.name == "AI-Agent监控台.data.json"
        assert appenv.SIDECAR_PATH.parent == appenv.HTML_PATH.parent
        assert appenv.WIDGET_HTML_PATH == desktop_dir / "widget.html"
        assert appenv.REFRESH_PY == desktop_dir.parent / "refresh.py"
        assert appenv.LOG_PATH == desktop_dir / "app.log"
        # 环境自洽：源码形态依赖的两个资源确实在仓库里（缺失说明打包/布局被破坏）
        assert appenv.WIDGET_HTML_PATH.is_file()
        assert appenv.REFRESH_PY.is_file()

    @staticmethod
    def _load_appenv(monkeypatch, exe, meipass):
        """以独立模块对象重放 appenv 顶层求值：frozen 分支不影响真实模块缓存。"""
        spec = importlib.util.spec_from_file_location(
            "appenv_under_test", Path(appenv.__file__))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(exe))
        if meipass is None:
            monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        else:
            monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
        spec.loader.exec_module(module)
        return module

    def test_frozen_paths_split_exe_dir_and_resource_dir(self, tmp_path, monkeypatch):
        """exe 形态：可写目录跟 exe、资源目录跟 _MEIPASS，两者分离不混用。"""
        exe = tmp_path / "GAUGE衡.exe"
        meipass = tmp_path / "_MEI解包"
        mod = self._load_appenv(monkeypatch, exe, meipass)
        assert mod.APP_DIR == tmp_path.resolve()
        assert mod.RESOURCE_DIR == meipass.resolve()
        # HTML/日志写 exe 同目录（可写），refresh.py/widget.html 从解包目录读（只读资源）
        assert mod.HTML_PATH == tmp_path.resolve() / "AI-Agent监控台.html"
        assert mod.LOG_PATH == tmp_path.resolve() / "app.log"
        assert mod.REFRESH_PY == meipass.resolve() / "refresh.py"
        assert mod.WIDGET_HTML_PATH == meipass.resolve() / "widget.html"

    def test_frozen_without_meipass_falls_back_to_exe_dir(self, tmp_path, monkeypatch):
        """降级路径：frozen 但无 _MEIPASS（onedir 直跑等）时资源目录并回 exe 目录。"""
        mod = self._load_appenv(monkeypatch, tmp_path / "GAUGE衡.exe", None)
        assert mod.RESOURCE_DIR == mod.APP_DIR == tmp_path.resolve()
        assert mod.WIDGET_HTML_PATH == tmp_path.resolve() / "widget.html"
        assert mod.REFRESH_PY == tmp_path.resolve() / "refresh.py"


def test_setup_logging_rotates_and_opens_lazily(tmp_path, monkeypatch):
    """轮转参数（1MB×3）与惰性建文件是驻留进程的行为契约，锁住防止回退。"""
    monkeypatch.setattr(appenv, "LOG_PATH", tmp_path / "app.log")
    monkeypatch.setattr(logging.root, "handlers", [])   # basicConfig 只在空 root 时生效
    monkeypatch.setattr(logging.root, "level", logging.root.level)  # 测后还原级别
    try:
        appenv.setup_logging("w")
        rotators = [h for h in logging.root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotators) == 1
        assert rotators[0].maxBytes == 1_000_000 and rotators[0].backupCount == 3
        assert not (tmp_path / "app.log").exists()      # delay=True：首条日志前不建文件
        appenv.log("探针")
        content = (tmp_path / "app.log").read_text(encoding="utf-8")
        assert "探针" in content and "[w] " in content  # 子进程 tag 前缀可用于日志归属
    finally:
        for handler in list(logging.root.handlers):
            handler.close()                             # 释放句柄，避免 Windows 文件锁连坐 tmp 清理


# ---------- singleinst：单实例锁的端口语义（端口经注入，不碰固定 59321） ----------
def _free_loopback_port() -> int:
    """抓一个当前空闲的回环端口：绑 0 拿号后立刻释放，供 monkeypatch 注入。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class TestSingleInstance:
    def test_bind_grants_listening_identity(self, monkeypatch):
        """端口可绑定 → 返回监听中的 socket，且身份精确落在注入端口上。"""
        port = _free_loopback_port()
        monkeypatch.setattr(singleinst, "SINGLE_PORT", port)
        srv = singleinst.bind_single_instance(lambda cmd: True)
        try:
            assert srv is not None
            assert srv.getsockname() == ("127.0.0.1", port)
        finally:
            if srv is not None:
                srv.close()

    def test_second_binder_yields_then_rebind_after_release(self, monkeypatch):
        """让位语义：端口被占 → 后启动方拿到 None（走通知+静默退出）；释放后可重绑。"""
        monkeypatch.setattr(singleinst, "SINGLE_PORT", _free_loopback_port())
        first = singleinst.bind_single_instance(lambda cmd: True)
        try:
            assert first is not None
            assert singleinst.bind_single_instance(lambda cmd: True) is None
        finally:
            first.close()
        again = singleinst.bind_single_instance(lambda cmd: True)
        try:
            assert again is not None                     # 锁释放后新进程能重新成为持有方
        finally:
            again.close()

    def test_show_is_forwarded_to_injected_callback(self, monkeypatch):
        """SHOW 协议：notify 发送 → 持有方 accept 线程解析后转发给 on_show 回调。"""
        monkeypatch.setattr(singleinst, "SINGLE_PORT", _free_loopback_port())
        received = []
        done = threading.Event()

        def on_show(cmd):
            received.append(cmd)
            done.set()
            return True

        srv = singleinst.bind_single_instance(on_show)
        try:
            assert srv is not None
            assert singleinst.notify_existing_instance() is True
            assert done.wait(5)                          # 转发在别的线程，等齐再断言
            assert received == ["SHOW"]
        finally:
            if srv is not None:
                srv.close()

    def test_notify_reports_failure_when_nobody_listens(self, monkeypatch):
        """无人监听（锁已释放但调用方还以为有实例）时如实返回 False，不假装送达。"""
        monkeypatch.setattr(singleinst, "SINGLE_PORT", _free_loopback_port())
        assert singleinst.notify_existing_instance() is False


# ---------- dialogs：WebView2 预检三分支与弹窗降级（_message_box 打桩，不真弹） ----------
class _FakeRegKey:
    def __init__(self, pv):
        self.pv = pv

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    """按 {根键: pv 值} 表驱动的 winreg 替身；缺根=键不存在，pv 为异常则查询抛错。"""

    HKEY_LOCAL_MACHINE = 0x80000002
    HKEY_CURRENT_USER = 0x80000001

    def __init__(self, table):
        self._table = table

    def OpenKey(self, root, sub):
        if sub != dialogs.WEBVIEW2_REG_SUB or root not in self._table:
            raise OSError("注册表键不存在")
        return _FakeRegKey(self._table[root])

    def QueryValueEx(self, key, name):
        if name != "pv":
            raise OSError("无 pv 值")
        if isinstance(key.pv, Exception):
            raise key.pv
        return key.pv, 1


class TestWebview2Detect:
    def _install(self, monkeypatch, table):
        monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(table))

    def test_absent_when_registry_keys_missing(self, monkeypatch):
        """两处注册表键都没有 → 明确未安装（False 会触发 missing 提示弹窗）。"""
        self._install(monkeypatch, {})
        assert dialogs.webview2_installed() is False

    def test_present_via_hkcu_when_hklm_missing(self, monkeypatch):
        """HKLM 缺、HKCU 有正常版本号 → 已安装（用户级安装是合法形态）。"""
        self._install(monkeypatch, {_FakeWinreg.HKEY_CURRENT_USER: "1.0.2311.32"})
        assert dialogs.webview2_installed() is True

    def test_placeholder_version_means_uncertain(self, monkeypatch):
        """键在但 pv 是 0.0.0.0 占位值 → 不确定（None），启动不被误阻断。"""
        fake_root = _FakeWinreg.HKEY_LOCAL_MACHINE
        self._install(monkeypatch, {fake_root: "0.0.0.0"})
        assert dialogs.webview2_installed() is None

    def test_query_error_means_uncertain(self, monkeypatch):
        """版本查询抛非 OSError 异常 → 一律按不确定处理，绝不让预检炸掉启动。"""
        fake_root = _FakeWinreg.HKEY_LOCAL_MACHINE
        self._install(monkeypatch, {fake_root: ValueError("注册表读崩")})
        assert dialogs.webview2_installed() is None


class TestMessageBoxDialogs:
    @staticmethod
    def _hook_box(monkeypatch, result=None, raises=None):
        """替换 _message_box 为记录桩：返回 result 或抛 raises，其余断言收集。"""
        calls = []

        def fake_box(text, caption, flags):
            calls.append((text, caption, flags))
            if raises is not None:
                raise raises
            return result

        monkeypatch.setattr(dialogs, "_message_box", fake_box)
        return calls

    @staticmethod
    def _hook_browser(monkeypatch):
        opened = []
        monkeypatch.setattr(dialogs.webbrowser, "open", opened.append)
        return opened

    def test_missing_dialog_yes_opens_download_page(self, monkeypatch):
        """缺失弹窗：文案点明未检测到、YESNO|ICONWARNING 组合，按是才开官方下载页。"""
        box = self._hook_box(monkeypatch, result=6)      # 6 = IDYES
        opened = self._hook_browser(monkeypatch)
        dialogs.webview2_dialog(missing=True)
        text, caption, flags = box[0]
        assert "未检测到" in text
        assert caption == WINDOW_TITLE
        assert flags == 0x34                             # MB_YESNO|MB_ICONWARNING
        assert opened == [WEBVIEW2_URL]

    def test_missing_dialog_no_does_not_open(self, monkeypatch):
        """用户按否（IDNO=7）：不打开下载页，不产生任何外跳。"""
        self._hook_box(monkeypatch, result=7)
        opened = self._hook_browser(monkeypatch)
        dialogs.webview2_dialog(missing=False)
        assert opened == []

    def test_dialog_exception_is_swallowed(self, monkeypatch):
        """弹窗 API 本身失败：降级为留日志，异常不得外泄炸掉托盘主流程。"""
        self._hook_box(monkeypatch, raises=RuntimeError("user32 失联"))
        self._hook_browser(monkeypatch)
        dialogs.webview2_dialog(missing=True)            # 不抛即通过

    def test_error_dialog_flags_and_swallows_failure(self, monkeypatch):
        """错误弹窗固定 MB_OK|MB_ICONERROR、标题用产品名；失败同样只留日志。"""
        box = self._hook_box(monkeypatch, result=1)
        dialogs.error_dialog("磁盘已满")
        assert box == [("磁盘已满", WINDOW_TITLE, 0x10)]
        self._hook_box(monkeypatch, raises=OSError("弹不出"))
        dialogs.error_dialog("再试一次")                  # 不抛即通过


# ---------- pin_desktop：WorkerW 查找与钉/取钉的状态翻转（user32 换替身） ----------
class _FakeUser32:
    """user32 替身：脚本化返回值驱动 pin_desktop，并记录关键调用供断言。"""

    def __init__(self):
        self.progman = 111            # 0 = FindWindowW 找不到 Progman
        self.defview = 222
        self.worker_behind = 555      # SHELLDLL_DefView 之后的同链 WorkerW；0=找不到
        self.top_worker = 777         # 兜底顶层枚举找到的 WorkerW；0=找不到
        self.class_names = {}         # 句柄 → 类名；空表=枚举不到 SHELLDLL_DefView
        self.window_rect_ok = True
        self.set_parent_result = 1
        self.get_style_boom = False
        self.style = 0x10000000 | 0x80000000   # WS_VISIBLE | WS_POPUP（贴纸窗口常态）
        self.calls = []

    def FindWindowW(self, cls, _wnd):
        return self.progman if cls == "Progman" else 0

    def SendMessageW(self, _hwnd, msg, _w, _l):
        self.calls.append(("SendMessageW", msg))
        return 0

    def GetClassNameW(self, hwnd, buf, _n):
        buf.value = self.class_names.get(hwnd, "Other")
        return len(buf.value)

    def FindWindowExW(self, parent, _after, cls, _wnd):
        if cls != "WorkerW":
            return 0
        return self.top_worker if parent is None else self.worker_behind

    def EnumChildWindows(self, _parent, callback, _lparam):
        callback(self.defview, 0)     # 以真实 ctypes 回调对象驱动一条枚举记录
        return 1

    def GetWindowRect(self, _hwnd, rect):
        if not self.window_rect_ok:
            return 0
        r = rect._obj
        r.left, r.top, r.right, r.bottom = 10, 20, 110, 140
        return 1

    def GetWindowLongW(self, _hwnd, _idx):
        if self.get_style_boom:
            raise RuntimeError("样式读取失败")
        return self.style

    def SetWindowLongW(self, _hwnd, idx, value):
        self.calls.append(("SetWindowLongW", idx, value))
        return self.style

    def SetParent(self, child, parent):
        self.calls.append(("SetParent", child, parent))
        return self.set_parent_result

    def SetWindowPos(self, _hwnd, after, x, y, w, h, flags):
        self.calls.append(("SetWindowPos", after, x, y, w, h, flags))
        return 1


@pytest.fixture()
def fake_user32(monkeypatch):
    fake = _FakeUser32()
    monkeypatch.setattr(pin_desktop, "user32", fake)
    return fake


class TestPinDesktop:
    def test_win32_message_constants_locked(self):
        """数值来自 Win32 SDK winuser.h，手误改错任何一位都会让钉桌面前功尽弃。"""
        assert pin_desktop._PROGMAN_SPAWN_MSG == 0x052C
        assert pin_desktop._GWL_STYLE == -16
        assert pin_desktop._WS_CHILD == 0x40000000
        assert pin_desktop._HWND_BOTTOM == 1

    def test_find_workerw_degrades_to_none(self, fake_user32):
        """找不到壁纸层（无 Progman / 枚举不到图标视图）一律 None，调用方回退置顶。"""
        fake_user32.progman = 0
        assert pin_desktop._find_workerw_behind_icons() is None
        fake_user32.progman = 111
        assert pin_desktop._find_workerw_behind_icons() is None
        # 只要走到枚举，0x052C 蜂鸣消息必须已发出（spawn WorkerW 是方案前提）
        assert ("SendMessageW", 0x052C) in fake_user32.calls

    def test_find_workerw_behind_icons_normal_chain(self, fake_user32):
        """标准链：DefView 所在 WorkerW 的下一个同级 WorkerW 即壁纸层。"""
        fake_user32.class_names = {222: "SHELLDLL_DefView"}
        assert pin_desktop._find_workerw_behind_icons() == 555

    def test_find_workerw_sibling_fallback_chain(self, fake_user32):
        """兜底链：某些构建 WorkerW 是 Progman 兄弟，同链找不到时枚举顶层窗口。"""
        fake_user32.class_names = {222: "SHELLDLL_DefView"}
        fake_user32.worker_behind = 0
        assert pin_desktop._find_workerw_behind_icons() == 777

    def test_pin_moves_back_and_clears_topmost(self, fake_user32, monkeypatch):
        """钉住成功链：样式去 WS_POPUP 加 WS_CHILD、挂到 WorkerW、移回原屏幕位并压底。"""
        monkeypatch.setattr(pin_desktop, "_find_workerw_behind_icons", lambda: 555)
        assert pin_desktop.pin_to_desktop(999) is True
        # 原窗口 0x10000000|0x80000000：清 WS_POPUP(0x80000000)、加 WS_CHILD(0x40000000)
        assert ("SetWindowLongW", -16, 0x50000000) in fake_user32.calls
        assert ("SetParent", 999, 555) in fake_user32.calls
        # 坐标取自钉前矩形 (10,20)-(110,140)；HWND_BOTTOM 压底、0x0004=SWP_NOACTIVATE
        assert ("SetWindowPos", 1, 10, 20, 100, 120, 0x0004) in fake_user32.calls

    def test_pin_fails_early_without_descent_actions(self, fake_user32, monkeypatch):
        """失败早退：取不到矩形或 SetParent 失败都返回 False，不产生误导性的后续动作。"""
        monkeypatch.setattr(pin_desktop, "_find_workerw_behind_icons", lambda: 555)
        fake_user32.window_rect_ok = False
        assert pin_desktop.pin_to_desktop(999) is False
        assert not any(c[0] in ("SetParent", "SetWindowLongW") for c in fake_user32.calls)
        fake_user32.window_rect_ok = True
        fake_user32.set_parent_result = 0
        assert pin_desktop.pin_to_desktop(999) is False

    def test_unpin_restores_topmost_state(self, fake_user32):
        """取钉成功链：摘 WS_CHILD、脱离父窗口，按 keep_on_top 恢复置顶(-1)/下沉(-2)。"""
        assert pin_desktop.unpin_from_desktop(999) is True
        assert ("SetWindowLongW", -16, 0x90000000) in fake_user32.calls   # 仅清 WS_CHILD
        assert ("SetParent", 999, None) in fake_user32.calls
        assert ("SetWindowPos", -1, 0, 0, 0, 0, 0x0053) in fake_user32.calls
        fake_user32.calls.clear()
        assert pin_desktop.unpin_from_desktop(999, keep_on_top=False) is True
        assert ("SetWindowPos", -2, 0, 0, 0, 0, 0x0053) in fake_user32.calls

    def test_unpin_reports_failure_safely(self, fake_user32):
        """取钉失败：SetParent 失败或样式读取异常都返回 False，不外泄异常。"""
        fake_user32.set_parent_result = 0
        assert pin_desktop.unpin_from_desktop(999) is False
        fake_user32.set_parent_result = 1
        fake_user32.get_style_boom = True
        assert pin_desktop.unpin_from_desktop(999) is False
