// W8 双形态 UI 回归：shell+sidecar 渲染与热替换（质检批次四）。
// 手法：先用 refresh 管线在临时目录产出三件（假 DB、假 codex，隔离真实数据），
// node 起本地静态服务模拟 pywebview 内置服务托管 shell+sidecar，Playwright
// 驱动真实 Chromium 分别打开 shell（http://）与成品（file://）对照关键节点，
// 再改写 sidecar 验证 __gaugeApplyLatest 热替换（DOM 更新、无导航、判重 no-op）。
// 退出码约定：0=通过；75=环境不可用（无浏览器/无 Python），runner 转 skip；1=断言失败。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const REPO = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/(\w:)/, '$1')), '../..');
const ENV_UNAVAILABLE = 75;

// ---- Playwright 解析：裸导入优先，其次全局 node_modules 里的嵌套依赖（本机
// 没有顶层 playwright 包；n8n/rednote-mcp 自带 1.55，配系统 Edge 无需下载浏览器）。
// Windows 下 npm 是 .cmd，spawnSync 必须带 shell 才能拉起；APPDATA 标准路径优先。
async function loadPlaywright() {
  try { return await import('playwright'); } catch { /* 继续找全局嵌套依赖 */ }
  const roots = [];
  if (process.env.APPDATA) roots.push(path.join(process.env.APPDATA, 'npm', 'node_modules'));
  const probe = spawnSync('npm root -g', { encoding: 'utf8', shell: process.platform === 'win32' });
  if (probe.status === 0 && probe.stdout) roots.push(probe.stdout.trim());
  for (const root of roots) {
    for (const sub of ['n8n/node_modules/playwright', 'rednote-mcp/node_modules/playwright']) {
      // ESM 目录导入不被支持，必须显式指到包内入口文件
      try { return await import(pathToFileURL(path.join(root, sub, 'index.mjs'))); }
      catch { /* 试下一个 */ }
    }
  }
  return null;
}

async function launchBrowser(pw) {
  // 优先系统 Edge（免浏览器下载）；失败再退默认 chromium（ms-playwright 缓存）。
  try { return await pw.chromium.launch({ channel: 'msedge' }); }
  catch { return await pw.chromium.launch(); }
}

// ---- 隔离管线：临时目录里产三件（与 test_shell_form.py 同一套假数据口径）。
function runPipeline(tmp) {
  const exe = process.env.GAUGE_PYTHON || 'python';
  let r = spawnSync(exe, ['-c', PY_SNIPPET, REPO, tmp], { encoding: 'utf8' });
  if (r.error && r.error.code === 'ENOENT') {
    r = spawnSync('py', ['-3.13', '-c', PY_SNIPPET, REPO, tmp], { encoding: 'utf8' });
  }
  return r;
}

const PY_SNIPPET = `
import os, sqlite3, sys, time
repo, tmp = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
import refresh, gauge_data
now_ms = int(time.time() * 1000)
db = os.path.join(tmp, 'fixture.sqlite')
conn = sqlite3.connect(db)
conn.executescript('''
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
''')
conn.execute("INSERT INTO session VALUES ('s1','会话一','J:/proj/a',?,?)", (now_ms - 3600000, now_ms - 60000))
conn.execute("INSERT INTO session VALUES ('s2','会话二','J:/proj/b',?,?)", (now_ms - 7200000, now_ms - 120000))
conn.executemany('INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [
    ('s1', 'builtin:bigmodel-coding-plan', 'GLM-5.3', 'zcode-agent', 'completed', now_ms - 3000000, 1200, 300, 'stop', 0, 0, None, 1000, 200, 100),
    ('s1', 'builtin:bigmodel-coding-plan', 'GLM-5.3-Flash', 'zcode-agent', 'error', now_ms - 2000000, 900, 250, 'stop', 1, 0, 'rate_limit', 500, 50, 0),
    ('s2', 'account:bigmodel-individual-coding-plan', 'GLM-5.3', 'zcode-agent', 'cancelled', now_ms - 1000000, 800, 200, 'tool-calls', 0, 1, None, 300, 30, 30),
])
conn.execute("INSERT INTO tool_usage VALUES ('s1','Bash','success',120,0,0,0)")
conn.execute("INSERT INTO tool_usage VALUES ('s2','Read','success',40,1,0,0)")
conn.commit()
conn.close()
gauge_data.collect_codex = lambda: {
    'platform': 'codex', 'available': True, 'status': 'ready',
    'generated_at': '2026-09-22T00:00:00+08:00', 'generated_at_ms': 1789999200000,
    'threads': [{'id': 't1', 'title': '任务一', 'updated_at': now_ms}, {'id': 't2', 'title': '任务二', 'updated_at': now_ms}],
    'turns': [], 'usage': [], 'tools': [], 'edges': [], 'projects': [], 'goals': [],
    'quota': None, 'coverage': {'complete': True}, 'warnings': [], 'environment': {},
    'diagnostics': {}, 'sources': {}}
refresh.DB_PATH = db
refresh.OUTPUT_PATH = os.path.join(tmp, 'AI-Agent监控台.html')
sys.exit(refresh.main())
`;

// ---- 本地静态服务：模拟 pywebview 内置服务的两条关键行为——按 root 托管文件、
// no-cache 响应头（缓存击穿交给页面侧 ?ts= cache-bust）。
function serve(root) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
      fs.readFile(path.join(root, pathname), (err, buf) => {
        if (err) {
          res.writeHead(404, { 'Cache-Control': 'no-store' });
          res.end('not found');
          return;
        }
        const type = pathname.endsWith('.html') ? 'text/html; charset=utf-8' : 'application/json';
        res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
        res.end(buf);
      });
    });
    srv.listen(0, '127.0.0.1', () => resolve(srv));
  });
}

async function collectLegacy(page) {
  // 关键节点采集：页签数、总览卡、codex 注入区标志、双形态装载分派的痕迹。
  return page.evaluate(() => {
    const kpis = [...document.querySelectorAll('#ov-kpis .card')].map((c) => ({
      k: c.querySelector('.k').textContent,
      v: c.querySelector('.v').textContent,
    }));
    const codex = window.GAUGE_CODEX;
    return {
      tabs: document.querySelectorAll('nav#tabs button').length,
      kpis,
      agents: document.querySelectorAll('#ov-agents .agent-card').length,
      genInfo: document.getElementById('gen-info').textContent,
      codexThreads: codex && Array.isArray(codex.threads) ? codex.threads.length : null,
      zmeta: window.GAUGE_ZCODE_META ? window.GAUGE_ZCODE_META.generated_at : null,
      statusBar: (document.getElementById('gauge-platform-status') || {}).textContent || null,
      fatalShown: document.getElementById('fatal').style.display === 'block',
      applyLatest: typeof window.__gaugeApplyLatest,
    };
  });
}

function kpiValue(snapshot, label) {
  const hit = snapshot.kpis.find((c) => c.k === label);
  assert.ok(hit, '缺少 KPI 卡：' + label);
  return hit.v;
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gauge-shell-'));
const tmpBare = fs.mkdtempSync(path.join(os.tmpdir(), 'gauge-shell-bare-'));
let server = null;
let serverBare = null;
let browser = null;
try {
  const piped = runPipeline(tmp);
  if (piped.status !== 0) {
    if (piped.error && piped.error.code === 'ENOENT') {
      console.error('python 不可用，跳过 shell UI 套件');
      process.exit(ENV_UNAVAILABLE);
    }
    console.error('隔离管线失败：\n' + (piped.stdout || '') + (piped.stderr || ''));
    process.exit(1);
  }
  // 产物名必须用规范名：页面 fetch 的 sidecar 文件名是契约冻结的
  // ./AI-Agent监控台.data.json，改名会让相对 fetch 404。
  const shellPath = path.join(tmp, 'AI-Agent监控台.shell.html');
  const sidecarPath = path.join(tmp, 'AI-Agent监控台.data.json');
  const outPath = path.join(tmp, 'AI-Agent监控台.html');
  if (!fs.existsSync(shellPath) || !fs.existsSync(sidecarPath)) {
    console.error('管线未产出 shell/sidecar（W8 双形态未实现或损坏）');
    process.exit(1);
  }

  const pw = await loadPlaywright();
  if (!pw) {
    console.error('Playwright 不可用（无顶层包且全局嵌套依赖缺失），跳过 shell UI 套件');
    process.exit(ENV_UNAVAILABLE);
  }
  browser = await launchBrowser(pw);

  // 无 sidecar 的裸目录：fetch 失败必须复用 #fatal 错误呈现（不新造 UI）。
  fs.copyFileSync(shellPath, path.join(tmpBare, 'AI-Agent监控台.shell.html'));
  serverBare = await serve(tmpBare);
  const barePort = serverBare.address().port;
  const barePage = await (await browser.newContext()).newPage();
  await barePage.goto('http://127.0.0.1:' + barePort + '/' +
    encodeURIComponent('AI-Agent监控台.shell.html'));
  await barePage.waitForFunction(() => document.getElementById('fatal').style.display === 'block',
    null, { timeout: 15000 });
  const bareFatal = await barePage.evaluate(() => document.getElementById('fatal').textContent);
  assert.ok(bareFatal.includes('shell 形态加载数据失败'), 'fetch 失败必须落到既有 #fatal 呈现');
  await barePage.context().close();

  // 正常路径：shell（http 模拟 pywebview 静态服务）+ sidecar。
  server = await serve(tmp);
  const port = server.address().port;
  const shellPage = await (await browser.newContext()).newPage();
  await shellPage.goto('http://127.0.0.1:' + port + '/' +
    encodeURIComponent('AI-Agent监控台.shell.html'));
  await shellPage.waitForSelector('#ov-kpis .card', { timeout: 15000 });
  const shellView = await collectLegacy(shellPage);

  // 成品单文件 file:// 直开（现状语义基线）。
  const outFilePage = await (await browser.newContext()).newPage();
  await outFilePage.goto(pathToFileURL(outPath).href);
  await outFilePage.waitForSelector('#ov-kpis .card', { timeout: 15000 });
  const fileView = await collectLegacy(outFilePage);

  assert.equal(shellView.tabs, fileView.tabs, '页签数必须与成品一致');
  assert.equal(shellView.tabs, 6);
  assert.deepEqual(shellView.kpis, fileView.kpis, '总览卡（标签+数值）必须与成品一致');
  assert.equal(shellView.agents, fileView.agents, 'Agent 卡数量必须与成品一致');
  assert.equal(shellView.genInfo, fileView.genInfo, '导出时间条必须与成品一致');
  assert.equal(shellView.codexThreads, fileView.codexThreads, 'codex 注入区标志（快照线程数）必须与成品一致');
  assert.ok(shellView.codexThreads === 2, 'codex 快照必须来自 sidecar');
  assert.equal(shellView.zmeta, fileView.zmeta, '平台状态 meta 必须与成品注入等价');
  assert.equal(shellView.statusBar, fileView.statusBar, '平台状态栏必须与成品等价');
  assert.equal(shellView.statusBar, 'ZCode', 'ready 数据下状态栏必须为 ZCode');
  assert.equal(shellView.applyLatest, 'function', 'shell 模式必须注册 __gaugeApplyLatest');
  assert.equal(fileView.applyLatest, 'undefined', '成品模式不注册（外部 && 短路安全）');
  assert.ok(!shellView.fatalShown && !fileView.fatalShown);

  // ---- 热替换：改 sidecar 的 generated_at 与统计值（撤一条请求 → 请求数 3→2），
  // evaluate __gaugeApplyLatest() 后 DOM 更新、且未发生整页导航。
  const sidecar = JSON.parse(fs.readFileSync(sidecarPath, 'utf8'));
  sidecar.generated_at = '2026-09-22 23:59:00';
  sidecar.data.meta.generated_at = sidecar.generated_at;
  const removed = sidecar.data.requests.pop();
  assert.ok(removed, '至少需要一条请求用于热替换');
  // codex 快照一并演进：新增线程 + 快照时间独立更新，供下方平台切换断言
  // 验证 codex.js 惰性捕获确实吃到注水后的新对象（而非加载时的空快照）。
  sidecar.codex.threads.push({ id: 't3', title: '任务三', updated_at: Date.now() });
  sidecar.codex.generated_at = '2026-09-22T23:59:00+08:00';
  fs.writeFileSync(sidecarPath, JSON.stringify(sidecar), 'utf8');

  await shellPage.evaluate(() => {
    window.__ren = 0;
    const orig = window.PAGE_RENDER.overview;
    window.PAGE_RENDER.overview = function () {
      window.__ren += 1;
      return orig.apply(this, arguments);
    };
  });
  const navBefore = await shellPage.evaluate(() => ({
    navs: performance.getEntriesByType('navigation').length,
    origin: performance.timeOrigin,
  }));
  const applied = await shellPage.evaluate(() => window.__gaugeApplyLatest());
  assert.equal(applied, true, 'revision 变化必须触发热替换');
  await shellPage.waitForFunction(
    (label) => {
      const card = [...document.querySelectorAll('#ov-kpis .card')]
        .find((c) => c.querySelector('.k').textContent === label);
      return card && card.querySelector('.v').textContent === '2';
    },
    '模型请求数', { timeout: 15000 });
  const afterSwap = {
    view: await collectLegacy(shellPage),
    nav: await shellPage.evaluate(() => ({
      navs: performance.getEntriesByType('navigation').length,
      origin: performance.timeOrigin,
    })),
    ren: await shellPage.evaluate(() => window.__ren),
  };
  assert.equal(kpiValue(afterSwap.view, '模型请求数'), '2', '热替换后 KPI 必须更新');
  assert.ok(afterSwap.view.genInfo.includes('2026-09-22 23:59:00'), '导出时间必须随 sidecar 更新');
  assert.equal(afterSwap.view.codexThreads, 3, '热替换必须同步 window.GAUGE_CODEX（含新线程）');
  assert.equal(afterSwap.nav.navs, navBefore.navs, '热替换禁止整页导航');
  assert.equal(afterSwap.nav.origin, navBefore.origin, '文档未重载（timeOrigin 不变）');
  assert.ok(afterSwap.ren >= 1, '热替换必须重渲染当前页');

  // ---- 判重 no-op：generated_at 未变时调用必须空转（返回 false、无重渲染）。
  const renBefore = afterSwap.ren;
  const appliedAgain = await shellPage.evaluate(() => window.__gaugeApplyLatest());
  assert.equal(appliedAgain, false, 'revision 未变必须 no-op');
  const renAfter = await shellPage.evaluate(() => window.__ren);
  assert.equal(renAfter, renBefore, 'no-op 不得触发重渲染');

  // ---- Codex 平台切换（shell 形态主用路径）：codex.js 惰性捕获必须让看板
  // 吃到注水后的快照——热替换新增的线程与快照时间出现在看板里，而非
  // “尚未读取”空态。独立页面无 pywebview 桥，select 直接本地生效。
  await shellPage.selectOption('#gauge-platform', 'codex');
  await shellPage.waitForFunction(
    () => { const a = document.getElementById('codex-app'); return a && a.hidden === false; },
    null, { timeout: 15000 });
  await shellPage.click('#codex-app [data-tab="tasks"]');
  await shellPage.waitForFunction(
    () => document.getElementById('codex-app').textContent.includes('任务三'),
    null, { timeout: 15000 });
  const codexBoard = await shellPage.evaluate(() => {
    const app = document.getElementById('codex-app');
    return { text: app.textContent, toolbar: app.querySelector('.cx-toolbar').textContent };
  });
  assert.ok(codexBoard.text.includes('任务一') && codexBoard.text.includes('任务二')
    && codexBoard.text.includes('任务三'), 'Codex 看板必须渲染注水后的全部线程');
  assert.ok(codexBoard.toolbar.includes('2026-09-22T23:59:00+08:00'),
    '看板“更新于”必须来自热替换后的快照');
  assert.ok(!codexBoard.text.includes('尚未读取'), '不得停留在未读取空态');
  assert.ok(!codexBoard.text.includes('暂不展示统计值'), '可用快照不得落入不可用空态');

  // ---- P2-2：停留在 Codex 页签时热替换，看板“更新于”必须立即随新快照变化
  // 且无空态（applyLatest 经 gaugePlatformState 重入 applyPlatform → render）。
  const toolbarBefore = await shellPage.evaluate(
    () => document.querySelector('#codex-app .cx-toolbar').textContent);
  const sidecar2 = JSON.parse(fs.readFileSync(sidecarPath, 'utf8'));
  sidecar2.generated_at = '2026-09-23 00:30:00';
  sidecar2.data.meta.generated_at = sidecar2.generated_at;
  sidecar2.codex.generated_at = '2026-09-23T00:30:00+08:00';
  sidecar2.codex.threads.push({ id: 't4', title: '任务四', updated_at: Date.now() });
  fs.writeFileSync(sidecarPath, JSON.stringify(sidecar2), 'utf8');
  const appliedCodex = await shellPage.evaluate(() => window.__gaugeApplyLatest());
  assert.equal(appliedCodex, true, 'Codex 页签下热替换必须生效');
  await shellPage.waitForFunction(
    (stamp) => document.querySelector('#codex-app .cx-toolbar').textContent.includes(stamp),
    '2026-09-23T00:30:00+08:00', { timeout: 15000 });
  const boardAfter = await shellPage.evaluate(() => {
    const app = document.getElementById('codex-app');
    return { toolbar: app.querySelector('.cx-toolbar').textContent, text: app.textContent };
  });
  assert.notEqual(boardAfter.toolbar, toolbarBefore, '看板“更新于”必须立即变化');
  assert.ok(boardAfter.text.includes('任务四'), 'Codex 看板必须立即渲染新线程');
  assert.ok(!boardAfter.text.includes('尚未读取'), '热替换不得落入未读取空态');

  // ---- P2-1：ZCode unavailable 场景，shell 注水后状态栏文案与成品等价
  //（applyPlatform 对 meta.available===false 透出 meta.error；重开新上下文
  // 模拟首次注水路径，localStorage 隔离不受前序 codex 偏好影响）。
  const sidecar3 = JSON.parse(fs.readFileSync(sidecarPath, 'utf8'));
  sidecar3.data.meta.available = false;
  sidecar3.data.meta.error = '测试：ZCode 数据不可用';
  sidecar3.generated_at = '2026-09-23 00:40:00';
  sidecar3.data.meta.generated_at = sidecar3.generated_at;
  fs.writeFileSync(sidecarPath, JSON.stringify(sidecar3), 'utf8');
  const unavailPage = await (await browser.newContext()).newPage();
  await unavailPage.goto('http://127.0.0.1:' + port + '/' +
    encodeURIComponent('AI-Agent监控台.shell.html'));
  await unavailPage.waitForSelector('#ov-kpis .card', { timeout: 15000 });
  const statusBar = await unavailPage.evaluate(
    () => document.getElementById('gauge-platform-status').textContent);
  assert.equal(statusBar, '测试：ZCode 数据不可用', 'unavailable 状态栏必须透出 meta.error');
  await unavailPage.context().close();

  console.log('shell 双形态渲染与热替换：通过');
} finally {
  if (server) server.close();
  if (serverBare) serverBare.close();
  if (browser) await browser.close().catch(() => {});
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.rmSync(tmpBare, { recursive: true, force: true });
}
