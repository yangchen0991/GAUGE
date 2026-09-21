// 用最小 DOM 驱动公开交互，验证数据筛选和桌面确认顺序；像素布局另做浏览器验收。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../web/codex.js', import.meta.url), 'utf8');
const now = Date.now() - 1000;
const snapshot = {
  available: true, status: 'ready', coverage: { complete: true },
  threads: [
    { id: 'parent', title: '<img src=x onerror=alert(1)>', updated_at: now, model: 'B' },
    { id: 'child', title: '子任务', parent_id: 'parent', updated_at: now, model: 'B' }
  ],
  edges: [
    { parent_id: 'parent', child_id: 'child' },
    { parent_id: 'parent', child_id: 'child' },
    { parent_id: 'child', child_id: 'parent' }
  ],
  usage: [
    { id: 'r1', thread_id: 'parent', turn_id: 't1', model: 'A', timestamp: now,
      input_tokens: 100, cached_input_tokens: 80, output_tokens: 20, reasoning_output_tokens: 10, total_tokens: 120 },
    { id: 'r2', thread_id: 'parent', turn_id: 't2', model: 'B', timestamp: now,
      input_tokens: 200, cached_input_tokens: 100, output_tokens: 30, reasoning_output_tokens: 20, total_tokens: 230 },
    { id: 'r3', thread_id: 'child', turn_id: 't3', model: 'B', timestamp: now,
      input_tokens: 300, cached_input_tokens: 150, output_tokens: 40, reasoning_output_tokens: 30, total_tokens: 340 }
  ],
  turns: [
    { id: 't1', thread_id: 'parent', started_at: now, status: 'completed' },
    { id: 't2', thread_id: 'parent', started_at: now, status: 'failed' }
  ],
  tools: [
    { id: 'tool1', thread_id: 'parent', turn_id: 't1', timestamp: now, name: 'A 工具', status: 'completed' },
    { id: 'tool2', thread_id: 'parent', turn_id: 't2', timestamp: now, name: 'B 工具', status: 'failed' }
  ],
  projects: [], goals: [], warnings: [],
  quota: { captured_at: now, primary: { used_percent: 42, window_minutes: 300, resets_at_ms: now + 3600000 } }
};

function mount(data = snapshot, api = null) {
  const elements = [];
  function element() {
    const node = { style: {}, innerHTML: '', listeners: {},
      setAttribute() {}, appendChild() {}, insertAdjacentElement() {},
      addEventListener(name, handler) { this.listeners[name] = handler; } };
    elements.push(node);
    return node;
  }
  const legacy = element();
  const events = {};
  const storage = new Map();
  const document = {
    createElement: element, querySelector: () => legacy,
    getElementById: id => elements.find(node => node.id === id) || legacy,
    addEventListener: (name, handler) => { events[name] = handler; }
  };
  const window = { GAUGE_CODEX: data, pywebview: api ? { api } : null };
  vm.runInNewContext(source, {
    window, document, Date, Map, Set, Promise, Number, String,
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    setTimeout() {}
  });
  const app = elements.find(node => node.id === 'codex-app');
  const select = elements.find(node => node.id === 'gauge-platform');
  function filter(key, value) { app.listeners.change({ target: { value, getAttribute: () => key } }); }
  function tab(value) {
    app.listeners.click({ target: { closest: () => ({
      hasAttribute: key => key === 'data-tab', getAttribute: () => value
    }) } });
  }
  return { window, app, select, filter, tab };
}

const ui = mount();
ui.window.gaugeApplyPlatform({ platform: 'codex', revision: 2 });
assert.match(ui.app.innerHTML, /Token 总量<\/span><strong>690<\/strong>/);
assert.match(ui.app.innerHTML, /&lt;img src=x onerror=alert\(1\)&gt;/);
assert.ok(!ui.app.innerHTML.includes('<img src=x'));

// 一个线程中途切换模型，不能把另一个模型的失败回合和工具也带入。
ui.filter('model', 'A');
assert.match(ui.app.innerHTML, /Token 总量<\/span><strong>120<\/strong>/);
assert.match(ui.app.innerHTML, /失败回合<\/span><strong>0<\/strong>/);
assert.match(ui.app.innerHTML, /已用 42%/);
ui.tab('tools');
assert.ok(ui.app.innerHTML.includes('A 工具'));
assert.ok(!ui.app.innerHTML.includes('B 工具'));

// 损坏的父子环和重复边不能导致递归失控或用量重复。
ui.window.gaugeOpenTask('parent');
assert.match(ui.app.innerHTML, /自身已读取 Token<\/span><strong>350<\/strong>/);
assert.match(ui.app.innerHTML, /含子 Agent Token<\/span><strong>690<\/strong>/);
assert.match(ui.app.innerHTML, /后代 Agent<\/span><strong>1<\/strong>/);
ui.window.gaugeApplyPlatform({ platform: 'zcode', revision: 1 });
assert.equal(ui.select.value, 'codex');
ui.window.gaugeApplyPlatform({ platform: 'zcode', revision: 3 });
assert.equal(ui.app.hidden, true);

const missing = mount({ available: false, status: 'unavailable' });
missing.window.gaugeApplyPlatform({ platform: 'codex' });
assert.ok(missing.app.innerHTML.includes('暂不展示统计值'));
assert.ok(!missing.app.innerHTML.includes('Token 总量'));

// 请求已发出并不表示主进程已保存配置；收到新 revision 才更新平台。
let requested;
const desktop = mount(snapshot, { set_platform: value => { requested = value; return true; } });
desktop.select.value = 'codex';
desktop.select.listeners.change();
assert.equal(requested, 'codex');
assert.equal(desktop.app.hidden, true);
assert.equal(desktop.select.disabled, true);
desktop.window.gaugeApplyPlatform({ platform: 'codex', revision: 1 });
assert.equal(desktop.app.hidden, false);
assert.equal(desktop.select.disabled, false);

// 项目页签：空 projects 快照下应出现「未归属」桶；点击展开详情并可跳任务详情。
ui.tab('projects');
assert.ok(ui.app.innerHTML.includes('data-tab="projects"'));
assert.ok(ui.app.innerHTML.includes('未归属'));
assert.ok(ui.app.innerHTML.includes('当前范围没有可展示的项目记录') === false);
function openProject(id) {
  ui.app.listeners.click({ target: { closest: () => ({
    hasAttribute: key => key === 'data-project', getAttribute: () => id
  }) } });
}
openProject('__unassigned__');
assert.ok(ui.app.innerHTML.includes('关联任务'));
assert.ok(ui.app.innerHTML.includes('未归属'));
assert.ok(ui.app.innerHTML.includes('目录推断') === false); // 无 project_id 的线程不足“未归属”文案外另一层
assert.match(ui.app.innerHTML, /已读取 Token<\/span><strong>690<\/strong>/);
// 详情里的任务按钮跳任务页签并打开回合时间线。
function clickTask(id) {
  ui.app.listeners.click({ target: { closest: () => ({
    hasAttribute: key => key === 'data-task', getAttribute: () => id
  }) } });
}
clickTask('parent');
assert.ok(ui.app.innerHTML.includes('回合时间线'));
// 回到项目页：任务跳转不清除项目选择，详情仍展开；再次点击同一项目收回（toggle）。
ui.tab('projects');
openProject('__unassigned__');
assert.ok(!ui.app.innerHTML.includes('关联任务 · 2 条'));
// 再点一次重新展开。
openProject('__unassigned__');
assert.ok(ui.app.innerHTML.includes('关联任务 · 2 条'));
console.log('Codex 页面交互与统计边界：通过');
