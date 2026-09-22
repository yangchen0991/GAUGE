/* GAUGE Codex 看板：只消费白名单统计快照，不访问源数据库或认证文件。
 * 平台选择在桌面由主进程决定；独立 HTML 才使用 localStorage 保存偏好。
 * 过滤与任务子树统计都以单次用量记录的 thread_id 为归属，累计字段不参与相加。
 * 项目列表按范围内选中集聚合；项目详情与任务详情一致，消费全量已读取历史。
 */
(function () {
  'use strict';

  // 惰性捕获（W8 shell 双形态异步注水）：shell 形态下本脚本执行时
  // window.GAUGE_CODEX 仍为 null，数据由页面 fetch sidecar 之后才挂回全局；
  // 若沿用加载时一次性捕获，Codex 看板将永远停留在“尚未读取”空态。因此改为
  // 访问器 + 按快照对象身份重建索引：成品形态快照在脚本加载前已置位，首建后
  // 身份不再变化，行为与原一次性捕获完全一致。统计口径仅在 rebuildIndex 内
  // 原样搬运，未做任何增删。
  function codexSnapshot() { return window.GAUGE_CODEX || EMPTY_SNAPSHOT; }
  var EMPTY_SNAPSHOT = {};   // 空快照恒定身份：无数据时反复 render 不触发重建
  var snapshot = null;
  var threads = [], usage = [], turns = [], tools = [], edges = [];
  var threadById = new Map(), childrenById = new Map(), projectById = new Map();

  // 从当前快照重建闭包索引（含按 thread_id 的父子边与项目映射）；
  // 快照对象身份未变时直接返回，成品路径反复 render 零额外开销。
  function rebuildIndex() {
    var current = codexSnapshot();
    if (current === snapshot) return;
    snapshot = current;
    threads = Array.isArray(current.threads) ? current.threads : [];
    usage = Array.isArray(current.usage) ? current.usage : [];
    turns = Array.isArray(current.turns) ? current.turns : [];
    tools = Array.isArray(current.tools) ? current.tools : [];
    edges = Array.isArray(current.edges) ? current.edges : [];
    threadById = new Map(threads.map(function (thread) { return [thread.id, thread]; }));
    childrenById = new Map();
    edges.forEach(function (edge) {
      var children = childrenById.get(edge.parent_id) || new Set();
      children.add(edge.child_id);
      childrenById.set(edge.parent_id, children);
    });
    projectById = new Map((current.projects || []).map(function (project) { return [project.id, project]; }));
  }
  rebuildIndex();

  function readPreference(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) || fallback; }
    catch (_) { return fallback; }
  }
  function savePreference(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* 私密浏览允许不持久化。 */ }
  }
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (character) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
    });
  }
  function numeric(value) { return typeof value === 'number' && Number.isFinite(value) ? value : 0; }
  function formatNumber(value) { return value == null ? '—' : numeric(value).toLocaleString('zh-CN'); }
  function formatTime(value) {
    if (!value) return '—';
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
  }
  function duration(value) {
    if (value == null) return '—';
    var seconds = Math.round(value / 1000);
    return seconds >= 60 ? Math.floor(seconds / 60) + '分 ' + seconds % 60 + '秒' : seconds + '秒';
  }
  function dayKey(timestamp) {
    var date = new Date(timestamp);
    return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
  }
  function statusLabel(status) {
    return { completed: '已完成', failed: '失败', interrupted: '已中断', inProgress: '记录未结束',
      active: '最近活动', open: '关系未关闭', closed: '关系已关闭', idle: '空闲记录',
      usage_limited: '用量受限', pending: '等待记录' }[status] || status || '未记录';
  }
  function bridge() { return window.pywebview && window.pywebview.api; }

  var allowedTabs = ['overview', 'usage', 'projects', 'tasks', 'agents', 'tools', 'environment'];
  // 未归属桶在 view.projectId 里需要独立编码：空字符串表示“未打开详情”，桶与关闭态不能共用值。
  var UNASSIGNED_KEY = '__unassigned__';
  var saved = readPreference('gauge.codex.view.v1', {});
  var view = {
    tab: allowedTabs.includes(saved.tab) ? saved.tab : 'overview',
    days: ['7', '14', '30', 'all'].includes(saved.days) ? saved.days : '7',
    project: typeof saved.project === 'string' ? saved.project : '',
    model: typeof saved.model === 'string' ? saved.model : '',
    query: typeof saved.query === 'string' ? saved.query : '',
    projectId: typeof saved.projectId === 'string' ? saved.projectId : '',
    page: 0, taskId: typeof saved.taskId === 'string' ? saved.taskId : ''
  };
  var activePlatform = 'zcode';
  var platformRevision = -1;
  var legacyMain = document.querySelector('body > main');
  var legacyTabs = document.getElementById('tabs');
  var legacyRange = document.getElementById('range-sel');
  var legacyGenerated = document.getElementById('gen-info');
  var legacyFooter = document.querySelector('body > footer');
  var app = document.createElement('div');
  app.id = 'codex-app';
  app.hidden = true;
  if (!legacyMain) return;
  legacyMain.insertAdjacentElement('afterend', app);

  var platformSelect = document.createElement('select');
  platformSelect.id = 'gauge-platform';
  platformSelect.setAttribute('aria-label', '监控平台');
  platformSelect.innerHTML = '<option value="zcode">ZCode</option><option value="codex">Codex</option>';
  var platformStatus = document.createElement('span');
  platformStatus.id = 'gauge-platform-status';
  var header = document.querySelector('header .row1');
  header.appendChild(platformSelect);
  header.appendChild(platformStatus);

  // 桌面必须等待主进程确认；网页中的临时选择不能成为第二份平台真值。
  function requestPlatform(platform) {
    if (platform !== 'zcode' && platform !== 'codex') return;
    var api = bridge();
    if (!api || typeof api.set_platform !== 'function') {
      applyPlatform({ platform: platform });
      return;
    }
    platformSelect.disabled = true;
    platformStatus.textContent = '正在切换…';
    Promise.resolve(api.set_platform(platform)).then(function (accepted) {
      if (accepted === false) throw new Error('平台切换请求未成功');
    }).catch(function (error) {
      platformSelect.disabled = false;
      platformSelect.value = activePlatform;
      platformStatus.textContent = error.message;
    });
    setTimeout(function () {
      if (!platformSelect.disabled) return;
      Promise.resolve(api.get_platform()).then(applyPlatform).catch(function () {
        platformSelect.disabled = false;
        platformSelect.value = activePlatform;
        platformStatus.textContent = '未收到桌面确认，请重试';
      });
    }, 3000);
  }

  function applyPlatform(state) {
    if (!state || !['zcode', 'codex'].includes(state.platform)) return;
    if (Number.isInteger(state.revision) && state.revision < platformRevision) {
      // 迟到的旧 revision 不能回滚平台，但若切换仍在等待确认必须解除禁用，防止选择器卡死。
      if (platformSelect.disabled) {
        platformSelect.disabled = false;
        platformSelect.value = activePlatform;
        platformStatus.textContent = '已忽略过期的切换确认，请重试';
      }
      return;
    }
    if (Number.isInteger(state.revision)) platformRevision = state.revision;
    activePlatform = state.platform;
    platformSelect.disabled = false;
    platformSelect.value = activePlatform;
    var isCodex = activePlatform === 'codex';
    [legacyMain, legacyTabs, legacyRange, legacyGenerated, legacyFooter].forEach(function (element) {
      if (element) element.style.display = isCodex ? 'none' : '';
    });
    app.hidden = !isCodex;
    var meta = window.GAUGE_ZCODE_META || {};
    platformStatus.textContent = isCodex ? 'Codex · 本地记录' : meta.available === false ? (meta.error || 'ZCode 数据暂不可用') : 'ZCode';
    savePreference('gauge.platform', activePlatform);
    if (isCodex) render();
  }
  window.gaugeApplyPlatform = applyPlatform;
  // 只读快照钩子（W8 shell 注水重入）：页面在 GAUGE_ZCODE_META/GAUGE_CODEX
  // 注水完成后经 gaugeApplyPlatform 重入一次，用当前平台与 revision 刷新状态
  // 栏（P2-1）；active 平台为 codex 时 applyPlatform 内部 render() 同步重渲染
  // 看板（P2-2）。同 revision 可过守卫、同平台仅重写状态与可见性，重复调用
  // 幂等；桌面确认链（desktopReady → get_platform）不受影响。
  window.gaugePlatformState = function () {
    return { platform: activePlatform, revision: platformRevision };
  };
  window.gaugeOpenTask = function (id) {
    if (typeof id !== 'string') return;
    view.tab = 'tasks'; view.taskId = id; view.page = 0;
    applyPlatform({ platform: 'codex' });
  };
  window.gaugeOpenQuota = function () {
    view.tab = 'overview';
    applyPlatform({ platform: 'codex' });
    var quota = document.getElementById('cx-account-quota');
    if (quota) quota.scrollIntoView({ block: 'center' });
  };
  platformSelect.addEventListener('change', function () { requestPlatform(platformSelect.value); });

  // 过滤按用量发生时间执行；任务表保留在范围内有活动或用量的线程。
  function selection() {
    var end = Date.now();
    var start = new Date(); start.setHours(0, 0, 0, 0);
    if (view.days !== 'all') start.setDate(start.getDate() - Number(view.days) + 1);
    var since = view.days === 'all' ? 0 : start.getTime();
    var ids = new Set(threads.filter(function (thread) {
      var text = ((thread.title || '') + ' ' + (thread.cwd || '') + ' ' + thread.id).toLowerCase();
      return (!view.project || (thread.project_id || '') === view.project) &&
        (!view.query || text.includes(view.query.toLowerCase()));
    }).map(function (thread) { return thread.id; }));
    var selectedUsage = usage.filter(function (record) {
      return numeric(record.timestamp) >= since && numeric(record.timestamp) <= end &&
        ((!view.project && !view.query) || ids.has(record.thread_id)) && (!view.model || record.model === view.model);
    });
    var usageIds = new Set(selectedUsage.map(function (record) { return record.thread_id; }));
    var selectedThreads = threads.filter(function (thread) {
      var text = ((thread.title || '') + ' ' + (thread.cwd || '') + ' ' + thread.id).toLowerCase();
      return ids.has(thread.id) && (!view.model || thread.model === view.model || usageIds.has(thread.id)) &&
        (usageIds.has(thread.id) || numeric(thread.updated_at) >= since) && (!view.query || text.includes(view.query.toLowerCase()));
    });
    // 线程可能中途换模型。模型筛选下只纳入有对应响应证据的回合，
    // 不能用线程最后一次模型把整段历史的工具和回合都算到同一模型。
    var modelTurns = new Set(selectedUsage.map(function (record) { return record.thread_id + '/' + record.turn_id; }));
    return {
      usage: selectedUsage, threads: selectedThreads,
      turns: turns.filter(function (turn) { return ((!view.project && !view.query) || ids.has(turn.thread_id)) && (!view.model || modelTurns.has(turn.thread_id + '/' + turn.id)) && numeric(turn.started_at) >= since && numeric(turn.started_at) <= end; }),
      tools: tools.filter(function (tool) { return ((!view.project && !view.query) || ids.has(tool.thread_id)) && (!view.model || modelTurns.has(tool.thread_id + '/' + tool.turn_id)) && numeric(tool.timestamp) >= since && numeric(tool.timestamp) <= end; })
    };
  }

  function total(records, field) { return records.reduce(function (sum, record) { return sum + numeric(record[field]); }, 0); }
  function tokenCards(records) {
    var input = total(records, 'input_tokens');
    var cached = total(records, 'cached_input_tokens');
    return cards([
      ['Token 总量', total(records, 'total_tokens'), '输入 + 输出；不重复加缓存和推理'],
      ['输入 Token', input, '其中缓存 ' + formatNumber(cached)],
      ['输出 Token', total(records, 'output_tokens'), '其中推理 ' + formatNumber(total(records, 'reasoning_output_tokens'))],
      ['缓存占比', input ? (cached / input * 100).toFixed(1) + '%' : '—', '缓存读取 / 总输入']
    ]);
  }
  function cards(items) {
    return '<div class="cx-grid">' + items.map(function (item) {
      return '<div class="cx-card"><span>' + escapeHtml(item[0]) + '</span><strong>' +
        escapeHtml(typeof item[1] === 'number' ? formatNumber(item[1]) : item[1]) + '</strong><small>' + escapeHtml(item[2] || '') + '</small></div>';
    }).join('') + '</div>';
  }
  function table(headers, rows) {
    if (!rows.length) return '<div class="cx-empty">当前范围没有可展示的记录</div>';
    return '<div class="cx-table-wrap"><table><thead><tr>' + headers.map(function (title) { return '<th>' + escapeHtml(title) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + rows.map(function (row) { return '<tr>' + row.map(function (cell) { return '<td>' + cell + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody></table></div>';
  }
  function panel(title, content) { return '<div class="cx-panel"><h2>' + escapeHtml(title) + '</h2>' + content + '</div>'; }
  function taskButton(thread) { return '<button class="cx-task" data-task="' + escapeHtml(thread.id) + '">' + escapeHtml(thread.title || thread.id) + '</button>'; }
  function rankings(records, key) {
    var grouped = new Map();
    records.forEach(function (record) { var name = key(record) || '未记录'; grouped.set(name, (grouped.get(name) || 0) + numeric(record.total_tokens)); });
    var rows = Array.from(grouped.entries()).sort(function (a, b) { return b[1] - a[1]; }).slice(0, 12);
    var maximum = rows.length ? rows[0][1] || 1 : 1;
    return rows.length ? rows.map(function (row) {
      return '<div class="cx-bar-row"><span title="' + escapeHtml(row[0]) + '">' + escapeHtml(row[0]) + '</span><div class="cx-bar-track"><div class="cx-bar-fill" style="width:' + (row[1] / maximum * 100).toFixed(2) + '%"></div></div><span>' + formatNumber(row[1]) + '</span></div>';
    }).join('') : '<div class="cx-empty">没有已核实的用量</div>';
  }

  function trend(records) {
    var buckets = new Map();
    records.forEach(function (record) { var key = dayKey(record.timestamp); buckets.set(key, (buckets.get(key) || 0) + numeric(record.total_tokens)); });
    var end = new Date(); end.setHours(0, 0, 0, 0);
    var days = view.days === 'all' ? 90 : Number(view.days);
    var dates = [];
    for (var offset = days - 1; offset >= 0; offset--) { var day = new Date(end); day.setDate(day.getDate() - offset); dates.push(dayKey(day)); }
    var values = dates.map(function (key) { return buckets.get(key) || 0; });
    var maximum = Math.max.apply(null, values.concat([1]));
    var width = 900 / dates.length;
    var bars = values.map(function (value, index) { var height = value / maximum * 140; return '<rect x="' + (index * width) + '" y="' + (150 - height) + '" width="' + Math.max(1, width - 3) + '" height="' + height + '" fill="#FFB300"><title>' + dates[index] + ' · ' + formatNumber(value) + '</title></rect>'; }).join('');
    return '<svg class="cx-chart" viewBox="0 0 900 160" role="img" aria-label="每日Token趋势">' + bars + '</svg><div class="cx-chart-labels"><span>' + dates[0] + '</span><span>' + (view.days === 'all' ? '趋势展示最近90天；其他统计使用全部数据' : '按本地自然日') + '</span><span>' + dates[dates.length - 1] + '</span></div>';
  }

  function quotaPanel() {
    var quota = snapshot.quota;
    if (!quota) return panel('账号额度', '<div class="cx-empty">暂不可用 · 尚未读取到额度记录</div>');
    var content = ['primary', 'secondary'].map(function (key) {
      var windowData = quota[key];
      if (!windowData || windowData.used_percent == null) return '<p class="cx-muted">' + (key === 'primary' ? '短周期' : '长周期') + '：暂不可用</p>';
      // quota 为冻结形状：resets_at_ms 已是毫秒时间戳，直接消费，不再 ×1000。
      var expires = numeric(windowData.resets_at_ms);
      var expired = expires > 0 && expires <= Date.now();
      return '<div><span>' + escapeHtml(windowData.window_minutes === 300 ? '5 小时' : windowData.window_minutes === 10080 ? '每周' : windowData.window_minutes + ' 分钟') + ' · 已用 ' + escapeHtml(windowData.used_percent) + '%</span><div class="cx-quota"><span style="width:' + Math.max(0, Math.min(100, numeric(windowData.used_percent))) + '%"></span></div><small class="cx-muted">' + (expired ? '记录窗口已到期；等待新快照' : '记录重置时间 ' + formatTime(expires)) + '</small></div>';
    }).join('<br>');
    return '<div id="cx-account-quota">' + panel('账号额度 · 不受项目筛选影响', content + '<p class="cx-muted">日志快照采集于 ' + formatTime(quota.captured_at) + '，并非实时账号查询。</p>') + '</div>';
  }

  function taskRows(list, records) {
    var sums = new Map();
    records.forEach(function (record) { sums.set(record.thread_id, (sums.get(record.thread_id) || 0) + numeric(record.total_tokens)); });
    return list.map(function (thread) {
      return [taskButton(thread), escapeHtml(thread.project_name || '未归属'), escapeHtml(thread.model || '未记录'),
        escapeHtml(statusLabel(thread.status)), formatNumber(sums.get(thread.id) || 0), escapeHtml(formatTime(thread.updated_at))];
    });
  }
  function overview(selected) {
    var failed = selected.turns.filter(function (turn) { return turn.status === 'failed'; }).length;
    var recent = selected.threads.slice().sort(function (a, b) { return b.updated_at - a.updated_at; }).slice(0, 8);
    return tokenCards(selected.usage) + cards([
      ['有活动的任务记录', selected.threads.length, '含主任务及子 Agent'], ['模型响应记录', selected.usage.length, '按 response_id 去重'],
      ['回合记录', selected.turns.length, '与模型请求数分别统计'], ['失败回合', failed, '不等于诊断日志错误数']
    ]) + '<div class="cx-two">' + panel('Token 趋势', trend(selected.usage)) + quotaPanel() + '</div>' +
      panel('最近活动', table(['任务', '项目', '模型', '历史状态', '范围内自身 Token', '最后更新'], taskRows(recent, selected.usage))) +
      '<div class="cx-two">' + panel('模型用量', rankings(selected.usage, function (record) { return record.model; })) +
      panel('项目用量', rankings(selected.usage, function (record) { var thread = threadById.get(record.thread_id); return thread && thread.project_name || '未归属'; })) + '</div>';
  }

  function usagePage(selected) {
    return tokenCards(selected.usage) + panel('每日 Token', trend(selected.usage)) + '<div class="cx-two">' +
      panel('模型', rankings(selected.usage, function (record) { return record.model; })) +
      panel('项目', rankings(selected.usage, function (record) { var thread = threadById.get(record.thread_id); return thread && thread.project_name || '未归属'; })) +
      panel('Provider', rankings(selected.usage, function (record) { return record.provider; })) +
      panel('Agent 角色', rankings(selected.usage, function (record) { var thread = threadById.get(record.thread_id); return thread && thread.role || '主任务'; })) + '</div>' +
      '<div class="cx-note">用量只统计已读取、可去重的响应。缓存包含在输入内，推理包含在输出内。线程记录的累计值不与响应再次相加；订阅用量不换算为现金账单。</div>';
  }

  // 项目页签：列表聚合范围内选中集（与总览筛选同源）；详情与任务详情一致，消费全量已读取历史。
  // 未归属桶 = 没有项目记录且工作目录未匹配的线程；线程记录缺失的用量也并入该桶（与总览排行口径一致）。
  function projectKeyOf(thread) { return thread && thread.project_id ? thread.project_id : UNASSIGNED_KEY; }
  function projectMembers(id) {
    return threads.filter(function (thread) {
      return id === UNASSIGNED_KEY ? !thread.project_id : thread.project_id === id;
    });
  }
  function projectsPage(selected) {
    var counts = new Map(), tokens = new Map(), lastSeen = new Map();
    selected.threads.forEach(function (thread) {
      var key = projectKeyOf(thread);
      counts.set(key, (counts.get(key) || 0) + 1);
      lastSeen.set(key, Math.max(lastSeen.get(key) || 0, numeric(thread.updated_at)));
    });
    selected.usage.forEach(function (record) {
      var key = projectKeyOf(threadById.get(record.thread_id));
      tokens.set(key, (tokens.get(key) || 0) + numeric(record.total_tokens));
    });
    var rows = (snapshot.projects || []).map(function (project) { return { id: project.id, name: project.name || project.id }; });
    var seen = new Set();
    rows = rows.filter(function (row) { if (seen.has(row.id)) return false; seen.add(row.id); return true; });
    if (counts.has(UNASSIGNED_KEY) || tokens.has(UNASSIGNED_KEY)) rows.push({ id: UNASSIGNED_KEY, name: '未归属' });
    rows.forEach(function (row) {
      row.count = counts.get(row.id) || 0;
      row.tokens = tokens.get(row.id) || 0;
      row.last = lastSeen.get(row.id) || 0;
    });
    // Token 降序；未归属桶恒排最末，避免长期大桶压顶盖住真实项目。
    rows.sort(function (a, b) { return b.tokens - a.tokens; });
    rows = rows.filter(function (row) { return row.id !== UNASSIGNED_KEY; })
      .concat(rows.filter(function (row) { return row.id === UNASSIGNED_KEY; }));
    var list = rows.length ? panel('项目 · ' + rows.length + ' 个', table(['项目', '任务数（范围内）', 'Token（范围内）', '最近活动'], rows.map(function (row) {
      var button = '<button class="cx-task" data-project="' + escapeHtml(row.id) + '">' + escapeHtml(row.name) + '</button>';
      return [button, formatNumber(row.count), formatNumber(row.tokens), escapeHtml(formatTime(row.last))];
    }))) : panel('项目', '<div class="cx-empty">当前范围没有可展示的项目记录</div>');
    return list + (view.projectId ? projectDetail(view.projectId) : '<p class="cx-muted">点击项目名称查看项目详情：根目录、关联任务与用量构成。</p>');
  }
  function projectDetail(id) {
    var unassigned = id === UNASSIGNED_KEY;
    var record = unassigned ? null : projectById.get(id);
    var members = projectMembers(id);
    var memberIds = new Set(members.map(function (thread) { return thread.id; }));
    var own = usage.filter(function (item) { return memberIds.has(item.thread_id); });
    var sums = new Map(), last = 0;
    own.forEach(function (item) { sums.set(item.thread_id, (sums.get(item.thread_id) || 0) + numeric(item.total_tokens)); });
    members.forEach(function (thread) { last = Math.max(last, numeric(thread.updated_at)); });
    var roots = record && Array.isArray(record.roots) && record.roots.length ? record.roots.join('、') : '—';
    var sorted = members.slice().sort(function (a, b) { return numeric(b.updated_at) - numeric(a.updated_at); });
    return '<div class="cx-details"><h2>' + escapeHtml(unassigned ? '未归属' : (record && record.name) || id) + '</h2>' + 
      '<p class="cx-muted">' + escapeHtml(unassigned ? '没有项目记录且工作目录未匹配的任务' : id) + '</p>' +
      cards([['已读取 Token', total(own, 'total_tokens'), '全部已读取历史，不随时间筛选变化'], ['关联任务', members.length, '含子 Agent 任务'], ['最近活动', members.length ? formatTime(last) : '—', '文件侧口径，不代表正在运行']]) +
      panel('项目信息', keyValues({ '项目 ID': unassigned ? '—' : id, '根目录': roots })) +
      '<div class="cx-two">' + panel('模型构成', rankings(own, function (item) { return item.model; })) + panel('每日 Token', trend(own)) + '</div>' +
      panel('关联任务 · ' + members.length + ' 条', table(['任务', '归属来源', '模型', '历史状态', '已读取 Token', '最后更新'], sorted.map(function (thread) {
        var source = thread.project_inferred ? '目录推断' : (thread.project_id ? '项目记录' : '未归属');
        return [taskButton(thread), escapeHtml(source), escapeHtml(thread.model || '未记录'), escapeHtml(statusLabel(thread.status)), formatNumber(sums.get(thread.id) || 0), escapeHtml(formatTime(thread.updated_at))];
      }))) + '</div>';
  }

  // 子树只用集合求闭包，防止重复边或损坏的环造成重复累计和无限递归。
  function descendants(id) {
    var visited = new Set(); var queue = [id];
    while (queue.length) { var current = queue.pop(); if (visited.has(current)) continue; visited.add(current); (childrenById.get(current) || []).forEach(function (child) { queue.push(child); }); }
    return visited;
  }
  function detail(id) {
    var thread = threadById.get(id);
    if (!thread) return '<div class="cx-note">此任务不在当前已读取的数据中。</div>';
    var own = usage.filter(function (record) { return record.thread_id === id; });
    var subtree = descendants(id);
    var combined = usage.filter(function (record) { return subtree.has(record.thread_id); });
    var taskTurns = turns.filter(function (turn) { return turn.thread_id === id; }).sort(function (a, b) { return b.started_at - a.started_at; });
    var taskTools = tools.filter(function (tool) { return tool.thread_id === id; }).sort(function (a, b) { return b.timestamp - a.timestamp; });
    var goals = (snapshot.goals || []).filter(function (goal) { return goal.thread_id === id; });
    // 契约：仅当存在 token_budget>0 的目标时才展示“已用/预算”比例列，
    // 否则整列省略，避免整列为“—”的空占位。
    var budgetGoals = goals.some(function (goal) { return numeric(goal.token_budget) > 0; });
    return '<div class="cx-details"><h2>' + escapeHtml(thread.title || id) + '</h2><p class="cx-muted">' + escapeHtml(thread.cwd || '') + ' · ' + escapeHtml(id) + '</p>' +
      cards([['自身已读取 Token', total(own, 'total_tokens'), '任务详情使用全部已读取历史'], ['含子 Agent Token', total(combined, 'total_tokens'), '每条响应只按所有者计一次'], ['后代 Agent', subtree.size - 1, '不代表当前仍在运行'], ['索引累计记录', thread.recorded_tokens, '单独列示，不参与上方相加']]) +
      panel('任务记录', keyValues({ '模型 / 角色': (thread.model || '—') + ' / ' + (thread.role || '主任务'), '推理档位记录': thread.reasoning_effort || '未记录', 'Git 分支': thread.git_branch || '未记录', 'Git 提交': thread.git_sha || '未记录', '创建时间': formatTime(thread.created_at), '项目归属': thread.project_inferred ? '目录推断' : thread.project_name || '未归属' })) +
      panel('回合时间线 · 最近100条', table(['开始', '结束', '状态', '耗时', '错误分类'], taskTurns.slice(0, 100).map(function (turn) { return [formatTime(turn.started_at), formatTime(turn.completed_at), escapeHtml(statusLabel(turn.status)), duration(turn.duration_ms), escapeHtml(turn.error_type || '—')]; }))) +
      panel('工具事件 · 最近100条', table(['时间', '类型', '工具', '状态', '耗时'], taskTools.slice(0, 100).map(function (tool) { return [formatTime(tool.timestamp), escapeHtml(tool.type), escapeHtml(tool.name), escapeHtml(statusLabel(tool.status)), duration(tool.duration_ms)]; }))) +
      panel('目标与预算', table(budgetGoals ? ['状态', 'Token 预算', '已记录用量', '已用/预算', '已记录时间'] : ['状态', 'Token 预算', '已记录用量', '已记录时间'], goals.map(function (goal) {
        var budget = numeric(goal.token_budget);
        var cells = [escapeHtml(statusLabel(goal.status)), formatNumber(goal.token_budget), formatNumber(goal.tokens_used)];
        if (budgetGoals) cells.push(budget > 0 ? (numeric(goal.tokens_used) / budget * 100).toFixed(1) + '%' : '—');
        cells.push(duration(goal.time_used_seconds == null ? null : goal.time_used_seconds * 1000));
        return cells;
      }))) + '</div>';
  }
  function tasksPage(selected) {
    var sorted = selected.threads.slice().sort(function (a, b) { return b.updated_at - a.updated_at; });
    var pages = Math.max(1, Math.ceil(sorted.length / 30)); view.page = Math.min(view.page, pages - 1);
    return panel('任务记录 · ' + sorted.length + ' 条', table(['任务', '项目', '模型', '历史状态', '范围内自身 Token', '最后更新'], taskRows(sorted.slice(view.page * 30, view.page * 30 + 30), selected.usage)) +
      '<div class="cx-pager"><button class="cx-button" data-page="-1" ' + (view.page === 0 ? 'disabled' : '') + '>上一页</button><span>' + (view.page + 1) + ' / ' + pages + '</span><button class="cx-button" data-page="1" ' + (view.page >= pages - 1 ? 'disabled' : '') + '>下一页</button></div>') + (view.taskId ? detail(view.taskId) : '<p class="cx-muted">点击任务名称查看回合、用量、子 Agent、工具及目标记录。</p>');
  }

  function agentsPage(selected) {
    var selfTokens = new Map();
    selected.usage.forEach(function (record) { selfTokens.set(record.thread_id, (selfTokens.get(record.thread_id) || 0) + numeric(record.total_tokens)); });
    var visible = new Set(selected.threads.map(function (thread) { return thread.id; }));
    var roots = selected.threads.filter(function (thread) { return !thread.parent_id || !visible.has(thread.parent_id); });
    var visited = new Set();
    function branch(thread, depth) {
      if (visited.has(thread.id) || depth > 15) return '';
      visited.add(thread.id);
      var totalTokens = 0; descendants(thread.id).forEach(function (id) { totalTokens += selfTokens.get(id) || 0; });
      var children = Array.from(childrenById.get(thread.id) || []).map(function (id) { return threadById.get(id); }).filter(function (child) { return child && visible.has(child.id); });
      var label = taskButton(thread) + ' <small>' + escapeHtml(thread.role || '主任务') + ' · 自身 ' + formatNumber(selfTokens.get(thread.id) || 0) + ' / 含后代 ' + formatNumber(totalTokens) + '</small>';
      if (!children.length) return '<div class="cx-leaf">' + label + '</div>';
      return '<details ' + (depth === 0 ? 'open' : '') + '><summary>' + label + '</summary>' + children.map(function (child) { return branch(child, depth + 1); }).join('') + '</details>';
    }
    var tree = roots.slice(0, 60).map(function (thread) { return branch(thread, 0); }).join('');
    var orphans = selected.threads.filter(function (thread) { return !visited.has(thread.id); });
    return '<div class="cx-note">父子关系来自本地记录。关系未关闭不代表仍在运行，Reviewer 角色也不代表已经独立验收通过。树上的“含后代”统计彼此重叠，不能再求总和。</div>' + panel('任务与 Agent · 最多60个根任务', '<div class="cx-tree">' + (tree || '<div class="cx-empty">暂无可展示的关系</div>') + '</div>') + (orphans.length ? panel('其余任务与无法展开的关系', table(['任务', '角色'], orphans.slice(0, 60).map(function (thread) { return [taskButton(thread), escapeHtml(thread.role || '主任务')]; }))) : '');
  }

  function toolsPage(selected) {
    var groups = new Map();
    selected.tools.forEach(function (tool) {
      var key = (tool.server ? tool.server + ' / ' : '') + (tool.name || tool.type || '未记录');
      var group = groups.get(key) || { count: 0, failed: 0, duration: 0, timed: 0 };
      group.count++; if (tool.status === 'failed' || tool.status === 'error') group.failed++;
      if (tool.duration_ms != null) { group.duration += numeric(tool.duration_ms); group.timed++; }
      groups.set(key, group);
    });
    var rows = Array.from(groups.entries()).sort(function (a, b) { return b[1].count - a[1].count; });
    var failures = selected.turns.filter(function (turn) { return turn.status === 'failed'; }).sort(function (a, b) { return b.started_at - a.started_at; });
    return cards([['工具事件', selected.tools.length, '只统计可识别工具类型'], ['失败工具事件', selected.tools.filter(function (tool) { return ['failed', 'error'].includes(tool.status); }).length, '独立于任务结果'], ['失败回合', failures.length, '按回合状态'], ['有耗时的工具', selected.tools.filter(function (tool) { return tool.duration_ms != null; }).length, '缺失耗时不当作零']]) +
      panel('工具分布', table(['工具', '次数', '失败', '平均耗时'], rows.map(function (entry) { var group = entry[1]; return [escapeHtml(entry[0]), formatNumber(group.count), formatNumber(group.failed), duration(group.timed ? group.duration / group.timed : null)]; }))) +
      panel('失败回合 · 最近100条', table(['任务', '时间', '错误分类'], failures.slice(0, 100).map(function (turn) { var thread = threadById.get(turn.thread_id) || { id: turn.thread_id }; return [taskButton(thread), formatTime(turn.started_at), escapeHtml(turn.error_type || '未分类')]; }))) +
      panel('诊断日志 · 全量独立口径', keyValues(snapshot.diagnostics || {}));
  }
  function keyValues(object) {
    return '<dl class="cx-key-values">' + Object.keys(object).map(function (key) {
      var value = object[key];
      if (value && typeof value === 'object') value = JSON.stringify(value, null, 2);
      return '<dt>' + escapeHtml(key) + '</dt><dd>' + escapeHtml(value == null ? '未记录' : value) + '</dd>';
    }).join('') + '</dl>';
  }
  function environmentPage() {
    return '<div class="cx-note">以下展示的是本地数据与配置声明。已安装、已配置和观察到实际调用是不同事实；不展示认证信息、对话正文或工具参数。</div>' +
      panel('数据来源', keyValues(snapshot.sources || {})) + panel('覆盖范围', keyValues(snapshot.coverage || {})) +
      panel('环境与功能清单', keyValues(snapshot.environment || {})) + panel('口径与限制', '<ul>' + (snapshot.warnings || []).map(function (warning) { return '<li>' + escapeHtml(warning) + '</li>'; }).join('') + '</ul><p class="cx-muted">本地 schema 并非长期稳定 API。历史记录不能证明当前线程仍在运行；额度记录为账号级日志快照。</p>');
  }

  function options(values, current, allLabel) {
    return '<option value="">' + allLabel + '</option>' + values.map(function (item) { return '<option value="' + escapeHtml(item[0]) + '" ' + (current === item[0] ? 'selected' : '') + '>' + escapeHtml(item[1]) + '</option>'; }).join('');
  }
  function render() {
    // render 是看板唯一输出网关（平台切换/筛选/翻页都经此），先按当前全局
    // 快照重建索引，保证 shell 异步注水后的首次渲染即用最新数据。
    rebuildIndex();
    var projects = (snapshot.projects || []).map(function (project) { return [project.id, project.name || project.id]; });
    var models = Array.from(new Set(usage.map(function (record) { return record.model; }).concat(threads.map(function (thread) { return thread.model; })).filter(Boolean))).sort().map(function (model) { return [model, model]; });
    // 来源目录变更或模型记录消失时，移除旧筛选，避免界面显示“全部”却仍按旧值过滤。
    if (view.project && !projects.some(function (item) { return item[0] === view.project; })) view.project = '';
    if (view.model && !models.some(function (item) { return item[0] === view.model; })) view.model = '';
    // 项目详情指向的项目消失（来源变化）时清除选择，避免打开一个不存在的“幽灵项目”。
    if (view.projectId === UNASSIGNED_KEY) {
      if (!threads.some(function (thread) { return !thread.project_id; })) view.projectId = '';
    } else if (view.projectId && !projectById.has(view.projectId)) view.projectId = '';
    savePreference('gauge.codex.view.v1', view);
    var selected = selection();
    var labels = ['总览', '用量分析', '项目', '任务', 'Agent 协作', '工具与异常', '数据与环境'];
    var coverage = snapshot.coverage || {};
    var warnings = (snapshot.warnings || []).slice(0, 3).map(escapeHtml).join('；');
    var page = { overview: overview, usage: usagePage, projects: projectsPage, tasks: tasksPage, agents: agentsPage, tools: toolsPage, environment: environmentPage }[view.tab];
    app.innerHTML = '<div class="cx-toolbar"><label>时间<select data-filter="days" aria-label="时间">' + ['7', '14', '30', 'all'].map(function (days) { return '<option value="' + days + '" ' + (view.days === days ? 'selected' : '') + '>' + (days === 'all' ? '全部' : '近' + days + '天') + '</option>'; }).join('') + '</select></label><label>项目<select data-filter="project" aria-label="项目">' + options(projects, view.project, '全部项目') + '</select></label><label>模型<select data-filter="model" aria-label="模型">' + options(models, view.model, '全部模型') + '</select></label><input data-filter="query" aria-label="搜索任务" placeholder="任务标题或工作目录" value="' + escapeHtml(view.query) + '"><button class="cx-button" data-refresh>刷新</button><span class="cx-muted">更新于 ' + escapeHtml(snapshot.generated_at || '尚未读取') + '</span></div>' +
      '<div class="cx-tabs" role="tablist">' + allowedTabs.map(function (tab, index) { return '<button class="cx-button" role="tab" aria-selected="' + (view.tab === tab) + '" data-tab="' + tab + '">' + labels[index] + '</button>'; }).join('') + '</div>' +
      '<div class="cx-note" data-error="' + (snapshot.status === 'error' || snapshot.status === 'unavailable') + '">' +
      (snapshot.available === false ? 'Codex 数据暂不可用。请检查本机数据目录后刷新。' : coverage.complete === false ? '历史索引处理中：已处理 ' + formatNumber(coverage.processed_files) + ' / ' + formatNumber(coverage.total_files) + ' 个文件，当前指标仅覆盖已读取部分。' : '本地历史记录 · Token 分项按已读取响应统计。') + (warnings ? '<br>' + warnings : '') + (view.model ? '<br>模型筛选下，回合与工具只纳入有该模型响应记录的回合。' : '') + '</div>' +
      (snapshot.available === false && view.tab !== 'environment' ? '<div class="cx-empty">来源不可用，暂不展示统计值。可在“数据与环境”查看原因。</div>' : page(selected));
  }
  app.addEventListener('change', function (event) {
    var key = event.target.getAttribute('data-filter');
    if (!['days', 'project', 'model', 'query'].includes(key)) return;
    view[key] = event.target.value; view.page = 0; render();
  });
  app.addEventListener('click', function (event) {
    var target = event.target.closest('button'); if (!target) return;
    if (target.hasAttribute('data-tab')) { view.tab = target.getAttribute('data-tab'); render(); }
    else if (target.hasAttribute('data-task')) { view.tab = 'tasks'; view.taskId = target.getAttribute('data-task'); render(); }
    else if (target.hasAttribute('data-project')) {
      var chosen = target.getAttribute('data-project');
      view.projectId = view.projectId === chosen ? '' : chosen; // 再次点击已选项目 = 收回详情
      render();
    }
    else if (target.hasAttribute('data-page')) { view.page += Number(target.getAttribute('data-page')); render(); }
    else if (target.hasAttribute('data-refresh')) {
      var api = bridge();
      if (api && api.refresh_now) Promise.resolve(api.refresh_now()).catch(function () { platformStatus.textContent = '刷新请求未成功'; });
      else platformStatus.textContent = '这是 HTML 快照，请运行刷新数据后重新打开';
    }
  });
  function desktopReady() {
    var api = bridge();
    if (api && api.get_platform) Promise.resolve(api.get_platform()).then(applyPlatform).catch(function () { platformStatus.textContent = '桌面平台状态暂不可用'; });
  }
  document.addEventListener('pywebviewready', desktopReady);
  applyPlatform({ platform: readPreference('gauge.platform', 'zcode') === 'codex' ? 'codex' : 'zcode' });
  desktopReady();
}());
