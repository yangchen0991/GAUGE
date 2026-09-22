# -*- coding: utf-8 -*-
"""W8 双形态（成品单文件 / shell / sidecar）管线产出与 winchild 分派回归。

冻结契约的测试面：
1. 管线产出三件齐全：成品内嵌非 null 数据且注入 GAUGE_CODEX；shell 占位为
   null 且带 fetch 分支；sidecar 顶层含 codex（GAUGE_CODEX 快照对象）与
   data（页面 DATA 契约字段）。
2. sidecar 既有键集合与基线一致（防漂移）。
3. RELOAD 分派：shell 窗口 state 标记下 evaluate_js 调 __gaugeApplyLatest；
   回退模式下调 location.reload()。
4. 主面板创建：内置静态服务不支持（签名探测为否）或 create_window 抛
   TypeError 时，回退加载成品单文件（file:// 快照现状路径）。

隔离手法与 test_dual_export / test_stage45_linkage 一致：假 DB + 假
collect_codex + 假 pipe / 假 webview，不触碰真实会话库与真实窗口。
"""
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import gauge_data
import refresh
from monitor import winchild

# sidecar 基线键（2026-09-22 冻结）：schema_version=2 时代的全部既有顶层键。
# 本批新增只允许 data（页面 DATA 契约字段）与 codex（GAUGE_CODEX 快照）两个键。
SIDECAR_BASELINE_KEYS = [
    "generated_at", "db_display", "today", "week", "last_error", "pricing",
    "plan", "window5h", "thisweek", "platform", "status", "schema_version",
    "platforms",
]


def _fixture_db(path: str) -> None:
    """构造最小隔离库：两会话、三请求（含错误/取消）、两工具行，时间都在当日。"""
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE model_usage (session_id TEXT, provider_id TEXT, model_id TEXT,
            agent TEXT, status TEXT, started_at INTEGER, duration_ms INTEGER,
            time_to_first_token_ms INTEGER, finish_reason TEXT, retry_count INTEGER,
            cancelled_by_user INTEGER, error_type TEXT, input_tokens INTEGER,
            output_tokens INTEGER, cache_read_input_tokens INTEGER);
        CREATE TABLE tool_usage (session_id TEXT, tool_name TEXT, status TEXT,
            duration_ms INTEGER, read_only INTEGER, destructive INTEGER,
            cancelled_by_user INTEGER);
        """
    )
    conn.execute("INSERT INTO session VALUES ('s1','会话一','J:/proj/a',?,?)",
                 (now_ms - 3_600_000, now_ms - 60_000))
    conn.execute("INSERT INTO session VALUES ('s2','会话二','J:/proj/b',?,?)",
                 (now_ms - 7_200_000, now_ms - 120_000))
    conn.executemany(
        "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("s1", "builtin:bigmodel-coding-plan", "GLM-5.3", "zcode-agent",
             "completed", now_ms - 3_000_000, 1200, 300, "stop", 0, 0, None,
             1000, 200, 100),
            ("s1", "builtin:bigmodel-coding-plan", "GLM-5.3-Flash", "zcode-agent",
             "error", now_ms - 2_000_000, 900, 250, "stop", 1, 0, "rate_limit",
             500, 50, 0),
            ("s2", "account:bigmodel-individual-coding-plan", "GLM-5.3", "zcode-agent",
             "cancelled", now_ms - 1_000_000, 800, 200, "tool-calls", 0, 1, None,
             300, 30, 30),
        ],
    )
    conn.execute("INSERT INTO tool_usage VALUES ('s1','Bash','success',120,0,0,0)")
    conn.execute("INSERT INTO tool_usage VALUES ('s2','Read','success',40,1,0,0)")
    conn.commit()
    conn.close()


def _codex_snapshot() -> dict:
    """脱敏 Codex 快照（形状与 collect_codex 输出一致，见 test_dual_export）。"""
    return {
        "platform": "codex", "available": True, "status": "ready",
        "generated_at": "2026-09-22T00:00:00+08:00", "generated_at_ms": 1789999200000,
        "threads": [{"id": "task-1", "title": "示例任务"}], "turns": [], "usage": [],
        "tools": [], "edges": [], "projects": [], "goals": [], "quota": None,
        "coverage": {"complete": True}, "warnings": [], "environment": {},
        "diagnostics": {}, "sources": {},
    }


@pytest.fixture()
def shell_triple(tmp_path, monkeypatch):
    """跑通整条管线，返回三件产物路径与注入的 codex 样本。"""
    db = tmp_path / "fixture.sqlite"
    _fixture_db(str(db))
    sample = _codex_snapshot()
    monkeypatch.setattr(gauge_data, "collect_codex", lambda: sample)
    monkeypatch.setattr(refresh, "DB_PATH", str(db))
    out = tmp_path / "mon.html"
    monkeypatch.setattr(refresh, "OUTPUT_PATH", str(out))
    assert refresh.main() == 0
    return {
        "out": out,
        "shell": Path(str(out)[:-5] + ".shell.html"),
        "sidecar": Path(str(out)).with_suffix(".data.json"),
        "codex": sample,
    }


def _extract_after(text: str, marker: str):
    """从成品脚本里 marker 之后原样解码一个 JSON 值（与注入端同口径转义）。

    用 rsplit 取最后一处命中：页面脚本（hydrateData 等）里也有
    window.GAUGE_CODEX= 字样，真正的注入脚本位于文末 PLATFORM_DATA 占位处。
    """
    head = text.rsplit(marker, 1)
    assert len(head) == 2, "缺少注入标记：" + marker
    value, _ = json.JSONDecoder().raw_decode(head[1].lstrip())
    return value


def test_pipeline_produces_three_artifacts(shell_triple):
    """三件齐全：成品内嵌非 null、shell 占位 null 带 fetch 分支、sidecar 双新键。"""
    html = shell_triple["out"].read_text(encoding="utf-8")
    assert "/*__DATA_PLACEHOLDER__*/" not in html
    assert html.count('<button data-tab="') == 6, "成品页签数必须保持 6"
    data = _extract_after(html, "const DATA = ")
    assert data is not None and len(data["sessions"]) == 2
    codex = _extract_after(html, "window.GAUGE_CODEX=")
    assert codex == shell_triple["codex"], "成品 GAUGE_CODEX 注入语义不变"

    shell = shell_triple["shell"].read_text(encoding="utf-8")
    assert shell_triple["shell"].exists()
    assert "var DATA = null;" in shell, "shell 数据占位符必须置 null"
    assert "window.GAUGE_CODEX=null;" in shell, "shell 平台桥占位必须置 null"
    assert "__gaugeApplyLatest" in shell and "AI-Agent监控台.data.json" in shell \
        and "location.protocol" in shell, "shell 必须携带 fetch 装载分支"
    assert shell.count('<button data-tab="') == 6, "shell 与成品同一模板（页签数一致）"
    assert 'id="ov-kpis"' in shell and 'id="ov-kpis"' in html

    sidecar = json.loads(shell_triple["sidecar"].read_text(encoding="utf-8"))
    assert sidecar["codex"] == shell_triple["codex"], "顶层 codex = GAUGE_CODEX 快照"
    page = sidecar["data"]
    assert page["meta"]["generated_at"] == sidecar["generated_at"], \
        "sidecar.generated_at 兼作 revision 口径（与页面 meta 同值）"
    assert len(page["sessions"]) == 2 and len(page["requests"]) == 3
    assert "codex" not in page, "大载荷单副本：页面 DATA 不携带 codex（与成品内嵌同口径）"


def test_sidecar_baseline_keys_no_drift(shell_triple):
    """既有键集合与基线一致：零删除、零改名、schema_version 不动、新增键可控。"""
    sidecar = json.loads(shell_triple["sidecar"].read_text(encoding="utf-8"))
    keys = set(sidecar)
    assert set(SIDECAR_BASELINE_KEYS) <= keys, "基线键必须全部保留"
    assert keys - set(SIDECAR_BASELINE_KEYS) == {"data", "codex"}, "新增键只允许 data/codex"
    assert sidecar["schema_version"] == 2
    assert len(sidecar["week"]) == 7
    assert set(sidecar["pricing"]["GLM-5.3"]) == {"input", "cache_read", "output"}
    assert set(sidecar["platforms"]) == {"zcode", "codex"}
    assert set(sidecar["week"][0]) == {"d", "full", "req", "cost"}


# ---- RELOAD 分派与主面板创建（假 pipe / 假窗口，手法同 test_stage45_linkage） ----

class _FakePipe:
    """命令循环假 pipe：recv 队列耗尽后抛 OSError 触发退出。"""

    def __init__(self, commands=None):
        self.sent = []
        self._incoming = list(commands or [])

    def send(self, message):
        self.sent.append(message)

    def poll(self, timeout=None):
        return True

    def recv(self):
        if not self._incoming:
            raise OSError("管道断开（测试退出）")
        return self._incoming.pop(0)


class _FakeEvents:
    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def is_set(self):
        return False

    def set(self):
        pass

    def clear(self):
        pass


class _FakeWindow:
    def __init__(self, label="win"):
        self.label = label
        self.events = SimpleNamespace(loaded=_FakeEvents(), closing=_FakeEvents(),
                                      shown=_FakeEvents(), closed=_FakeEvents(),
                                      moved=_FakeEvents(), resized=_FakeEvents())
        self.scripts = []
        self.destroyed = False

    def evaluate_js(self, script):
        self.scripts.append(script)

    def show(self):
        pass

    def hide(self):
        pass

    def destroy(self):
        self.destroyed = True


def _drive(state, commands):
    winchild._window_cmd_loop(state, _FakePipe(list(commands)))


def test_reload_dispatch_shell_and_fallback():
    """RELOAD 分派：shell 标记调 __gaugeApplyLatest；回退模式保持 location.reload()。"""
    state = winchild._WindowState()
    state.window = _FakeWindow()
    state.window_shell_mode = True
    _drive(state, ["RELOAD"])
    assert state.window.scripts == ["window.__gaugeApplyLatest && window.__gaugeApplyLatest();"]

    state = winchild._WindowState()
    state.window = _FakeWindow()
    state.window_shell_mode = False
    _drive(state, ["RELOAD"])
    assert state.window.scripts == ["location.reload()"], "回退模式必须保持现状整页重载"


class _FakeWebView:
    """webview 替身：按脚本返回窗口；可注入首个调用抛 TypeError。"""

    def __init__(self, fail_first=False):
        self.calls = []
        self.start_kwargs = []
        self._fail_first = fail_first

    def create_window(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._fail_first and len(self.calls) == 1:
            raise TypeError("create_window() got an unexpected keyword argument")
        return _FakeWindow("widget" if len(self.calls) > 1 else "main")

    def start(self, http_server=False, **kwargs):
        # 显式声明 http_server 形参：生产实现会先探测 start 签名再决定传参
        self.start_kwargs.append(dict(kwargs, **({"http_server": True} if http_server else {})))


def _run_window_process(monkeypatch, tmp_path, fake_webview, supported):
    html_path = tmp_path / "AI-Agent监控台.html"
    html_path.write_text("<html>成品</html>", encoding="utf-8")
    shell_path = tmp_path / "AI-Agent监控台.shell.html"
    shell_path.write_text("<html>shell</html>", encoding="utf-8")
    monkeypatch.setattr(winchild, "HTML_PATH", html_path)
    monkeypatch.setattr(winchild, "_builtin_http_server_supported", lambda: supported)
    monkeypatch.setattr(winchild, "webview", fake_webview)
    code = winchild.window_process_main(_FakePipe(), None)
    return code, html_path, shell_path


def test_main_window_prefers_shell_with_builtin_server(monkeypatch, tmp_path):
    """内置静态服务可用且 shell 存在：主面板加载 shell（普通本地路径 + http_server）。"""
    fake = _FakeWebView()
    code, html_path, shell_path = _run_window_process(monkeypatch, tmp_path, fake, True)
    assert code == 0
    main_args, main_kwargs = fake.calls[0]
    assert main_args[1] == str(shell_path), "shell 优先：普通本地路径（6.x 自动静态服务）"
    assert "http_server" not in main_kwargs
    assert fake.start_kwargs == [{"http_server": True}]
    assert fake.calls[1][0][1] == winchild.WIDGET_HTML_PATH.as_uri(), "贴纸窗口零改动（file:// URI）"


def test_main_window_falls_back_on_type_error(monkeypatch, tmp_path):
    """create_window 抛 TypeError：回退加载成品单文件，且不带 http_server 参数。"""
    fake = _FakeWebView(fail_first=True)
    code, html_path, shell_path = _run_window_process(monkeypatch, tmp_path, fake, True)
    assert code == 0
    fallback_args, fallback_kwargs = fake.calls[1]
    assert fallback_args[1] == html_path.as_uri(), "回退必须加载成品单文件"
    assert "http_server" not in fallback_kwargs
    assert fake.start_kwargs == [{}]
    assert fake.calls[2][0][1] == winchild.WIDGET_HTML_PATH.as_uri(), "贴纸窗口不受影响"


def test_main_window_falls_back_when_unsupported(monkeypatch, tmp_path):
    """签名探测判定不支持内置静态服务：直接按现状加载成品单文件。"""
    fake = _FakeWebView()
    code, html_path, shell_path = _run_window_process(monkeypatch, tmp_path, fake, False)
    assert code == 0
    main_args, main_kwargs = fake.calls[0]
    assert main_args[1] == html_path.as_uri()
    assert "http_server" not in main_kwargs
    assert fake.start_kwargs == [{}]


def test_shell_ui_suite():
    """拉起 node/Playwright UI 套件（test_shell_ui.mjs），纳入 pytest 统一入口。

    与 test_codex_ui_runner 同一手法：pytest 只负责运行与透传结果；mjs 内部
    自建隔离管线与本地静态服务。退出码 75 = 浏览器/Python 环境不可用，按环境
    缺失跳过而非误报红灯。
    """
    import shutil
    import subprocess

    script = Path(__file__).with_name("test_shell_ui.mjs")
    if shutil.which("node") is None:
        pytest.skip("node 不可用，跳过 shell UI 套件")
    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if result.returncode == 75:
        pytest.skip("Playwright/Python 环境不可用，跳过 shell UI 套件：\n%s%s"
                    % (result.stdout, result.stderr))
    assert result.returncode == 0, (
        "shell UI 套件失败：\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (result.stdout, result.stderr)
    )
