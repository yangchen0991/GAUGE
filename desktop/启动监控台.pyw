# -*- coding: utf-8 -*-
"""启动监控台.pyw — 无控制台入口（双击运行）

以 runpy 方式执行同目录 app.py 的 __main__ 流程。
pythonw 下 sys.std* 为 None，app.py 入口处已做兜底防护。
"""
import os
import runpy

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(_HERE, "app.py")

if __name__ == "__main__":
    runpy.run_path(_APP, run_name="__main__")
