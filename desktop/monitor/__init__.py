# -*- coding: utf-8 -*-
"""monitor — AI Agent 监控台桌面版的数据与配置层。

本包只承载**纯函数**（统计聚合、配置文件读写），便于 pytest 单元测试；
窗口/托盘生命周期代码保留在 app.py（与 spawn 机制强耦合）。
"""
