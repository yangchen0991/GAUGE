# -*- coding: utf-8 -*-
"""monitor — AI Agent 监控台桌面版的数据、配置与桌面壳支撑层。

纯函数层（pytest 覆盖）：stats（统计聚合）、config（配置文件读写）、
pin_desktop（窗口钉桌面）。

桌面壳支撑层（批次3a 自 app.py 拆分，依赖无环）：
  appenv（路径/常量/轮转日志）
    ← singleinst（单实例锁）、dialogs（WebView2 预检与原生弹窗）
    ← traycore（托盘状态/图标/通知/子进程命令发送）
    ← refreshctl（刷新编排：sidecar 主路径 + stats 直气回退）
  winchild（窗口子进程：webview 窗口 + Win32 毛玻璃/穿透 + 命令循环；
    window_process_main 为 multiprocessing spawn target，必须可被子进程 import）

入口 app.py 只保留菜单 actions、build_menu/start_tray、run() 主流程、
--smoke 冒烟与 main()。
"""
