# -*- coding: utf-8 -*-
import sys
from pathlib import Path
# 同时把两个源码目录放进 sys.path，保证双口径可跑：
# - desktop\（parents[1]）：monitor 包所在，缺失时从仓库根跑
#   `python -m pytest desktop/tests -q` 收集失败（No module named monitor）
# - 仓库根（parents[2]）：refresh.py 所在，缺失时从 desktop\ 跑
#   `python -m pytest tests -q` 报 No module named refresh
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE.parents[2]))
