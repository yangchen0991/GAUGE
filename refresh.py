#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh.py — 从本机 ZCode 会话库（只读）抽取统计数据，注入 AI-Agent监控台.html

- 仅使用 Python 标准库；绝不写数据库（mode=ro + query_only 双重保证）。
- 内置自检：行扫描与聚合 SQL 两条独立路径核对通过才写成品文件。
- 失败时不产出损坏 HTML：全部校验通过后才原子替换（保留旧文件）。

main() 只做流程编排；产出三件（W8 双形态）：
  ①成品单文件 AI-Agent监控台.html（现状语义不变：内嵌全部数据，浏览器快照模式）
  ②shell 形态 AI-Agent监控台.shell.html（同一模板同源注入，数据占位与
   GAUGE_CODEX 桥置 null，供 winchild 内置静态服务加载后 fetch sidecar）
  ③sidecar AI-Agent监控台.data.json（既有键之上 additive 追加顶层 data=页面
   DATA 契约字段、codex=GAUGE_CODEX 快照；schema_version 不动，停写保留语义不变）

main() 只做流程编排；九步主流程各自封装为单一职责函数：
  load_template          步骤 1  读取模板并校验数据占位符
  open_db                步骤 2  只读建 WAL 快照连接
  scan_model_usage       步骤 3a 行扫描 model_usage（路径 A）
  scan_tool_usage        步骤 3b 行扫描 tool_usage（路径 A）
  selfcheck              步骤 4  聚合 SQL（路径 B）与路径 A 逐项核对
  build_payload          步骤 5  组装 DATA（契约见下）
  serialize_and_inject   步骤 6  JSON 回读验证 + 转义注入 + 外链检查
  node_syntax_check      步骤 6b  成品/壳内联 JS 语法校验（独立哈希缓存）
  atomic_write           步骤 7  tmp + os.replace 原子写入
  build_shell_html       步骤 7b  生成 shell 形态（成品同模板同源注入）
  write_sidecar          步骤 8  原子写桌面 sidecar（AI-Agent监控台.data.json）

DATA 契约（template.html 消费端）：
  meta/agents/providers/models/pricing/pricing_usd/sessions/requests/tools/agg，
  字段语义详见 build_payload 内注释与 desktop/README.md。

sidecar 契约（desktop/app.py 消费端，见 write_sidecar）：
  generated_at/db_display/today/week/last_error/pricing/plan/window5h/thisweek，
  与 desktop/monitor/stats.py 的 widget_stats 输出形状一致；
  plan/window5h/thisweek 为 GAUGE 官方积分窗口口径（2026-09-20 冻结，末尾追加，
  旧键与顺序不变），与 stats.py 双链共用同一套常数（测试锁一致）；
  sidecar 写失败仅警告不影响退出码（HTML 是主交付物，桌面侧有 stats 直查回退）。
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "template.html")
OUTPUT_PATH = os.path.join(BASE_DIR, "AI-Agent监控台.html")
# 测试钩子：AGENT_MONITOR_DB 环境变量可覆盖数据库路径（默认本机活库）
DB_PATH = os.environ.get("AGENT_MONITOR_DB") or os.path.expanduser("~/.zcode/cli/db/db.sqlite")
DB_DISPLAY = DB_PATH + "（只读导出）"
PLACEHOLDER = "/*__DATA_PLACEHOLDER__*/null"

# node --check 子进程的 Windows 创建旗标（导入期用真实 subprocess 解析一次）。
# 冻结 exe 为 GUI 子系统、无控制台父进程，Windows 会为 node（控制台子系统）
# 子进程分配新控制台窗口——真机每次刷新黑窗闪现的根因。capture_output 已通
# 过管道接管输出，无窗口不影响采集；非 Windows 传 0 即无任何创建旗标，行为
# 与原先完全一致。
_NODE_CHECK_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ---- 显示名映射（唯一权威定义；template.html 已无回退映射、直接消费 DATA 里的
# label，原始 id 仍始终保留在 DATA.agents[].id / providers[].id 中）----
AGENT_LABELS = {
    "zcode-agent": "主会话（Astra）",
    "zcode-astra-luna:luna-worker": "Luna Worker",
    "zcode-astra-luna:reviewer": "独立 Reviewer",
    "zcode-general-purpose": "通用子代理",
    "zcode-Explore": "Explore 只读探查",
}
PROVIDER_LABELS = {
    "builtin:bigmodel-coding-plan": "智谱 Coding 套餐",
    "account:bigmodel-individual-coding-plan": "智谱个人 Coding 套餐",
    "builtin:bigmodel-start-plan": "智谱 Start 套餐",
    "builtin:zai-start-plan": "Z.ai Start 套餐",
}

PRICE_AS_OF = "2026-09-18"
PRICE_NOTE = ("按官方目录价估算；Coding Plan 订阅实际按积分配额计费，此估算 ≠ 现金账单；"
              "缓存存储限时免费按 0 计；价格检索日期 2026-09-18")
SRC_BIGMODEL = {"name": "智谱开放平台官方定价（docs.bigmodel.cn）",
                "url": "https://docs.bigmodel.cn/cn/guide/start/pricing"}
SRC_ZAI = {"name": "Z.ai 官方定价（docs.z.ai，USD 参考）",
           "url": "https://docs.z.ai/guides/overview/pricing"}

PRICING_CNY = {
    "currency": "CNY", "as_of": PRICE_AS_OF, "unit": "元/百万token",
    "sources": [SRC_BIGMODEL], "note": PRICE_NOTE,
    "rows": [
        {"model": "GLM-5.3", "input": 8, "output": 28, "cache_read": 2, "cache_write": 0},
        {"model": "GLM-5.3-Flash",
         "input": 0.8, "output": 2.8, "cache_read": 0.23, "cache_write": 0},
    ],
}
PRICING_USD = {
    "as_of": PRICE_AS_OF,
    "sources": [SRC_ZAI],
    "note": "两平台价格互不换算，仅作参考；默认估算使用 CNY 目录价。",
    "rows": [
        {"model": "GLM-5.3", "input": 1.40, "output": 4.40, "cache_read": 0.26, "cache_write": 0},
        {"model": "GLM-5.3-Flash",
         "input": 0.15, "output": 0.50, "cache_read": 0.03, "cache_write": 0},
    ],
}

# ---------- GAUGE 官方积分口径（2026-09-20 检索冻结；与 desktop/monitor/stats.py
# 双链共用同一套常数，一致性由 desktop/tests/test_stats.py 的双链漂移锁测试守护） ----------
# 积分系数（积分/百万 token）：model_id -> (输入, 缓存读取, 输出)；未列出模型按 0 计。
CREDIT_COEFFS = {
    "GLM-5.3": (6.9, 1.7, 24.0),
    "GLM-5.3-Flash": (2.3, 0.56, 8.0),
}
# 积分换算除数：系数语义=积分/百万 token；官方文档字面为 /10000，与官方 V2→V3
# 迁移等价关系及实测数据矛盾，工程判定取 /1e6（2026-09-20 研究判定，非官方确认）。
CREDIT_DIVISOR = 1_000_000
# 套餐档位额度（积分）：tier -> (5h 窗口额度, 周额度)；默认 lite。
PLAN_QUOTAS = {
    "lite": (2000, 10000),
    "pro": (12000, 60000),
    "max": (28000, 140000),
}
DEFAULT_PLAN_TIER = "lite"


def die(msg):
    """打印中文错误信息并以非零退出码终止脚本（不产出任何成品文件）。"""
    print("[刷新失败] " + msg, file=sys.stderr)
    sys.exit(1)


def clean(s):
    """用户字符串安全化：去掉非法代理对/无法编码字符。"""
    if s is None:
        return None
    if not isinstance(s, str):
        s = str(s)
    return s.encode("utf-8", "replace").decode("utf-8")


def norm(v):
    """维度值归一化：None/空 → 占位，保证可索引可显示。"""
    if v is None:
        return "(未知)"
    s = str(v).strip()
    return s if s else "(未知)"


def st_of(status, cancelled_by_user):
    """status → 0=completed, 1=error, 2=cancelled(含 cancelled_by_user=1)。

    cancelled_by_user=1 但 status='completed' 的行归为 2（取消），不计为错误。
    status 为 NULL/其他值按 completed 处理（本库实际无 NULL status）。
    """
    s = (status or "").strip().lower()
    if s == "error":
        return 1
    if s == "cancelled" or cancelled_by_user:
        return 2
    return 0


def fin_of(f):
    """finish_reason 归一化：stop→0（自然结束）、tool-calls→1（调用工具）、其他/NULL→2。

    该映射写入 DATA.requests[].fin（JSON 契约的一部分），供页面按需消费；
    template.html「数据说明」页的字段约定表记录了同一套 0/1/2 值。
    """
    if f == "stop":
        return 0
    if f == "tool-calls":
        return 1
    return 2


def node_syntax_check(html_text, check_path=None):
    """抽取成品 HTML 中内联 <script>，用 node --check 做语法检查。无 node 时跳过。

    条件化：逐 script 先算成品内容 sha1，与 <产物路径>.nodecheck 内记录一致
    则跳过 spawn；不一致或哈希文件损坏/缺失才校验，校验后回写全量哈希。
    哈希文件是可再生缓存，写坏只损失一次跳过机会，不影响正确性。
    check_path 可为 shell 形态指定独立缓存文件，避免与成品的哈希缓存互相覆盖。
    """
    node = shutil.which("node")
    if not node:
        print("[警告] 未找到 node，跳过 JS 语法检查（不影响数据正确性）。")
        return None
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html_text, re.S | re.I)
    if not scripts:
        die("成品 HTML 中未找到内联 <script>，模板可能已损坏。")
    if check_path is None:
        check_path = OUTPUT_PATH + ".nodecheck"
    stored = None
    try:
        parsed = json.loads(Path(check_path).read_text(encoding="utf-8"))
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            stored = parsed
    except (OSError, ValueError):
        stored = None
    hashes = [hashlib.sha1(code.encode("utf-8")).hexdigest() for code in scripts]
    spawned = False
    for i, code in enumerate(scripts):
        if stored is not None and i < len(stored) and stored[i] == hashes[i]:
            continue
        fd, tmp = tempfile.mkstemp(suffix=".js", dir=BASE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code)
            # 旗标在模块级用真实 subprocess 解析（见 _NODE_CHECK_CREATIONFLAGS），
            # 子进程静默不闪控制台窗口。
            r = subprocess.run([node, "--check", tmp], capture_output=True,
                               text=True, creationflags=_NODE_CHECK_CREATIONFLAGS)
            if r.returncode != 0:
                die("JS 语法检查失败（script #%d，node --check）：\n%s" % (i, (r.stderr or r.stdout)[:2000]))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        spawned = True
    if spawned or stored != hashes:
        tmp_path = check_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(hashes, f)
        os.replace(tmp_path, check_path)
    return True


def open_db():
    """以只读方式打开 ZCode 会话库并返回连接。

    双重只读保险：URI `mode=ro`（打开层面禁止写）+ `PRAGMA query_only=1`
    （会话层面禁止任何写语句）。开启 busy_timeout 应对宿主应用并发写入；
    `BEGIN DEFERRED` 建立 WAL 读快照，保证"行扫描"与"聚合 SQL"两条自检
    路径看到同一份数据。失败时 die（中文报错，保留旧成品文件）。
    """
    if not os.path.exists(DB_PATH):
        die("未找到数据库文件：%s。请确认 ZCode 已在本机安装并运行过。" % DB_PATH)
    try:
        uri = Path(os.path.abspath(DB_PATH)).as_uri() + "?mode=ro"
    except Exception as e:  # noqa: BLE001
        die("数据库路径无法转换为只读 URI：%s" % e)
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.isolation_level = None  # 显式事务管理
        # 活库正被 ZCode 写入时等锁（最多 10s）而不是整轮刷新直接失败——
        # 刷新是低频批处理、等得起。沿用值，调整前需实测宿主写事务时长分布。
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA query_only=1")   # 运行时禁止任何写操作
        conn.execute("BEGIN DEFERRED")        # WAL 快照：两条自检路径看到同一份数据
    except sqlite3.Error as e:
        die("无法以只读方式打开数据库（可能正被 ZCode 占用，请稍后重试）。\n详细：%s" % e)
    return conn


# ---------- 步骤 1：模板 ----------
def load_template():
    """读取模板文件并校验数据占位符恰好出现一次。返回模板全文。"""
    if not os.path.exists(TEMPLATE_PATH):
        die("未找到模板文件：%s" % TEMPLATE_PATH)
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        tpl = f.read()
    n_ph = tpl.count(PLACEHOLDER)
    if n_ph != 1:
        die("模板中数据占位符出现 %d 次（应恰好为 1 次），模板可能已损坏。" % n_ph)
    for marker, filename in (("/*__CODEX_STYLE__*/", "codex.css"),
                             ("/*__CODEX_SCRIPT__*/", "codex.js")):
        if marker in tpl:
            asset = Path(BASE_DIR) / "web" / filename
            tpl = tpl.replace(marker, asset.read_text(encoding="utf-8"))
    return tpl


# ---------- 步骤 3a：model_usage 行扫描（自检路径 A 的一半） ----------
def scan_model_usage(conn, sess_idx, n_sess, orphan_idx=None):
    """全量行扫描 model_usage，聚合出会话/Agent/Provider/模型四类统计。

    orphan_idx：「未知会话」合成行在 sess_rows 中的下标（无孤儿时为 None）；
    孤儿行（session_id 不在 session 表）计入该下标的会话累计并进入请求明细。
    返回 dict：
      agg        全局计数器（req/i/o/cr/st 三态）
      req_raw    请求明细行列表（供 build_payload 生成 DATA.requests）
      s_acc      按会话累计 {r,i,o,cr,ok,err,canc,retry}
      s_ag/s_md  按会话出现过的 agent/model 名集合
      orphans    session_id 不在 session 表中的孤儿行数（已归入合成行）
      min_t/max_t 请求时间范围（毫秒 epoch；全空为 None）
      err_types  error_type 非空计数（供 agg.err_types）
    """
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    s_acc = [{"r": 0, "i": 0, "o": 0, "cr": 0, "ok": 0, "err": 0, "canc": 0, "retry": 0}
             for _ in range(n_sess)]
    s_ag = [set() for _ in range(n_sess)]
    s_md = [set() for _ in range(n_sess)]

    mu_rows = q(
        "SELECT session_id, provider_id, model_id, agent, status, started_at, duration_ms, "
        "time_to_first_token_ms, finish_reason, retry_count, cancelled_by_user, error_type, "
        "input_tokens, output_tokens, cache_read_input_tokens FROM model_usage"
    )
    agg = {"req": 0, "i": 0, "o": 0, "cr": 0, "st": [0, 0, 0]}
    agent_cnt, prov_cnt, model_cnt, err_types = {}, {}, {}, {}
    req_raw = []
    orphans = 0
    min_t = max_t = None
    for (sid, prov, model, agent, status, t, d, tt, fin, retry, cbu, et, i, o, cr) in mu_rows:
        agg["req"] += 1
        agg["i"] += i or 0
        agg["o"] += o or 0
        agg["cr"] += cr or 0
        st = st_of(status, cbu)
        agg["st"][st] += 1
        if t is not None:
            if min_t is None or t < min_t:
                min_t = t
            if max_t is None or t > max_t:
                max_t = t
        ak, pk, mk = norm(agent), norm(prov), norm(model)
        agent_cnt[ak] = agent_cnt.get(ak, 0) + 1
        prov_cnt[pk] = prov_cnt.get(pk, 0) + 1
        model_cnt[mk] = model_cnt.get(mk, 0) + 1
        if et is not None and str(et).strip() != "":
            err_types[str(et)] = err_types.get(str(et), 0) + 1
        si = sess_idx.get(sid)
        if si is None:
            orphans += 1
            si = orphan_idx
            if si is None:
                # 防御路径：调用方未提供合成行下标（快照不一致）时保留旧行为——
                # 跳过明细与按会话累计；该不一致由 selfcheck 的明细对账兜底拦截
                continue
        acc = s_acc[si]
        acc["r"] += 1
        acc["i"] += i or 0
        acc["o"] += o or 0
        acc["cr"] += cr or 0
        if st == 0:
            acc["ok"] += 1
        elif st == 1:
            acc["err"] += 1
        else:
            acc["canc"] += 1
        if (retry or 0) > 0:
            acc["retry"] += 1
        s_ag[si].add(ak)
        s_md[si].add(mk)
        req_raw.append((sid, pk, mk, ak, status, t, d, tt, fin, cbu, et, i, o, cr))
    return {
        "agg": agg, "req_raw": req_raw, "s_acc": s_acc, "s_ag": s_ag, "s_md": s_md,
        "orphans": orphans, "min_t": min_t, "max_t": max_t, "err_types": err_types,
        "agent_cnt": agent_cnt, "prov_cnt": prov_cnt, "model_cnt": model_cnt,
    }


# ---------- 步骤 3b：tool_usage 行扫描（自检路径 A 的另一半） ----------
def scan_tool_usage(conn, sess_idx, n_sess):
    """全量行扫描 tool_usage，聚合出工具维度与会话维度统计。

    返回 dict：tool_acc（按工具名）、total（总行数）、t_acc（按会话 {tc,te}）。
    """
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    t_acc = [{"tc": 0, "te": 0} for _ in range(n_sess)]
    tu_rows = q(
        "SELECT session_id, tool_name, status, duration_ms, read_only, destructive, "
        "cancelled_by_user FROM tool_usage"
    )
    tool_acc = {}
    total = 0
    for (sid, name, status, d, ro, de, ca) in tu_rows:
        total += 1
        nm = clean(name) or "(未知)"
        e = tool_acc.setdefault(
            nm, {"calls": 0, "errs": 0, "dsum": 0, "ro": 0, "destr": 0, "canc": 0})
        e["calls"] += 1
        if status == "error":
            e["errs"] += 1
        e["dsum"] += d or 0
        if ro:
            e["ro"] += 1
        if de:
            e["destr"] += 1
        if ca:
            e["canc"] += 1
        si = sess_idx.get(sid)
        if si is not None:
            t_acc[si]["tc"] += 1
            if status == "error":
                t_acc[si]["te"] += 1
    return {"tool_acc": tool_acc, "total": total, "t_acc": t_acc}


# ---------- 步骤 4：自检（聚合 SQL 路径 B，同一 WAL 快照内与路径 A 核对） ----------
def selfcheck(conn, scan_mu, tools_total, n_sess, n_sessions_sql):
    """行扫描（路径 A）与聚合 SQL（路径 B）逐项核对。

    核对项：请求数/三类 token 总和/status 三态/tool_usage 总数/session 总数
    （n_sess=排除合成行后的行数、n_sessions_sql=SQL COUNT，两条独立采集路径交叉）；
    另核对请求明细条数与总计同源（req_raw 数 == agg.req）。
    任一不一致 die（数据库可能被并发修改，放弃写入）；孤儿行归入「未知会话」合成行。
    """
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    b_mu = q("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
             "COALESCE(SUM(cache_read_input_tokens),0) FROM model_usage")[0]
    b_st = [0, 0, 0]
    for (status, cbu, c) in q("SELECT status, cancelled_by_user, COUNT(*) FROM model_usage "
                              "GROUP BY status, cancelled_by_user"):
        b_st[st_of(status, cbu)] += c
    b_tools = q("SELECT COUNT(*) FROM tool_usage")[0][0]
    agg = scan_mu["agg"]
    checks = [
        ("请求数", agg["req"], b_mu[0]),
        ("输入token总和", agg["i"], b_mu[1]),
        ("输出token总和", agg["o"], b_mu[2]),
        ("缓存读取token总和", agg["cr"], b_mu[3]),
        ("status=completed", agg["st"][0], b_st[0]),
        ("status=error", agg["st"][1], b_st[1]),
        ("status=cancelled", agg["st"][2], b_st[2]),
        ("tool_usage总数", tools_total, b_tools),
        ("session总数", n_sess, n_sessions_sql),
    ]
    print("--- 自检（行扫描 vs 聚合 SQL，同一 WAL 快照）---")
    ok = True
    for name, x, y in checks:
        flag = "OK " if x == y else "FAIL"
        print("  [%s] %-16s 行扫描=%s  聚合SQL=%s" % (flag, name, x, y))
        if x != y:
            ok = False
    if not ok:
        die("自检未通过：两条 SQL 路径统计不一致（数据库可能在读取中被并发修改），已放弃写入，旧文件保留。")
    # 明细对账：请求明细必须与总计同源，否则页面"总计含孤儿、明细不含"的口径分裂会复发
    if len(scan_mu["req_raw"]) != scan_mu["agg"]["req"]:
        die("自检未通过：请求明细与总计口径不一致（明细 %d 条 ≠ 总计 %d 条），已放弃写入，旧文件保留。"
            % (len(scan_mu["req_raw"]), scan_mu["agg"]["req"]))
    if scan_mu["orphans"]:
        print("  [警告] %d 条 model_usage 记录的 session_id 在 session 表中不存在（孤儿行），已归入「未知会话」合成行。"
              % scan_mu["orphans"])


# ---------- 步骤 5：组装 DATA ----------
def build_payload(scan_mu, scan_tu, sess_rows):
    """把扫描结果组装为 template.html 消费端约定的 DATA 字典。

    前置条件：sess_rows 已由 main 按 (time_created, id) 排序（排序在扫描前完成，
    保证 s_acc 累加器下标与本函数 enumerate 下标一致）；「未知会话」合成行（若有）
    固定追加在末尾、不参与排序，不破坏该不变量。requests 按开始时间升序。
    st/fin 映射与错误类型仅随 status='error' 行携带。
    """
    sess_idx = {r[0]: i for i, r in enumerate(sess_rows)}
    # 「未知会话」合成行下标（id=""，仅存在孤儿请求时由 main 追加；空 id 会话已被
    # main 防御 die 排除，故该键存在当且仅当追加了合成行）。孤儿请求的 session_id
    # 指向不存在的会话，映射时统一落到合成行。
    synthetic_idx = sess_idx.get("")

    agent_order = sorted(scan_mu["agent_cnt"].keys(), key=lambda k: (-scan_mu["agent_cnt"][k], k))
    agent_idx = {k: i for i, k in enumerate(agent_order)}
    prov_order = sorted(scan_mu["prov_cnt"].keys(), key=lambda k: (-scan_mu["prov_cnt"][k], k))
    prov_idx = {k: i for i, k in enumerate(prov_order)}
    model_order = sorted(scan_mu["model_cnt"].keys(), key=lambda k: (-scan_mu["model_cnt"][k], k))
    model_idx = {k: i for i, k in enumerate(model_order)}

    requests_out = _build_requests(scan_mu["req_raw"], sess_idx, agent_idx, prov_idx, model_idx)
    sessions_out = _build_sessions(sess_rows, scan_mu, scan_tu, agent_idx, model_idx)
    tools_out = _build_tools(scan_tu["tool_acc"])

    agg = scan_mu["agg"]
    payload = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "db_path": DB_DISPLAY,
            "range": [scan_mu["min_t"], scan_mu["max_t"]],
            "orphans": scan_mu["orphans"],
            "counts": {
                "sessions": len(sess_rows),
                "requests": agg["req"],
                "tools": scan_tu["total"],
                "selfcheck_pass": True,
            },
        },
        "agents": [{"id": k, "label": AGENT_LABELS.get(k, k)} for k in agent_order],
        "providers": [{"id": k, "label": PROVIDER_LABELS.get(k, k)} for k in prov_order],
        "models": model_order,
        "pricing": PRICING_CNY,
        "pricing_usd": PRICING_USD,
        "sessions": sessions_out,
        "requests": requests_out,
        "tools": tools_out,
        "agg": {"err_types": scan_mu["err_types"]},
    }
    return payload


def _build_requests(req_raw, sess_idx, agent_idx, prov_idx, model_idx):
    """生成 DATA.requests：每行一条请求记录，按开始时间升序（t 为 None 的排尾部）。

    孤儿请求的 session_id 不在 sess_idx 中，统一映射到「未知会话」合成行下标
    （synthetic_idx）；无合成行时不存在此类行（selfcheck 明细对账兜底拦截）。
    """
    synthetic_idx = sess_idx.get("")
    requests_out = []
    for (sid, pk, mk, ak, status, t, d, tt, fin, cbu, et, i, o, cr) in req_raw:
        st = st_of(status, cbu)
        rec = {
            "t": t, "s": sess_idx.get(sid, synthetic_idx),
            "m": model_idx[mk], "p": prov_idx[pk], "a": agent_idx[ak],
            "i": i or 0, "o": o or 0, "cr": cr or 0,
        }
        if d is not None:
            rec["d"] = d
        if tt is not None:
            rec["tt"] = tt
        rec["st"] = st
        rec["fin"] = fin_of(fin)
        if st == 1 and et is not None and str(et).strip() != "":
            rec["et"] = clean(et)
        requests_out.append(rec)
    requests_out.sort(key=lambda r: (r["t"] is not None, r["t"] if r["t"] is not None else 0))
    return requests_out


def _build_sessions(sess_rows, scan_mu, scan_tu, agent_idx, model_idx):
    """生成 DATA.sessions：按创建时间升序（requests[].s 引用其下标）。"""
    sessions_out = []
    s_acc, s_ag, s_md = scan_mu["s_acc"], scan_mu["s_ag"], scan_mu["s_md"]
    t_acc = scan_tu["t_acc"]
    for i, r in enumerate(sess_rows):
        sid, title, directory, t0, t1 = r
        acc, tac = s_acc[i], t_acc[i]
        sessions_out.append({
            "id": clean(sid),
            "title": clean(title),
            "dir": clean(directory),
            "t0": t0, "t1": t1,
            "r": acc["r"], "i": acc["i"], "o": acc["o"], "cr": acc["cr"],
            "ok": acc["ok"], "err": acc["err"], "canc": acc["canc"],
            "retry": acc["retry"],
            "tc": tac["tc"], "te": tac["te"],
            "ag": sorted(agent_idx[x] for x in s_ag[i]),
            "md": sorted(model_idx[x] for x in s_md[i]),
        })
    return sessions_out


def _build_tools(tool_acc):
    """生成 DATA.tools：按调用次数降序。"""
    return [
        {"name": k, "calls": v["calls"], "errs": v["errs"], "dsum": v["dsum"],
         "ro": v["ro"], "destr": v["destr"], "canc": v["canc"]}
        for k, v in sorted(tool_acc.items(), key=lambda kv: (-kv[1]["calls"], kv[0]))
    ]


# ---------- 步骤 6：JSON 回读验证 + 转义注入 + 外链检查 ----------

def serialize_and_inject(tpl, payload, n_sess):
    """payload → JSON → `</` 转义与 U+2028/9 处理 → 注入模板 → 多重校验。

    校验链：JSON 回读可解析、sessions 长度与会话数一致、占位符恰好消耗一次、
    成品不含外部 URL 的 src/href。任一失败 die（旧成品保留）。
    """
    legacy = {key: value for key, value in payload.items() if key != "codex"}
    json_str = json.dumps(legacy, ensure_ascii=False, separators=(",", ":"))
    try:
        back = json.loads(json_str)
    except ValueError as e:
        die("序列化后的 JSON 无法回读解析：%s" % e)
    if len(back["sessions"]) != n_sess:
        die("回读验证失败：sessions 长度 %d ≠ SQL 会话数 %d" % (len(back["sessions"]), n_sess))
    if len(back["requests"]) != len(payload["requests"]):
        die("回读验证失败：requests 长度不一致")
    print("JSON 回读验证：OK（sessions=%d, requests=%d, tools=%d）"
          % (len(back["sessions"]), len(back["requests"]), len(back["tools"])))

    # 转义所有 "<"（覆盖 </、<script、<!-- 等 HTML5 script 解析陷阱），
    # \u003c 在 JSON/JS 字符串中均合法，解析回 `<`
    js = json_str.replace("<", "\\u003c") \
                 .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    out = tpl.replace(PLACEHOLDER, js)
    # Codex 展示脚本与旧页面共享同一个导出批次，注入前沿用相同转义规则。
    extra = json.dumps(payload.get("codex"), ensure_ascii=False, separators=(",", ":"))
    extra = extra.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    meta = json.dumps(payload.get("meta", {}), ensure_ascii=False).replace("<", "\\u003c") \
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    out = out.replace("/*__PLATFORM_DATA__*/", "window.GAUGE_CODEX=" + extra
                      + ";window.GAUGE_ZCODE_META=" + meta + ";")
    if PLACEHOLDER in out:
        die("占位符替换后仍残留占位符，中止写入。")
    if re.search(r'(src|href)\s*=\s*["\']https?://', out, re.I):
        die("成品 HTML 出现外部 URL 的 src/href 引用（违反离线零依赖约束），中止写入。")
    return out


# ---------- 步骤 7：原子写入 ----------
def atomic_write(out, output_path=None):
    """tmp + os.replace 原子写入成品；失败保留旧文件。output_path 供 shell 复用。"""
    target = OUTPUT_PATH if output_path is None else output_path
    tmp_out = str(target) + ".tmp"
    try:
        with open(tmp_out, "w", encoding="utf-8", errors="replace", newline="\n") as f:
            f.write(out)
        os.replace(tmp_out, target)
    except PermissionError:
        die("无法写入成品文件（可能正被浏览器/编辑器锁定）：%s" % target)
    except OSError as e:
        die("写入成品文件失败：%s" % e)


def shell_output_path():
    """shell 形态产物路径：与成品同目录同名主干（从 OUTPUT_PATH 运行时派生）。

    与 sidecar 同一派生口径、不设模块级常量：冻结形态 refreshctl._run_refresh_inline
    在 exec_module 之后才把 OUTPUT_PATH 覆盖为 exe 目录，模块顶层常量会错指向
    _MEIPASS 只读临时区（同 write_sidecar 的既有注释）。
    """
    return OUTPUT_PATH[:-5] + ".shell.html"


def build_shell_html(tpl):
    """生成 shell 形态：与成品同一模板同源注入，仅数据两处占位不同。

    - DATA 占位符置 null：shell 不内嵌数据，页面运行时 fetch sidecar 并入；
    - GAUGE_CODEX 桥置 null：页面取到 sidecar 后再设 window.GAUGE_CODEX
      （白名单消费方式不变，只是来源改为 fetch）。
    样式/脚本/其余注入与成品完全同源（tpl 已含 codex 资产注入），外链检查
    与成品同口径，维持离线零依赖约束。
    声明改写：仅 shell 把 const DATA 改为 var（页面 hydrateData 需要二次赋值）；
    成品保持 const 原文，hydrateData 在成品路径永不调用，行为与字节零变化。
    """
    out = tpl.replace("const DATA =", "var DATA =")
    out = out.replace(PLACEHOLDER, "null")
    out = out.replace("/*__PLATFORM_DATA__*/", "window.GAUGE_CODEX=null;")
    if PLACEHOLDER in out or "/*__PLATFORM_DATA__*/" in out:
        die("shell 模板占位符替换异常，中止写入。")
    if re.search(r'(src|href)\s*=\s*["\']https?://', out, re.I):
        die("shell HTML 出现外部 URL 的 src/href 引用（违反离线零依赖约束），中止写入。")
    return out


# ---------- 步骤 8：桌面 sidecar（单一统计链） ----------
def _sidecar_price_map():
    """PRICING_CNY.rows → {model_id: {"input": f, "cache_read": f, "output": f}}。

    与 template.html 从 DATA.pricing.rows 构建 PRICES 的形状一致（网页默认价目）。
    """
    return {r["model"]: {"input": r["input"], "cache_read": r["cache_read"],
                         "output": r["output"]} for r in PRICING_CNY["rows"]}


def _row_cost(model_id, i, o, cr, prices):
    """单请求成本（元），与 template.html costOf 同公式：
    非缓存输入×输入价 + 缓存读取×缓存读取价 + 输出×输出价；
    缓存读取超出输入按输入钳制（防御脏数据）；未配价模型按 0 计。
    """
    p = prices.get(model_id)
    if not p:
        return 0.0
    cr = min(cr, i)
    return ((i - cr) * p["input"] + cr * p["cache_read"] + o * p["output"]) / 1000000.0


def _peak_factor(ts_ms):
    """峰谷系数：周一至周五本地 14:00–18:00（含 14:00、不含 18:00）×1.0，其余 ×0.5。

    官方高峰定义为 UTC+8；本产品假设本机时区=UTC+8。与 stats.peak_factor 同构。
    """
    local = datetime.fromtimestamp(ts_ms / 1000.0)
    if local.weekday() >= 5:          # weekday(): 周一=0 … 周六=5、周日=6
        return 0.5
    return 1.0 if 14 <= local.hour < 18 else 0.5


def _row_credits(model_id, i, o, cr, ts_ms):
    """单请求积分（官方口径逐行计；与 _row_cost 不同，不做 cache_read 钳制；
    未列出模型按 0 计）。与 stats.credits_of 同构。"""
    ci, cc, co = CREDIT_COEFFS.get(model_id, (0.0, 0.0, 0.0))
    return (i * ci + cr * cc + o * co) / CREDIT_DIVISOR * _peak_factor(ts_ms)


def _sidecar_plan_tier():
    """从贴纸配置 widget.json 读取档位；缺失/损坏/非法一律回退 "lite"。

    候选路径覆盖两种形态：源码（BASE_DIR/desktop/widget.json，与
    monitor.config 的 WIDGET_CFG_PATH 同源）；冻结（exe）形态 refresh.py 与
    打包资源同目录（_MEIPASS/_internal，即 BASE_DIR/widget.json）。

    冻结形态下 BASE_DIR=_MEIPASS（只读临时区，退出即删），前两个候选必然落空；
    故追加「成品 HTML 同目录」候选：refreshctl._run_refresh_inline 会在 exec_module
    之后把 OUTPUT_PATH 覆盖为 APP_DIR/AI-Agent监控台.html，此时 dirname(OUTPUT_PATH)
    即 exe 目录，正好与 monitor.config 冻结形态写出的 widget.json 同处。
    源码形态该候选指向仓库根（widget.json 不存在，无害），原有候选与顺序保持不变。
    """
    for cand in (os.path.join(BASE_DIR, "desktop", "widget.json"),
                 os.path.join(BASE_DIR, "widget.json"),
                 os.path.join(os.path.dirname(OUTPUT_PATH), "widget.json")):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            t = raw.get("plan_tier")
            if isinstance(t, str) and t in PLAN_QUOTAS:
                return t
    return DEFAULT_PLAN_TIER


def build_zcode_widget(scan_mu, meta_generated_at):
    """从同一批已聚合数据计算 ZCode 贴纸内容，由 write_sidecar 统一写出。

    不再查库：全部数据来自 scan_mu["req_raw"] 行扫描结果，与成品 HTML 同一
    WAL 快照、同一口径，保证桌面统计链与网页完全一致。

    路径派生不变量：sidecar 与成品 HTML 永远同目录同名主干——运行时从
    OUTPUT_PATH 派生（OUTPUT_PATH 以 .html 结尾，取 [:-5] 替换扩展名）；
    exe 内联形态 desktop/monitor/refreshctl.py 会把 OUTPUT_PATH 覆盖为 exe 目录，派生自动跟随，
    故不设模块级 sidecar 常量（避免 _MEIPASS 只读临时区错位）。

    内容契约（desktop/app.py load_sidecar / widget.html renderWidget 消费）：
      generated_at  与 DATA.meta.generated_at 同一值（由 main 传入）
      db_display    数据库展示路径
      today         本地今日零点起：requests 计数 + in/out/cr 累计 + 按管线价目成本
      week          近 7 天（含今日）每日 {d,full,req,cost}，恰 7 项、今日为最后一项
      last_error    最近 24h 内最新一条错误行（st==1 且 error_type 非空，
                    与 _build_requests 的 et 携带口径及 stats.py 回退口径一致），
                    {"type", "at"(HH:MM)}；无则 null
      pricing       管线价目快照（同 DATA.pricing.rows 的模型集合）
      plan          GAUGE 档位 {"tier", "window5h_limit", "week_limit"}（widget.json
                    读取，缺失/损坏/非法回退 lite）
      window5h      {"credits", "used_pct", "reset_eta_min", "tokens"}：
                    started_at >= now-5h 的积分和（官方公式逐行计×峰谷系数）、
                    占额度百分比（1 位小数，可 >100）、窗口内最早请求 + 5h 距现在
                    的分钟数（四舍五入取整，窗口为空 null）、窗口内原始 token
                    累计 {"in","cr","out"}（缓存读取=cr；贴纸 Token 模式消费）
      thisweek      {"credits", "used_pct", "tokens"}：本自然周（周一 00:00 本地起）
                    积分和、占周额度百分比与窗口内原始 token 累计（同 window5h 键）

    写失败仅打印警告、不影响退出码：HTML 是主交付物，桌面侧对 sidecar 缺失
    有 stats 直查回退。返回 True=成功。
    """
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_ms = int(today0.timestamp() * 1000)          # 本地今日零点（epoch 毫秒）
    week0_ms = midnight_ms - 6 * 86400 * 1000             # 近 7 天（含今日）起点
    err_since_ms = int((time.time() - 86400) * 1000)      # 最近 24h（与 stats.py 同构）
    prices = _sidecar_price_map()

    # GAUGE 积分窗口（官方口径）：5h 窗口 + 本自然周（周一 00:00 本地起）
    tier = _sidecar_plan_tier()
    w5h_limit, week_limit = PLAN_QUOTAS[tier]
    now_ms = int(time.time() * 1000)
    win5_start_ms = now_ms - 5 * 3600 * 1000              # now-5h
    monday0 = today0 - timedelta(days=today0.weekday())   # 本自然周周一 00:00（本地）
    week_start_ms = int(monday0.timestamp() * 1000)
    win5_credits = 0.0
    week_credits = 0.0
    win5_tin = win5_tcr = win5_tout = 0
    week_tin = week_tcr = week_tout = 0
    earliest5 = None

    # today/week/last_error/积分窗口一次遍历 req_raw 聚合完成（禁止再查库）
    n_req, t_in, t_out, t_cr = 0, 0, 0, 0
    today_cost = 0.0
    week = [{"req": 0, "cost": 0.0} for _ in range(7)]    # 下标 0=最旧 … 6=今日
    last_error = None
    err_best_t = None
    for (_sid, _pk, mk, _ak, status, t, _d, _tt, _fin, cbu, et, i, o, cr) in scan_mu["req_raw"]:
        if t is None:
            continue                                      # 无时间行不计入时间窗（同 SQL NULL 语义）
        i, o, cr = i or 0, o or 0, cr or 0
        cost = _row_cost(mk, i, o, cr, prices)
        credits = _row_credits(mk, i, o, cr, t)
        if t >= week_start_ms:
            week_credits += credits
            week_tin += i
            week_tcr += cr
            week_tout += o
        if t >= win5_start_ms:
            win5_credits += credits
            win5_tin += i
            win5_tcr += cr
            win5_tout += o
            if earliest5 is None or t < earliest5:
                earliest5 = t
        if t >= midnight_ms:
            n_req += 1
            t_in += i
            t_out += o
            t_cr += cr
            today_cost += cost
        if t >= week0_ms:
            # 按本地日历日分桶：与 stats.py 的 date(...,'localtime') 及 "%Y-%m-%d" 键一致；
            # 未来时间戳（异常数据）idx 为负，防御性跳过
            idx = 6 - (today0.date() - datetime.fromtimestamp(t / 1000).date()).days
            if 0 <= idx < 7:
                week[idx]["req"] += 1
                week[idx]["cost"] += cost
        if t >= err_since_ms and st_of(status, cbu) == 1 \
                and et is not None and str(et).strip() != "":
            if err_best_t is None or t > err_best_t:
                err_best_t = t
                last_error = {"type": clean(et),
                              "at": datetime.fromtimestamp(t / 1000).strftime("%H:%M")}

    week_out = []
    for off in range(6, -1, -1):
        d0 = today0 - timedelta(days=off)
        e = week[6 - off]
        week_out.append({
            "d": d0.strftime("%m-%d"),
            "full": d0.strftime("%Y-%m-%d"),
            "req": e["req"],
            "cost": round(e["cost"], 2),
        })

    # 5h 窗口重置倒计时：窗口内最早请求 + 5h 距现在的分钟数；四舍五入取整
    # （半进位，整数毫秒运算避免浮点边界）；窗口为空 → None。窗口成员保证
    # earliest5 >= win5_start_ms，故 diff_ms >= 0。
    reset_eta_min = None
    if earliest5 is not None:
        diff_ms = earliest5 + 5 * 3600 * 1000 - now_ms
        reset_eta_min = (diff_ms + 30_000) // 60_000

    data = {
        "generated_at": meta_generated_at,
        "db_display": DB_DISPLAY,
        "today": {"requests": n_req, "cost": round(today_cost, 2),
                  "in": t_in, "out": t_out, "cr": t_cr},
        "week": week_out,
        "last_error": last_error,
        "pricing": prices,
        "plan": {"tier": tier, "window5h_limit": w5h_limit, "week_limit": week_limit},
        "window5h": {"credits": round(win5_credits, 1),
                     "used_pct": round(win5_credits / w5h_limit * 100, 1),
                     "reset_eta_min": reset_eta_min,
                     "tokens": {"in": win5_tin, "cr": win5_tcr, "out": win5_tout}},
        "thisweek": {"credits": round(week_credits, 1),
                     "used_pct": round(week_credits / week_limit * 100, 1),
                     "tokens": {"in": week_tin, "cr": week_tcr, "out": week_tout}},
    }
    data["platform"] = "zcode"
    data["status"] = "ready"
    return data


def write_sidecar(scan_mu, meta_generated_at, codex=None, zcode_error=None,
                  page_payload=None):
    """写双平台桌面快照；保留旧版顶层字段，平台失败不会伪装成另一平台。

    W8 双形态追加（additive，既有键零删除零改名、schema_version 不动）：
    - codex     顶层键 = GAUGE_CODEX 快照对象（与成品 HTML 注入的
                window.GAUGE_CODEX 同一对象；大载荷单副本，页面 DATA 不携带）；
    - data      顶层键 = 页面 DATA 契约字段（meta/agents/…/agg，即成品内嵌
                legacy 载荷同一对象），shell 形态页面 fetch 后把字段并入页面
                DATA 状态；单独成键避免与既有顶层 pricing（价目 map 形状）冲突。
    generated_at 既有顶层键兼作热替换 revision 口径（与 data.meta.generated_at
    同值，由 main 传同一来源）。"""
    from gauge_data import codex_widget
    data = build_zcode_widget(scan_mu, meta_generated_at)
    if zcode_error:
        data.update(status="unavailable", error=zcode_error)
    platforms = {"zcode": dict(data)}
    if codex is not None:
        platforms["codex"] = codex_widget(codex)
    data.update(schema_version=2, platforms=platforms)
    if page_payload is not None:
        data["data"] = {key: value for key, value in page_payload.items()
                        if key != "codex"}
    data["codex"] = codex
    sidecar_path = str(Path(OUTPUT_PATH).with_suffix(".data.json"))
    tmp = sidecar_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, sidecar_path)                     # tmp + os.replace，与 atomic_write 同语义
    except Exception as e:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print("[警告] sidecar 写入失败（桌面侧将回退 stats 直查，不影响成品 HTML）：%s: %s"
              % (type(e).__name__, e), file=sys.stderr)
        return False
    print("桌面 sidecar：%s" % sidecar_path)
    return True


def collect_zcode():
    """在一个只读事务中采集并校验 ZCode，返回页面数据和贴纸聚合结果。"""
    print("=== AI Agent 监控台 数据刷新 ===")
    print("数据库（只读）：" + DB_PATH)

    # ---- 2. 只读打开数据库（单快照）----
    conn = open_db()
    try:
        q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731

        # 会话：取出行后立即按 (time_created 升序, id) 排序——排序必须先于
        # sess_idx 构建与行扫描聚合，保证扫描累加器下标与最终 DATA.sessions 下标一致
        # （不变量：若在 build_payload 内才排序，DB 返回序≠排序序时统计会错位）
        sess_rows = q("SELECT id, title, directory, time_created, time_updated FROM session")
        sess_rows.sort(key=lambda r: ((r[3] if r[3] is not None else 0), r[0] or ""))
        # 孤儿请求（session_id 不在 session 表的 model_usage 行）：旧实现只计入全局
        # 总计、不进请求明细，页面"总计含孤儿、明细不含"。统一口径：存在孤儿时在
        # 排序结果末尾追加「未知会话」合成行——合成行固定追加在末尾、不参与排序，
        # 维持「排序先于 sess_idx/行扫描」不变量。
        has_orphan = q("SELECT 1 FROM model_usage WHERE session_id NOT IN "
                       "(SELECT id FROM session) LIMIT 1")
        if has_orphan:
            # 防御：正常库不可能出现空 id 会话；若存在，合成行 id="" 会与其在
            # sess_idx 中冲突，无法安全统一口径，放弃写入
            if q("SELECT 1 FROM session WHERE id='' LIMIT 1"):
                die("session 表存在 id 为空字符串的记录，与「未知会话」合成行 id 冲突，"
                    "无法统一孤儿请求口径。")
            sess_rows.append(("", "（未知会话）", "", None, None))
            orphan_idx = len(sess_rows) - 1
        else:
            orphan_idx = None
        n_sess = len(sess_rows)
        n_sessions_sql = q("SELECT COUNT(*) FROM session")[0][0]

        # ---- 3a/3b. 行扫描（路径 A；此时 sess_rows 已排序，下标与最终输出一致）----
        sess_idx = {r[0]: i for i, r in enumerate(sess_rows)}
        scan_mu = scan_model_usage(conn, sess_idx, n_sess, orphan_idx)
        scan_tu = scan_tool_usage(conn, sess_idx, n_sess)

        # ---- 4. 自检（路径 B 核对）----
        # 会话行数对账用「排除合成行后的行数」与 SQL COUNT 比较（合成行非库内记录）
        n_sess_real = len(sess_rows) - (1 if orphan_idx is not None else 0)
        selfcheck(conn, scan_mu, scan_tu["total"], n_sess_real, n_sessions_sql)

        # ---- 5. 组装 DATA ----
        payload = build_payload(scan_mu, scan_tu, sess_rows)

        return payload, scan_mu
    finally:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:
            pass
        conn.close()


def main():
    """生成双平台快照；关闭 ZCode 读事务后再增量读取 Codex，避免长事务。"""
    from gauge_data import collect_codex
    tpl = load_template()
    zcode_error = None
    if os.path.isfile(DB_PATH):
        payload, scan_mu = collect_zcode()
        payload["meta"].update(available=True, status="ready", platform="zcode")
    else:
        zcode_error = "未找到 ZCode 数据库，请检查安装位置或选择 Codex。"
        scan_mu = {"req_raw": []}
        payload = {
            "meta": {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "platform": "zcode", "available": False, "status": "unavailable",
                     "error": zcode_error, "db_path": DB_DISPLAY, "range": [None, None],
                     "orphans": 0, "counts": {"sessions": 0, "requests": 0, "tools": 0,
                                              "selfcheck_pass": False}},
            "agents": [], "providers": [], "models": [], "sessions": [], "requests": [],
            "tools": [], "pricing": PRICING_CNY, "pricing_usd": PRICING_USD,
            "agg": {"err_types": {}},
        }
    # 单次读取预算可供候选构建验证调整；正常后台刷新自动续读缓存。
    codex = collect_codex()
    # 避免大型 Codex 载荷同时复制到旧 DATA 和独立全局变量。
    payload["codex"] = codex
    out = serialize_and_inject(tpl, payload, len(payload["sessions"]))
    node_syntax_check(out)
    atomic_write(out)
    # W8 双形态：shell 与成品同一模板同源注入，仅数据占位置 null（页面运行时
    # fetch sidecar）。独立 nodecheck 缓存文件，避免与成品的哈希缓存互相覆盖。
    shell = build_shell_html(tpl)
    node_syntax_check(shell, OUTPUT_PATH[:-5] + ".shell.nodecheck")
    shell_path = shell_output_path()
    atomic_write(shell, shell_path)
    write_sidecar(scan_mu, payload["meta"]["generated_at"], codex, zcode_error,
                  page_payload=payload)
    print("双平台快照：ZCode %s；Codex %s" % (payload["meta"]["status"], codex.get("status")))
    print("成品：%s（%.2f MB）" % (OUTPUT_PATH, os.path.getsize(OUTPUT_PATH) / 1048576.0))
    print("shell：%s（%.2f MB）；sidecar 携带页面数据与 codex 快照" % (
        shell_path, os.path.getsize(shell_path) / 1048576.0))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        die("未预期的错误：%r" % (e,))
