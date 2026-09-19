#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh.py — 从本机 ZCode 会话库（只读）抽取统计数据，注入 AI-Agent监控台.html

- 仅使用 Python 标准库；绝不写数据库（mode=ro + query_only 双重保证）。
- 内置自检：行扫描与聚合 SQL 两条独立路径核对通过才写成品文件。
- 失败时不产出损坏 HTML：全部校验通过后才原子替换（保留旧文件）。

用法：双击 刷新数据.bat，或在本目录运行 python refresh.py
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "template.html")
OUTPUT_PATH = os.path.join(BASE_DIR, "AI-Agent监控台.html")
DB_PATH = os.path.expanduser(r"~/.zcode/cli/db/db.sqlite")
DB_DISPLAY = "~/.zcode/cli/db/db.sqlite（只读导出）"
PLACEHOLDER = "/*__DATA_PLACEHOLDER__*/null"

# ---- 显示名映射（与 template.html 内回退映射保持一致；原始 id 始终保留在 DATA 中）----
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
        {"model": "GLM-5.3-Flash", "input": 0.8, "output": 2.8, "cache_read": 0.23, "cache_write": 0},
    ],
}
PRICING_USD = {
    "as_of": PRICE_AS_OF,
    "sources": [SRC_ZAI],
    "note": "两平台价格互不换算，仅作参考；默认估算使用 CNY 目录价。",
    "rows": [
        {"model": "GLM-5.3", "input": 1.40, "output": 4.40, "cache_read": 0.26, "cache_write": 0},
        {"model": "GLM-5.3-Flash", "input": 0.15, "output": 0.50, "cache_read": 0.03, "cache_write": 0},
    ],
}


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
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA query_only=1")   # 运行时禁止任何写操作
        conn.execute("BEGIN DEFERRED")        # WAL 快照：两条自检路径看到同一份数据
    except sqlite3.Error as e:
        die("无法以只读方式打开数据库（可能正被 ZCode 占用，请稍后重试）。\n详细：%s" % e)
    return conn


def node_syntax_check(html_text):
    """抽取成品 HTML 中内联 <script>，用 node --check 做语法检查。无 node 时跳过。"""
    node = shutil.which("node")
    if not node:
        print("[警告] 未找到 node，跳过 JS 语法检查（不影响数据正确性）。")
        return None
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html_text, re.S | re.I)
    if not scripts:
        die("成品 HTML 中未找到内联 <script>，模板可能已损坏。")
    for i, code in enumerate(scripts):
        fd, tmp = tempfile.mkstemp(suffix=".js", dir=BASE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code)
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
            if r.returncode != 0:
                die("JS 语法检查失败（script #%d，node --check）：\n%s" % (i, (r.stderr or r.stdout)[:2000]))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return True


def main():
    """数据抽取主流程：模板校验 → 只读建快照 → 行扫描 → 双路径自检 →
    组装 DATA → JSON 回读验证 → 注入模板 → JS 语法/外链检查 → 原子写入成品。

    任何一步失败都会 die（中文报错、退出码非 0），旧成品文件保持不变。
    """
    print("=== AI Agent 监控台 数据刷新 ===")
    print("数据库（只读）：" + DB_PATH)

    # ---- 1. 模板 ----
    if not os.path.exists(TEMPLATE_PATH):
        die("未找到模板文件：%s" % TEMPLATE_PATH)
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        tpl = f.read()
    n_ph = tpl.count(PLACEHOLDER)
    if n_ph != 1:
        die("模板中数据占位符出现 %d 次（应恰好为 1 次），模板可能已损坏。" % n_ph)

    # ---- 2. 只读打开数据库（单快照） ----
    conn = open_db()
    try:
        q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731

        # 会话（按 time_created 升序 → 稳定下标）
        sess_rows = q("SELECT id, title, directory, time_created, time_updated FROM session")
        sess_rows.sort(key=lambda r: ((r[3] if r[3] is not None else 0), r[0] or ""))
        n_sessions_sql = q("SELECT COUNT(*) FROM session")[0][0]
        sess_idx = {r[0]: i for i, r in enumerate(sess_rows)}
        n_sess = len(sess_rows)
        s_acc = [{"r": 0, "i": 0, "o": 0, "cr": 0, "ok": 0, "err": 0, "canc": 0, "retry": 0} for _ in range(n_sess)]
        s_ag = [set() for _ in range(n_sess)]
        s_md = [set() for _ in range(n_sess)]
        t_acc = [{"tc": 0, "te": 0} for _ in range(n_sess)]

        # model_usage 全量行扫描（路径 A）
        mu_rows = q(
            "SELECT session_id, provider_id, model_id, agent, status, started_at, duration_ms, "
            "time_to_first_token_ms, finish_reason, retry_count, cancelled_by_user, error_type, "
            "input_tokens, output_tokens, cache_read_input_tokens FROM model_usage"
        )
        agent_cnt, prov_cnt, model_cnt, err_types = {}, {}, {}, {}
        req_raw = []
        orphans = 0
        a = {"req": 0, "i": 0, "o": 0, "cr": 0, "st": [0, 0, 0]}
        min_t = max_t = None
        for (sid, prov, model, agent, status, t, d, tt, fin, retry, cbu, et, i, o, cr) in mu_rows:
            a["req"] += 1
            a["i"] += i or 0
            a["o"] += o or 0
            a["cr"] += cr or 0
            st = st_of(status, cbu)
            a["st"][st] += 1
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

        # tool_usage 全量行扫描（路径 A）
        tu_rows = q(
            "SELECT session_id, tool_name, status, duration_ms, read_only, destructive, cancelled_by_user FROM tool_usage"
        )
        tool_acc = {}
        a_tools = 0
        for (sid, name, status, d, ro, de, ca) in tu_rows:
            a_tools += 1
            nm = clean(name) or "(未知)"
            e = tool_acc.setdefault(nm, {"calls": 0, "errs": 0, "dsum": 0, "ro": 0, "destr": 0, "canc": 0})
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

        # ---- 3. 自检：聚合 SQL（路径 B），同一快照内与路径 A 逐项核对 ----
        b_mu = q("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                 "COALESCE(SUM(cache_read_input_tokens),0) FROM model_usage")[0]
        b_st = [0, 0, 0]
        for (status, cbu, c) in q("SELECT status, cancelled_by_user, COUNT(*) FROM model_usage GROUP BY status, cancelled_by_user"):
            b_st[st_of(status, cbu)] += c
        b_tools = q("SELECT COUNT(*) FROM tool_usage")[0][0]
        b_sess = n_sessions_sql

        checks = [
            ("请求数", a["req"], b_mu[0]),
            ("输入token总和", a["i"], b_mu[1]),
            ("输出token总和", a["o"], b_mu[2]),
            ("缓存读取token总和", a["cr"], b_mu[3]),
            ("status=completed", a["st"][0], b_st[0]),
            ("status=error", a["st"][1], b_st[1]),
            ("status=cancelled", a["st"][2], b_st[2]),
            ("tool_usage总数", a_tools, b_tools),
            ("session总数", n_sess, b_sess),
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
        if orphans:
            print("  [警告] %d 条 model_usage 记录的 session_id 在 session 表中不存在（孤儿行），已从请求明细中剔除。" % orphans)

        # ---- 4. 组装 DATA（契约 §6） ----
        agents_order = sorted(agent_cnt.keys(), key=lambda k: (-agent_cnt[k], k))
        agents_idx = {k: i for i, k in enumerate(agents_order)}
        provs_order = sorted(prov_cnt.keys(), key=lambda k: (-prov_cnt[k], k))
        provs_idx = {k: i for i, k in enumerate(provs_order)}
        models_order = sorted(model_cnt.keys(), key=lambda k: (-model_cnt[k], k))
        models_idx = {k: i for i, k in enumerate(models_order)}

        requests_out = []
        for (sid, pk, mk, ak, status, t, d, tt, fin, cbu, et, i, o, cr) in req_raw:
            st = st_of(status, cbu)
            rec = {
                "t": t, "s": sess_idx[sid], "m": models_idx[mk], "p": provs_idx[pk], "a": agents_idx[ak],
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

        sessions_out = []
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
                "ag": sorted(agents_idx[x] for x in s_ag[i]),
                "md": sorted(models_idx[x] for x in s_md[i]),
            })

        tools_out = [
            {"name": k, "calls": v["calls"], "errs": v["errs"], "dsum": v["dsum"],
             "ro": v["ro"], "destr": v["destr"], "canc": v["canc"]}
            for k, v in sorted(tool_acc.items(), key=lambda kv: (-kv[1]["calls"], kv[0]))
        ]

        payload = {
            "meta": {
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "db_path": DB_DISPLAY,
                "range": [min_t, max_t],
                "counts": {
                    "sessions": n_sess,
                    "requests": a["req"],
                    "tools": a_tools,
                    "selfcheck_pass": True,
                },
            },
            "agents": [{"id": k, "label": AGENT_LABELS.get(k, k)} for k in agents_order],
            "providers": [{"id": k, "label": PROVIDER_LABELS.get(k, k)} for k in provs_order],
            "models": models_order,
            "pricing": PRICING_CNY,
            "pricing_usd": PRICING_USD,
            "sessions": sessions_out,
            "requests": requests_out,
            "tools": tools_out,
            "agg": {"err_types": err_types},
        }

        # ---- 5. JSON 序列化 + 回读验证 + 注入 ----
        json_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            back = json.loads(json_str)
        except ValueError as e:
            die("序列化后的 JSON 无法回读解析：%s" % e)
        if len(back["sessions"]) != b_sess:
            die("回读验证失败：sessions 长度 %d ≠ SQL 会话数 %d" % (len(back["sessions"]), b_sess))
        if len(back["requests"]) != len(requests_out):
            die("回读验证失败：requests 长度不一致")
        print("JSON 回读验证：OK（sessions=%d, requests=%d, tools=%d）"
              % (len(back["sessions"]), len(back["requests"]), len(back["tools"])))

        # 转义所有 "<"（覆盖 </、<script、<!-- 等 HTML5 script 解析陷阱），
        # \u003c 在 JSON/JS 字符串中均合法，解析回 `<`
        js = json_str.replace("<", "\\u003c") \
                     .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        out = tpl.replace(PLACEHOLDER, js)
        if PLACEHOLDER in out:
            die("占位符替换后仍残留占位符，中止写入。")
        if re.search(r'(src|href)\s*=\s*["\']https?://', out, re.I):
            die("成品 HTML 出现外部 URL 的 src/href 引用（违反离线零依赖约束），中止写入。")

        # ---- 6. JS 语法检查（node --check） ----
        node_ok = node_syntax_check(out)
        if node_ok:
            print("JS 语法检查（node --check）：OK（%d 个内联脚本）" % len(re.findall(r"<script\b", out)))

        # ---- 7. 原子写入（失败保留旧文件） ----
        tmp_out = OUTPUT_PATH + ".tmp"
        try:
            with open(tmp_out, "w", encoding="utf-8", errors="replace", newline="\n") as f:
                f.write(out)
            os.replace(tmp_out, OUTPUT_PATH)
        except PermissionError:
            die("无法写入成品文件（可能正被浏览器/编辑器锁定）：%s" % OUTPUT_PATH)
        except OSError as e:
            die("写入成品文件失败：%s" % e)

        size = os.path.getsize(OUTPUT_PATH)
        print("--- 完成 ---")
        print("会话 %d · 请求 %d · 工具记录 %d · 错误类型 %d 种"
              % (n_sess, a["req"], a_tools, len(err_types)))
        print("时间范围：%s ~ %s"
              % (datetime.fromtimestamp(min_t / 1000).strftime("%Y-%m-%d %H:%M") if min_t else "—",
                 datetime.fromtimestamp(max_t / 1000).strftime("%Y-%m-%d %H:%M") if max_t else "—"))
        print("成品：%s（%.2f MB）" % (OUTPUT_PATH, size / 1048576.0))
        print("双击 AI-Agent监控台.html 即可打开。")
    finally:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:
            pass
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        die("未预期的错误：%r" % (e,))
