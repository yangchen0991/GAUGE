# -*- coding: utf-8 -*-
"""appenv — 桌面壳的路径/常量环境层（批次3a 自 app.py 拆分，无包内依赖）。

冻结（exe）与源码两种形态的路径解析：
  源码：APP_DIR=desktop 目录（含 widget.html 等资源，同目录可写）
  exe ：APP_DIR=exe 所在目录（可写：日志/配置/成品 HTML）；RESOURCE_DIR=解包资源目录

日志：双进程写同一 desktop/app.log；FileHandler 改为 RotatingFileHandler
（单文件上限 1MB，保留 3 个历史备份），避免长期驻留时日志无限增长，
其余行为/格式/控制台逻辑与原实现保持一致。
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional

# ---------- 路径常量 ----------
if getattr(sys, "frozen", False):
    APP_DIR: Path = Path(sys.executable).resolve().parent
    RESOURCE_DIR: Path = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent.parent      # desktop 目录（本文件在 monitor/ 下）
    RESOURCE_DIR = APP_DIR
PARENT_DIR: Path = APP_DIR.parent
# 成品 HTML：源码形态由 refresh.py 生成在 agent-monitor 根；exe 形态在 exe 同目录
HTML_PATH: Path = (APP_DIR if getattr(sys, "frozen", False) else PARENT_DIR) / "AI-Agent监控台.html"
# 桌面统计链 sidecar：refresh.py 随成品 HTML 原子写（同目录同名主干，见 refresh.write_sidecar）
SIDECAR_PATH: Path = HTML_PATH.with_name(HTML_PATH.name[:-5] + ".data.json")
WIDGET_HTML_PATH: Path = RESOURCE_DIR / "widget.html"
REFRESH_PY: Path = (PARENT_DIR / "refresh.py") if not getattr(sys, "frozen", False) \
    else (RESOURCE_DIR / "refresh.py")
LOG_PATH: Path = APP_DIR / "app.log"
ICO_PATH: Path = APP_DIR / "monitor.ico"
SMOKE_PATH: Path = APP_DIR / "smoke_result.json"

# ---------- 应用常量 ----------
# 命名依据 brand/01-命名方案.md：产品名 GAUGE（全大写）+ 中文单字「衡」+ 副题「AI Agent 监控台」
WINDOW_TITLE = "GAUGE 衡 · AI Agent 监控台"
WIDGET_TITLE = "GAUGE 衡 · 贴纸"
SINGLE_PORT = 59321
REFRESH_TIMEOUT = 600          # 秒，与 refresh.py 的超时口径一致
NOTIFY_MAX = 200               # 失败气泡最多展示 stderr 尾部 200 字
CHILD_JOIN_TIMEOUT = 10        # 退出时等待窗口子进程的秒数
WIDGET_OPACITY_CHOICES = (("60%", 0.60), ("75%", 0.75), ("90%", 0.90))
WIDGET_BACKDROP_MIN_BUILD = 22621   # DWMWA_SYSTEMBACKDROP_TYPE 的最低 Win11 build

# 自动刷新间隔选项（label, 秒）；0 = 关闭。默认 5 分钟。
INTERVAL_CHOICES = (("5 分钟", 300), ("15 分钟", 900), ("30 分钟", 1800), ("关闭", 0))

_log = logging.getLogger("agent_monitor")


def log(msg: str) -> None:
    """写一条 INFO 日志（带进程前缀，双进程写同一 app.log）。"""
    _log.info(msg)


def setup_logging(tag: Optional[str] = None) -> None:
    """tag 用于区分窗口子进程日志（如 [win]），避免双进程日志无法归属。"""
    handlers: List[logging.Handler] = []
    try:
        # 轮转写 app.log：单文件上限 1MB、保留 3 个历史（app.log.1~3）；
        # delay=True 保持原 FileHandler 的惰性打开语义（仅在首条日志时建文件）
        handlers.append(RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3,
            encoding="utf-8", delay=True))
    except Exception:
        pass
    if sys.stdout is not None:
        try:
            handlers.append(logging.StreamHandler(sys.stdout))
        except Exception:
            pass
    prefix = ("[%s] " % tag) if tag else ""
    logging.basicConfig(
        level=logging.INFO,
        format="%%(asctime)s [%%(levelname)s] %s%%(message)s" % prefix,
        handlers=handlers,
    )
