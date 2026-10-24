// AI 板块（D-075）：显示不显示只听 api（utils/ai-access.js）、底栏第 5 格、列表行和详情的入口、搜索"只看能解读的"、
// AI 首页的各种情况、联系客服和使用帮助。AI 关着时任何页面都没有 AI 字样，直接打开 AI 首页回首页、不写"暂未开放"。
// 依赖都用替身，不读取 legal.js 或本机配置。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { render, textOf, findAll, hasClass, tapsTo } = require('./wxml-render.js');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const regions = require('../utils/regions.js');
const context = require('../utils/tool-context.js');
const advisor = require('../utils/advisor.js');
const interpret = require('../utils/ai-interpret.js');
const reasons = require('../utils/ai-reasons.js');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 6; i += 1) await flush(); };
const plain = (value) => JSON.parse(JSON.stringify(value));
const HOUR = 3600 * 1000;
const NOT_OPEN = /暂未开放|即将开放|敬请期待|内测|测试中/;

function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, Math, Promise, Set, Map, setTimeout, clearTimeout, console,
    require(name) {
      if (name === '../../utils/regions.js') return regions;
      if (!Object.hasOwn(deps, name) && /daywatch\.js$/.test(name)) return require('./helpers/day-off.js');
      assert.ok(Object.hasOwn(deps, name), `${file} 缺少依赖 ${name}`);
      return deps[name];
    },
    Page(d) { def = d; }, Component(d) { def = d; },
    getApp: () => ({ globalData: { statusBarHeight: 20, navHeight: 44, menuWidth: 96 } }),
    Date,
    ...globals,
  }, { filename: file });
  if (!def) return module.exports;
  const instance = { ...def, ...(def.methods || {}), data: plain(def.data || {}), setData(values) { Object.assign(this.data, plain(values)); } };
  if (def.properties) instance.properties = Object.fromEntries(Object.entries(def.properties).map(([key, value]) => [key, value.value]));
  return instance;
}

function memoryWx(extra = {}) {
  const saved = new Map();
  return Object.assign({
    saved,
    getStorageSync: (key) => saved.get(key),
    setStorageSync: (key, value) => saved.set(key, plain(value)),
    removeStorageSync: (key) => saved.delete(key),
  }, extra);
}

const ON = (extra = {}) => ({
  data_tools: { enabled: false, features: [], remaining: {} },
  ai_advisor: { enabled: true, features: ['interpret', 'clause'], remaining: { interpret: 5, clause: 4 }, available: true, siteFull: false, disclosure: null, ...extra },
  reason: '',
});
const OFF = (reason = 'off', extra = {}) => ({ data_tools: { enabled: false, features: [], remaining: {} }, ai_advisor: { enabled: false, features: [], remaining: {} }, reason, ...extra });
const NOT_FOUND = () => Promise.reject(Object.assign(new Error('Not Found'), { status: 404 }));
const NETWORK = () => Promise.reject(Object.assign(new Error('网络不太好，请稍后再试'), { retryable: true }));

// 真的 utils/ai-access.js，接口和时钟用替身
function accessWith(options = {}) {
  const calls = { mode: 0, caps: 0 };
  const answer = (value) => (typeof value === 'function' ? value() : Promise.resolve(value));
  const api = {
    isLive: () => options.live !== false,
    advisorMode: () => { calls.mode += 1; return answer(options.mode); },
    advisorCapabilities: (opts) => { calls.caps += 1; calls.refresh = opts && opts.refresh; return answer(options.caps); },
    forgetCapabilities: () => { calls.forgot = (calls.forgot || 0) + 1; },
  };
  const wx = options.wx || memoryWx();
  const clock = options.clock || { now: 1000 * HOUR };
  const raw = load('utils/ai-access.js', { './api.js': api }, { wx, Date: { now: () => clock.now } });
  // 模块在另一个运行环境里建的对象原型不同：比较前转成这边的普通对象
  const access = Object.assign({}, raw, {
    raw,
    current: () => plain(raw.current()),
    ensure: (...args) => raw.ensure(...args).then(plain),
    refresh: (...args) => raw.refresh(...args).then(plain),
    lastDoc: () => { const value = raw.lastDoc(); return value ? plain(value) : value; },
    subscribe: (fn) => raw.subscribe((state) => fn(plain(state))),
  });
  return { access, calls, wx, clock };
}

// 页面、组件测试用的"AI 板块状态"替身：set 改状态并通知订阅的页面
function control(initial) {
  let state = Object.assign({}, initial);
  const listeners = new Set();
  const asks = { ensure: 0, refresh: 0, reconcile: 0, recheck: 0 };
  const stored = { doc: null };
  return {
    asks, stored, listeners,
    current: () => Object.assign({}, state),
    subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
    ensure: () => { asks.ensure += 1; return Promise.resolve(Object.assign({}, state)); },
    refresh: () => { asks.refresh += 1; return Promise.resolve(Object.assign({}, state)); },
    recheck: () => { asks.recheck += 1; return Promise.resolve(Object.assign({}, state)); },
    reconcile() { asks.reconcile += 1; },
    canOpen: (value = state) => value.visible === true && value.confirmed === true && value.available !== false,
    clear() {}, rememberDoc(doc) { stored.doc = doc; }, lastDoc: () => stored.last || null,
    forgetDoc() { stored.last = null; stored.forgot = (stored.forgot || 0) + 1; },
    set(next) { state = Object.assign({}, state, next); listeners.forEach((fn) => fn(Object.assign({}, state))); },
  };
}
const OPEN = { visible: true, confirmed: true, available: true };
const CLOSED = { visible: false, confirmed: true, available: false };

test('显示不显示只听 api：首发版关着不显示、擦掉上次的记录；对所有人开放只问开放状态；白名单阶段再看能力接口；演示数据不请求', async () => {
  // 首发版：开放状态说没有，能力接口说关着
  let h = accessWith({ mode: { open: false, available: false }, caps: OFF() });
  h.wx.saved.set(h.access.CACHE_KEY, { at: h.clock.now - HOUR }); // 上次是显示的
  assert.deepEqual(h.access.current(), { visible: true, confirmed: false, available: true }, '启动时先按上次的画');
  assert.equal(h.access.canOpen(), false, '后台确认之前不能点');
  const seen = []; h.access.subscribe((state) => seen.push(state));
  assert.deepEqual(await h.access.ensure(), CLOSED);
  assert.equal(h.wx.saved.has(h.access.CACHE_KEY), false);
  assert.deepEqual(seen, [CLOSED]);
  assert.equal(h.calls.refresh, true, '能力接口要新问，不用这次启动里缓存的');
  // 对所有人开放：不问能力接口（没同意协议的新用户也看得到板块）
  h = accessWith({ mode: { open: true, available: true }, caps: OFF('consent') });
  assert.deepEqual(await h.access.ensure(), OPEN);
  assert.equal(h.calls.caps, 0); assert.equal(h.access.canOpen(), true);
  assert.deepEqual(h.wx.saved.get(h.access.CACHE_KEY), { at: h.clock.now });
  // 对所有人开放，但后台处理不了（admin 心跳没来、两边设置对不上）：显示，按钮不能点
  h = accessWith({ mode: { open: true, available: false } });
  assert.deepEqual(await h.access.ensure(), { visible: true, confirmed: true, available: false }); assert.equal(h.access.canOpen(), false);
  // 白名单阶段：白名单里的显示；后台处理不了时不能点
  h = accessWith({ mode: { open: false, available: false }, caps: ON() });
  assert.deepEqual(await h.access.ensure(), OPEN); assert.equal(h.calls.caps, 1);
  h = accessWith({ mode: { open: false, available: false }, caps: ON({ available: false }) });
  assert.deepEqual(await h.access.ensure(), { visible: true, confirmed: true, available: false });
  // 不在白名单、没同意协议：不显示
  for (const reason of ['off', 'consent']) {
    h = accessWith({ mode: { open: false, available: false }, caps: OFF(reason) });
    assert.deepEqual(await h.access.ensure(), CLOSED, reason);
  }
  // 旧后台没有开放状态接口（404）：往下看白名单
  h = accessWith({ mode: NOT_FOUND, caps: ON() });
  assert.deepEqual(await h.access.ensure(), OPEN);
  // 演示数据：不请求、不显示
  h = accessWith({ live: false, mode: { open: true }, caps: ON() });
  assert.deepEqual(await h.access.ensure(), CLOSED); assert.deepEqual([h.calls.mode, h.calls.caps], [0, 0]);
});

test('连不上时不下结论：上次记的"显示"照样先画、但不能点；15 秒内不重复问，强制重问不受限；记录 24 小时作废；10 分钟内不再问', async () => {
  const clock = { now: 2000 * HOUR };
  const wx = memoryWx(); wx.saved.set('youjuAiVisibleV1', { at: clock.now - HOUR });
  const h = accessWith({ wx, clock, mode: NETWORK, caps: ON() });
  assert.equal(h.access.current().visible, true);
  assert.deepEqual(await h.access.ensure(), { visible: true, confirmed: false, available: true });
  assert.equal(h.access.canOpen(), false); assert.deepEqual([h.calls.mode, h.calls.caps], [1, 0]);
  await h.access.ensure(); assert.equal(h.calls.mode, 1, '15 秒内不重复问');
  clock.now += h.access.RETRY_AFTER; await h.access.ensure(); assert.equal(h.calls.mode, 2);
  await h.access.refresh(); assert.equal(h.calls.mode, 3, '"重试"马上再问');
  assert.equal(wx.saved.has('youjuAiVisibleV1'), true, '连不上时不擦掉记录');
  // 能力接口那一步连不上（reason offline）也不下结论
  const h2 = accessWith({ mode: { open: false, available: false }, caps: OFF('offline') });
  assert.equal((await h2.access.ensure()).confirmed, false);
  // 24 小时前记的、时间在将来的（手机改过时间）都不用
  for (const at of [clock.now - 24 * HOUR, clock.now + 60000, 'x']) {
    const stale = memoryWx(); stale.saved.set('youjuAiVisibleV1', { at });
    assert.equal(accessWith({ wx: stale, clock, mode: NETWORK }).access.current().visible, false, String(at));
  }
  // 同时只问一次；问到以后 10 分钟内页面显示不再问
  let release;
  let first = true;
  const h3 = accessWith({ mode: () => { if (first) { first = false; return new Promise((done) => { release = done; }); } return Promise.resolve({ open: true, available: true }); } });
  const a = h3.access.raw.ensure(); const b = h3.access.raw.ensure();
  assert.equal(a, b); assert.equal(h3.calls.mode, 1);
  release({ open: true, available: true }); assert.deepEqual(plain(await a), OPEN);
  await h3.access.ensure(); assert.equal(h3.calls.mode, 1);
  h3.clock.now += h3.access.RECHECK; await h3.access.ensure(); assert.equal(h3.calls.mode, 2);
});

test('注销时擦掉：显示状态、本机记录和"刚才看的通知"都清掉，各页面收起；刚才看的通知只记编号、标题、机关、日期，7 天后不显示', async () => {
  const h = accessWith({ mode: { open: true, available: true } });
  await h.access.ensure();
  h.access.rememberDoc({ id: 42, title: '关于组织申报的通知', issuer: '科技厅', date: '2026-10-16', body: { text: '正文不记' }, ai: { interpret: true } });
  assert.deepEqual(h.wx.saved.get(h.access.LAST_DOC_KEY), { id: '42', title: '关于组织申报的通知', issuer: '科技厅', date: '2026-10-16', at: h.clock.now });
  assert.deepEqual(h.access.lastDoc(), { id: '42', title: '关于组织申报的通知', issuer: '科技厅', date: '2026-10-16' });
  h.clock.now += h.access.LAST_DOC_FOR + 1; assert.equal(h.access.lastDoc(), null);
  h.access.rememberDoc({ id: 43, title: '另一份' }); assert.equal(h.access.lastDoc().id, '43');
  const seen = []; h.access.subscribe((state) => seen.push(state));
  h.access.clear();
  assert.deepEqual(seen, [{ visible: false, confirmed: false, available: false }]);
  assert.equal(h.wx.saved.has(h.access.CACHE_KEY), false); assert.equal(h.wx.saved.has(h.access.LAST_DOC_KEY), false);
  // 擦掉以后再问一次后台
  await h.access.ensure(); assert.equal(h.calls.mode, 2);
  // 没有编号或标题的不记
  const empty = accessWith();
  empty.access.rememberDoc({ id: '', title: '标题' }); empty.access.rememberDoc({ id: 1 }); empty.access.rememberDoc(null);
  assert.equal(empty.wx.saved.size, 0);
  // 注销（utils/consent.js 的 withdraw）调用 clear：见 data-idempotency.test.js
  assert.match(read('utils/consent.js'), /aiAccess\.clear\(\);/);
});

function tabBar(state, final = true) {
  const access = control(state);
  const switched = [];
  const bar = load('custom-tab-bar/index.js', { '../utils/consent.js': { loginOpen: () => final }, '../utils/ai-access.js': access },
    { wx: { switchTab: (o) => switched.push(o.url) } });
  bar.lifetimes.attached.call(bar);
  return { bar, access, switched, items: (selected) => findAll(render('custom-tab-bar/index.wxml', { ...bar.data, selected }), hasClass('item')) };
}

test('底栏：AI 对这个人开着时第 5 格"AI"画在中间（app.json 序号 4，其余四页的序号不变）；关着时和原来一样；中途关了立刻收起', async () => {
  const app = JSON.parse(read('app.json'));
  assert.equal(app.tabBar.list.map((t) => t.pagePath).indexOf('pages/ai/index'), 4);
  for (const route of ['pages/ai/index', 'pages/help/index']) assert.ok(app.pages.includes(route), route);
  for (const final of [true, false]) {
    // 启动时按上次记的先画（还没确认）：不等后台回话，免得 4 格变 5 格
    const { bar, access, switched, items } = tabBar({ visible: true, confirmed: false, available: true }, final);
    assert.ok(access.asks.ensure >= 1, '底栏出现时问一次后台');
    const names = items(4).map((n) => textOf(n).trim());
    assert.deepEqual(names, final ? ['首页', '关注', 'AI', '文件', '我的'] : ['首页', 'AI', '文件', '我的']);
    const root = render('custom-tab-bar/index.wxml', { ...bar.data, selected: 4 });
    assert.equal(findAll(root, hasClass('bar'))[0].attrs.style, `grid-template-columns: repeat(${names.length}, 1fr)`);
    assert.deepEqual(findAll(root, hasClass('item--on')).map((n) => textOf(n).trim()), ['AI']);
    assert.equal(findAll(root, hasClass('spark--on')).length, 1, '选中时实心');
    const other = render('custom-tab-bar/index.wxml', { ...bar.data, selected: 0 });
    assert.equal(findAll(other, hasClass('spark')).length, 1); assert.equal(findAll(other, hasClass('spark--on')).length, 0, '没选中时空心');
    const ai = items(0).find((n) => textOf(n).trim() === 'AI');
    bar.switchTab({ currentTarget: { dataset: { path: ai.attrs['data-path'] } } });
    assert.deepEqual(switched, ['/pages/ai/index']);
    // 后台说关了（或不在白名单）：立刻收起
    access.set(CLOSED);
    assert.deepEqual(items(0).map((n) => textOf(n).trim()), final ? ['首页', '关注', '文件', '我的'] : ['首页', '文件', '我的']);
    bar.lifetimes.detached.call(bar); assert.equal(access.listeners.size, 0);
  }
  const { bar } = tabBar(CLOSED);
  assert.deepEqual(bar.data.list.map((t) => t.text), ['首页', '关注', '文件', '我的']);
  // 样式画的四角星：线宽和字体图标接近；微信 WebView 要带 -webkit- 前缀
  const css = read('custom-tab-bar/index.wxss');
  assert.match(css, /-webkit-clip-path: polygon/); assert.match(css, /\.spark--on \.spark-in \{ display: none; \}/);
});

function aiButton(props, state) {
  const access = control(state);
  const navigations = []; const events = [];
  const view = load('components/ai-button/index.js', { '../../utils/ai-access.js': access, '../../utils/tool-context.js': context },
    { wx: { navigateTo: (o) => navigations.push(o) } });
  Object.assign(view.properties, props);
  view.triggerEvent = (name, detail) => events.push([name, plain(detail)]);
  view.lifetimes.attached.call(view);
  return { view, access, navigations, events, text: () => textOf(render('components/ai-button/index.wxml', { ...view.data, ...view.properties })).trim() };
}

test('列表行的"AI 解读"：这一行能解读、AI 对这个人开着、后台确认过且能处理才显示；点了直接进解读页，连点只进一次；勾选导出时不显示', async () => {
  const eligible = { docId: '42', docTitle: '关于组织申报的通知 & 附件', ai: { interpret: true, clause: true, reason: null } };
  const shown = aiButton(eligible, OPEN);
  assert.equal(shown.text(), 'AI 解读'); assert.deepEqual(shown.events, [['toggle', { shown: true }]]);
  shown.view.open(); shown.view.open();
  assert.equal(shown.navigations.length, 1);
  assert.equal(shown.navigations[0].url, `/pages/advisor-interpret/index?docId=42&title=${encodeURIComponent('关于组织申报的通知 & 附件')}`);
  shown.navigations[0].complete(); shown.view.open(); assert.equal(shown.navigations.length, 2);
  // 后台说关了：按钮收起，告诉列表行
  shown.access.set(CLOSED); assert.equal(shown.text(), ''); assert.deepEqual(shown.events.at(-1), ['toggle', { shown: false }]);
  const hiddenCases = [
    [{ ...eligible, ai: null }, OPEN],
    [{ ...eligible, ai: { interpret: false, clause: false, reason: 'list' } }, OPEN],
    [{ ...eligible, ai: { interpret: 'true' } }, OPEN],
    [{ ...eligible, selecting: true }, OPEN],
    [{ ...eligible, docId: '' }, OPEN],
    [eligible, { visible: true, confirmed: false, available: true }],
    [eligible, { visible: true, confirmed: true, available: false }],
    [eligible, CLOSED],
  ];
  for (const [props, state] of hiddenCases) {
    const button = aiButton(props, state);
    assert.equal(button.text(), '', JSON.stringify([props, state])); assert.deepEqual(button.events, []);
    button.view.open(); assert.equal(button.navigations.length, 0);
  }
  // 列表行：按钮放在"复制原文链接"后面；勾选导出时隐藏；有按钮时发文机关另起一行腾地方
  const rowWxml = read('components/doc-row/index.wxml');
  assert.match(rowWxml, /<ai-button doc-id="\{\{doc\.id\}\}" doc-title="\{\{doc\.title\}\}" ai="\{\{doc\.ai\}\}" selecting="\{\{selecting\}\}" bind:toggle="aiToggle" \/>/);
  assert.ok(rowWxml.indexOf('<ai-button') > rowWxml.indexOf('查看详情'));
  const row = load('components/doc-row/index.js', { '../../utils/format.js': format, '../../utils/copy.js': { copyLink() {} }, '../../utils/repost.js': require('../utils/repost.js') });
  row.data.doc = { id: '42', title: '通知', kind: '申报通知', issuer: '山东省科学技术厅', date: '2026-10-16', ai: { interpret: true } };
  row.observers['doc, q, today'].call(row, row.data.doc, '');
  assert.equal(row.data.issuerLong, false, '没按钮时短的发文机关和日期同一行');
  row.aiToggle({ detail: { shown: true } }); row.observers.aiShown.call(row, true);
  assert.equal(row.data.issuerLong, true);
  row.aiToggle({ detail: { shown: false } }); row.observers.aiShown.call(row, false);
  assert.equal(row.data.issuerLong, false);
  assert.match(read('components/doc-row/index.json'), /"ai-button": "\/components\/ai-button\/index"/);
});

test('首页"7 天内截止"和截止页也有同一个按钮：按日期列的、选中某天的、可能 7 天内截止的都有；已截止的那组没有', async () => {
  const QUOTE = '请于2026年10月21日前提交材料。';
  const today = format.todayISO();
  const plus = (days) => { const d = new Date(`${today}T00:00:00+08:00`); d.setUTCDate(d.getUTCDate() + days); return new Date(d.getTime() + 8 * HOUR).toISOString().slice(0, 10); };
  const res = {
    stats: { week_total: 1, apply: 1, lists: 0, other: 0, provinces: 28, sources: 70 }, items: [],
    dueSoon: [{ id: '1', title: '甲', label: '企业申报截止', date: plus(2), time: '17:00', source: 'verified', ai: { interpret: true, clause: true, reason: null } }],
    dueSoonUnverifiedTotal: 0,
  };
  const page = load('pages/home/index.js', {
    '../../utils/boost.js': require('../utils/boost.js'),
    '../../utils/api.js': { home: () => Promise.resolve(plain(res)) }, '../../utils/format.js': format, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/consent.js': { loginOpen: () => false },
  }, { wx: { navigateTo() {} } });
  page.onLoad(); await settle();
  assert.deepEqual(page.data.soon.map((s) => [s.id, s.ai]), [['1', { interpret: true, clause: true, reason: null }]]);
  const home = render('pages/home/index.wxml', { ...page.data, loading: false });
  const soon = findAll(home, hasClass('soon'))[0];
  assert.deepEqual(findAll(soon, (n) => n.tag === 'ai-button').map((n) => n.attrs['doc-id']), ['1']);
  assert.match(read('pages/home/index.json'), /"ai-button": "\/components\/ai-button\/index"/);
  // 截止页：每一行都带接口给的 ai；已截止的不放按钮
  const report = read('pages/report/index.wxml');
  assert.equal((read('pages/report/index.js').match(/ai: d\.ai \|\| null,/g) || []).length, 2);
  const rows = render('pages/report/index.wxml', {
    statusBarHeight: 20, navHeight: 44, tabs: [], summary: {}, provinces: [], depts: [], cityRegion: [], tab: 'all', loading: false, error: '',
    rows: [
      { key: 'a', docId: '7', title: '未截止', kindText: '申报截止', dateText: '10月21日', week: '周三', days: 3, tone: 'normal', ai: { interpret: true } },
      { key: 'b', docId: '8', title: '日期未核对', kindText: '申报截止', dateText: '', week: '', days: null, ai: { interpret: true } },
      { key: 'c', docId: '9', title: '按截止时刻刚过', kindText: '申报截止', dateText: '10月17日', week: '周五', days: -1, tone: 'past', ai: { interpret: true } },
    ],
    past: [{ key: 'p', docId: '10', title: '已截止', dateText: '10月1日', week: '周三' }], pastOpen: true,
    soon: { total: 1, items: [{ id: '11', title: '可能 7 天内', issuer: '科技厅', ai: { interpret: true } }] }, prefilled: true,
    cells: [], month: 10, year: 2026, total: 3,
  });
  assert.deepEqual(findAll(rows, (n) => n.tag === 'ai-button').map((n) => n.attrs['doc-id']), ['7', '8', '11']);
  assert.equal(findAll(findAll(rows, hasClass('past'))[0] || { children: [] }, (n) => n.tag === 'ai-button').length, 0);
  assert.ok(report.includes('"ai-button": ') === false && /"ai-button": "\/components\/ai-button\/index"/.test(read('pages/report/index.json')));
});

function searchPage(state, { respond = () => ({ total: 0, total_all: 0, items: [] }) } = {}) {
  const calls = [];
  const access = control(state);
  const page = load('pages/search/index.js', {
    '../../utils/api.js': { search: (p) => { calls.push(p); return Promise.resolve(respond(p)); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} }, '../../utils/exporter.js': {},
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/subscribe.js': { remindOpen: () => true }, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
    '../../utils/ai-access.js': access,
  }, { wx: {} });
  return { page, calls, access };
}

test('搜索"只看能 AI 解读的"：从 AI 首页带 ai=1 进来只管这一次查询，可以去掉；不进关注和转发；AI 对这个人没开着时不生效、页面没有这几个字', async () => {
  const query = { kind: encodeURIComponent('申报通知'), time: encodeURIComponent('近 7 天'), ai: '1' };
  const { page, calls, access } = searchPage(OPEN);
  page.onLoad(query); await settle();
  assert.equal(page.data.aiOnly, true);
  assert.equal(calls.at(-1).ai, 1); assert.equal(calls.at(-1).kind, '申报通知'); assert.equal(calls.at(-1).time, '近 7 天');
  assert.equal(page.conditions().ai, undefined, '关注保存的条件里没有');
  assert.doesNotMatch(page.onShareAppMessage().path, /ai=/, '转发路径里没有');
  let root = render('pages/search/index.wxml', page.data);
  assert.equal(findAll(root, (n) => n.tag === 'ai-only').length, 2, '结果上面一条、没有结果时一个"去掉"');
  page.removeAi(); await settle();
  assert.equal(page.data.aiOnly, false); assert.equal(calls.at(-1).ai, undefined);
  assert.equal(access.listeners.size, 0, '去掉以后不再跟着变');
  // 后台确认 AI 没开着：去掉这一项重新查
  const later = searchPage(OPEN);
  later.page.onLoad(query); await settle();
  later.access.set(CLOSED); await settle();
  assert.equal(later.page.data.aiOnly, false); assert.equal(later.calls.at(-1).ai, undefined);
  // AI 关着时带着 ai=1 打开（旧链接）：不生效，页面上没有这一条
  const closed = searchPage(CLOSED);
  closed.page.onLoad(query); await settle();
  assert.equal(closed.page.data.aiOnly, false); assert.equal(closed.calls.at(-1).ai, undefined);
  root = render('pages/search/index.wxml', closed.page.data);
  assert.equal(findAll(root, (n) => n.tag === 'ai-only').length, 0);
  // 文字都在组件里
  assert.match(read('components/ai-only/index.wxml'), /只看能 AI 解读的通知/);
  assert.doesNotMatch(read('pages/search/index.wxml').replace(/<!--[\s\S]*?-->/g, ''), /\bAI\b/);
});

function hint(doc, state) {
  const access = control(state); const events = [];
  const view = load('components/ai-hint/index.js', { '../../utils/ai-access.js': access });
  if (doc) Object.assign(view.properties, { docId: doc.id, docTitle: doc.title, issuer: doc.issuer, date: doc.date, ai: doc.ai });
  view.triggerEvent = (name) => events.push(name);
  view.lifetimes.attached.call(view);
  return { view, access, events, text: () => textOf(render('components/ai-hint/index.wxml', view.data)).trim() };
}

test('详情标题卡里的"这份通知可以 AI 解读 ↓"：能解读、AI 对这个人开着才显示，点了滚到"要点"下面的卡片（卡片位置不动）；显示时记下这份给 AI 首页用', async () => {
  const doc = { id: '42', title: '通知', issuer: '科技厅', date: '2026-10-16', ai: { interpret: true, clause: true, reason: null } };
  const shown = hint(doc, OPEN);
  assert.equal(shown.text(), '这份通知可以 AI 解读 ↓');
  assert.deepEqual(plain(shown.access.stored.doc), { id: '42', title: '通知', issuer: '科技厅', date: '2026-10-16' }, '只记这几项');
  shown.view.jump(); assert.deepEqual(shown.events, ['jump']);
  shown.access.set(CLOSED); assert.equal(shown.text(), '');
  for (const [value, state] of [[{ ...doc, ai: { interpret: false, reason: 'list' } }, OPEN], [{ ...doc, ai: null }, OPEN], [doc, { visible: true, confirmed: false, available: true }], [doc, CLOSED], [null, OPEN]]) {
    const other = hint(value, state);
    assert.equal(other.text(), ''); assert.equal(other.access.stored.doc, null);
    other.view.jump(); assert.deepEqual(other.events, []);
  }
  const wxml = read('pages/detail/index.wxml');
  assert.match(wxml, /<view class="pub">[^\n]*<\/view>\n[^\n]*\n\s*<ai-hint doc-id="\{\{doc\.id\}\}" doc-title="\{\{doc\.title\}\}" issuer="\{\{doc\.issuer\}\}" date="\{\{doc\.date\}\}" ai="\{\{doc\.ai\}\}" bind:jump="toAi" \/>/);
  assert.match(wxml, /<view id="ai-entry"><ai-entry /); assert.match(wxml, /id="attachments" class="card"/);
  assert.ok(wxml.indexOf('<view id="ai-entry">') > wxml.indexOf('要点'), '卡片仍在"要点"下面');
  const scrolls = [];
  const page = load('pages/detail/index.js', {
    '../../utils/api.js': { watchStartup() {} }, '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': {},
    '../../utils/reminders.js': {}, '../../utils/consent.js': {}, '../../utils/exporter.js': {}, '../../utils/daywatch.js': {},
    '../../utils/attachments.js': {}, '../../utils/repost.js': {},
  }, { wx: { pageScrollTo: (o) => scrolls.push(o.selector) } });
  page.toAi(); page.toAttachments();
  assert.deepEqual(scrolls, ['#ai-entry', '#attachments']);
});

function hubPage({ state = OPEN, caps = ON(), search = () => Promise.resolve({ items: [] }), tasks = () => Promise.resolve({ items: [] }), last = null, consentOk = true,
  detail = (id) => Promise.resolve({ id, title: '刚才看的那份', ai: { interpret: true, clause: true, reason: null } }), realAccess = null, extraApi = {}, daywatch = null, now = null } = {}) {
  // realAccess：用真的 utils/ai-access.js（accessWith 建的），看缓存多久再问
  const access = realAccess || control(state); if (!realAccess) access.stored.last = last;
  const calls = { search: [], caps: 0, consent: 0 };
  // 页面里的定时器（我的 AI 记录过一会儿再问）不真的等：记下来，测试自己调 runTimers()
  const timers = new Map(); let timerSeq = 0;
  const runTimers = () => { const due = [...timers.values()]; timers.clear(); due.forEach((t) => t.fn()); return due.map((t) => t.ms); };
  const switched = []; const navigations = []; let selected = null;
  const page = load('pages/ai/index.js', {
    '../../utils/api.js': {
      advisorCapabilities: (opts) => { calls.caps += 1; calls.refresh = opts && opts.refresh; return typeof caps === 'function' ? caps() : Promise.resolve(caps); },
      search: (p) => { calls.search.push(p); return search(p); },
      advisorTasks: tasks,
      detail: (id) => { calls.detail = (calls.detail || 0) + 1; return detail(id); },
      ...extraApi,
    },
    '../../utils/ai-access.js': access, '../../utils/advisor.js': advisor, '../../utils/ai-interpret.js': interpret, '../../utils/tool-context.js': context,
    '../../utils/consent.js': { ensureConsent: (opts) => { calls.consent += 1; calls.consentOpts = opts; return Promise.resolve(consentOk); } }, '../../utils/format.js': format,
    '../../utils/daywatch.js': daywatch || require('./helpers/day-off.js'),
  }, { wx: { switchTab: (o) => { switched.push(o.url); if (o.complete) o.complete(); }, navigateTo: (o) => navigations.push(o.url) },
    setTimeout: (fn, ms) => { timerSeq += 1; timers.set(timerSeq, { fn, ms }); return timerSeq; }, clearTimeout: (id) => { timers.delete(id); },
    ...(now ? { Date: { now } } : {}) });
  page.getTabBar = () => ({ setData: (v) => { selected = v.selected; } });
  page.onLoad();
  return { page, access, calls, switched, navigations, timers, runTimers, selected: () => selected, text: () => textOf(render('pages/ai/index.wxml', page.data)) };
}

test('AI 首页：两项能力各写今天还剩几次，下面是刚才看的通知、近 7 天能解读的申报通知（后台筛、最多 5 条）、我的 AI 记录（最近 3 条）和联系客服；不放空白提问框', async () => {
  const doc = (id) => ({ id: String(id), title: `通知${id}`, kind: '申报通知', ai: { interpret: true, clause: true, reason: null } });
  const tasks = [
    { id: 1, kind: 'interpret', status: 'done', docTitle: '甲通知', createdAt: '2026-10-18T02:00:00Z' },
    { id: 2, kind: 'compare', status: 'done', createdAt: '2026-10-18T02:00:00Z' },
    { id: 3, kind: 'clause', status: 'failed', docTitle: '乙通知', createdAt: '2026-10-17T02:00:00Z' },
    { id: 4, kind: 'interpret', status: 'queued', docTitle: '丙通知', createdAt: '2026-10-16T02:00:00Z' },
    { id: 5, kind: 'interpret', status: 'done', docTitle: '丁通知', createdAt: '2026-10-15T02:00:00Z' },
  ];
  const h = hubPage({
    caps: ON({ remaining: { interpret: 3, clause: 0 }, disclosure: { modelLabel: '某模型', filingNo: '备案编号' } }),
    search: () => Promise.resolve({ items: [1, 2, 3, 4, 5, 6].map(doc) }),
    tasks: () => Promise.resolve({ items: tasks }),
    last: { id: '42', title: '刚才看的那份', issuer: '科技厅', date: '2026-10-16' },
  });
  h.page.onShow(); await settle();
  assert.equal(h.selected(), 4);
  assert.deepEqual(plain(h.calls.search), [{ kind: '申报通知', time: '近 7 天', ai: 1, limit: 5, offset: 0, fold: 1 }]);
  assert.equal(h.calls.refresh, true);
  assert.equal(h.page.data.recent.length, 5);
  const text = h.text();
  assert.match(text, /AI 生成 · 以原文为准/);
  assert.match(text, /AI 解读[^]*今天还剩 3 次/);
  assert.match(text, /今天的条款对照 5 次用完了，明天 0 点恢复。/);
  assert.match(text, /去找一份通知/);
  assert.match(text, /刚才看的通知 刚才看的那份 科技厅 · 2026-10-16/);
  assert.match(text, /近 7 天能解读的申报通知/);
  assert.deepEqual(h.page.data.records.map((r) => [r.id, r.kind, r.title]), [['1', 'interpret', '甲通知'], ['3', 'clause', '乙通知'], ['4', 'interpret', '丙通知']]);
  assert.match(text, /用的时候遇到问题？/);
  assert.match(text, /模型：某模型｜备案号：备案编号/);
  assert.doesNotMatch(text, NOT_OPEN); assert.doesNotMatch(text, /免费|购买|开通/);
  const root = render('pages/ai/index.wxml', h.page.data);
  assert.equal(findAll(root, (n) => n.tag === 'input' || n.tag === 'textarea').length, 0, '没有提问框');
  assert.equal(findAll(root, (n) => n.tag === 'doc-row').length, 5);
  assert.equal(findAll(root, (n) => n.tag === 'contact-button').length, 1);
  assert.deepEqual(findAll(root, (n) => n.tag === 'ai-button').map((n) => n.attrs['doc-id']), ['42'], '刚才看的那份直接给按钮');
  // 各个去处
  h.page.findDoc(); h.page.allRecent(); h.page.clauseLast(); h.page.allRecords();
  h.page.openRecord({ currentTarget: { dataset: { id: '3', kind: 'clause' } } }); h.page.openRecord({ currentTarget: { dataset: { id: '1', kind: 'interpret' } } });
  h.page.openDoc({ currentTarget: { dataset: { id: '42' } } }); h.page.help(); h.page.report();
  // 条款对照今天的次数用完了（remaining.clause 为 0）：刚才看的那份不给"条款对照"
  assert.deepEqual(h.navigations, [
    '/pages/search/index?ai=1',
    `/pages/search/index?kind=${encodeURIComponent('申报通知')}&time=${encodeURIComponent('近 7 天')}&ai=1`,
    '/pages/advisor-tasks/index',
    '/pages/advisor-clause/index?taskId=3', '/pages/advisor-interpret/index?taskId=1',
    '/pages/detail/index?id=42', '/pages/help/index', '/pages/complaint/index',
  ]);
  // 说明展开：适用人群、场合、用途、不适用、保存多久
  h.page.toggleScope();
  assert.match(h.text(), /适用人群：.*不适用：.*你填的企业情况保存 7 天，任务和结果保存 30 天。/);
});

test('AI 首页的几种情况：没同意协议先走同意（通知列表照样看）、后台处理不了、全站满了、连不上；AI 没对这个人开着时回首页，不写"暂未开放"', async () => {
  // 没同意协议（全员开放后的新用户）：两项能力换成同意入口；同意后回到这一页刷新
  let h = hubPage({ caps: OFF('consent'), search: () => Promise.resolve({ items: [{ id: '1', title: '通知', kind: '申报通知', ai: { interpret: true } }] }) });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.gate, 'consent');
  assert.match(h.text(), /使用 AI 前，请先阅读并同意《用户协议》和《隐私保护指引》。 阅读并同意/);
  assert.equal(h.page.data.recent.length, 1, '通知列表照样能看');
  h.page.consent(); await settle();
  assert.equal(h.calls.consent, 1); assert.equal(h.calls.caps, 2, '同意后刷新');
  h = hubPage({ caps: OFF('consent', { consentUpdated: true }) }); h.page.onShow(); await settle();
  assert.match(h.text(), /《用户协议》和《隐私保护指引》已更新，请阅读并同意新版后再使用 AI。/);
  // 后台处理不了：写暂时不可用，给联系客服
  h = hubPage({ caps: ON({ available: false }) }); h.page.onShow(); await settle();
  assert.match(h.text(), /AI 暂时不可用，请稍后再试。/);
  assert.equal(findAll(render('pages/ai/index.wxml', h.page.data), (n) => n.tag === 'contact-button').length, 2);
  // 全站当天的上限满了：两项都不给剩几次，写明天恢复
  h = hubPage({ caps: ON({ siteFull: true }) }); h.page.onShow(); await settle();
  assert.match(h.text(), /今天 AI 的总用量满了，明天 0 点恢复；可以先看原文。/); assert.doesNotMatch(h.text(), /今天还剩/);
  // 能力接口连不上：给重试；近 7 天的通知没取到也写明
  h = hubPage({ caps: OFF('offline'), search: () => Promise.reject(new Error('网络')) }); h.page.onShow(); await settle();
  assert.match(h.text(), /暂时连不上服务器，请稍后重试。 重试/); assert.match(h.text(), /近 7 天的通知没有加载出来/);
  // 后台还没回话（启动时只按上次记的画了第 5 格）、而且连不上：写连不上，"重试"马上再问
  h = hubPage({ state: { visible: true, confirmed: false, available: true } }); h.page.onShow(); await settle();
  assert.equal(h.page.data.offline, true); assert.match(h.text(), /暂时连不上服务器，请稍后重试。/);
  assert.equal(h.calls.caps, 0, '没确认之前不问能力接口'); h.page.retry(); await settle(); assert.equal(h.access.asks.refresh, 1);
  // AI 没对这个人开着（关闭、不在白名单、旧链接直接打开）：回首页
  for (const state of [CLOSED, { visible: false, confirmed: true, available: true }]) {
    h = hubPage({ state }); h.page.onShow(); await settle();
    assert.deepEqual(h.switched, ['/pages/home/index']); assert.equal(h.calls.caps, 0); assert.equal(h.calls.search.length, 0);
  }
  // 看着看着后台说关了：回首页
  h = hubPage(); h.page.onShow(); await settle();
  h.access.set(CLOSED); assert.deepEqual(h.switched, ['/pages/home/index']);
  // 能力接口说这个人用不了：再问一次开没开（确认关了就会回首页）
  h = hubPage({ caps: OFF('off') }); h.page.onShow(); await settle(); assert.equal(h.access.asks.refresh, 1);
  for (const file of ['pages/ai/index.wxml', 'pages/ai/index.js', 'pages/help/index.wxml', 'content/help.js', 'components/contact-button/index.wxml', 'components/ai-button/index.wxml', 'components/ai-hint/index.wxml', 'components/ai-only/index.wxml']) {
    // 注释里写着"不写暂未开放"这类规矩，不算；只看用户能看到的文字
    const shown = read(file).replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
    assert.doesNotMatch(shown, NOT_OPEN, file);
  }
});

function contact(props, { platform = 'ios', email = 'help@example.invalid', getDeviceInfo = true } = {}) {
  const wx = getDeviceInfo ? { getDeviceInfo: () => ({ platform }), navigateTo: (o) => wx.urls.push(o.url), urls: [] }
    : { getSystemInfoSync: () => ({ platform }), navigateTo: (o) => wx.urls.push(o.url), urls: [] };
  const view = load('components/contact-button/index.js', { '../../content/legal.js': { operator: { email } } }, { wx });
  Object.assign(view.properties, props);
  view.lifetimes.attached.call(view);
  return { view, wx, root: () => render('components/contact-button/index.wxml', { ...view.data, ...view.properties }) };
}

test('联系客服：微信自带客服按钮，写回复时间和客服管什么；带给客服的只有页面、通知编号、原因代码；在通知页上点带这份通知的卡片；鸿蒙手机换成一行字', () => {
  const c = contact({ from: 'ai_result', docId: '42', docTitle: '关于组织申报的通知', reason: 'AI_UNAVAILABLE' });
  const [button] = findAll(c.root(), (n) => n.tag === 'button');
  assert.equal(button.attrs['open-type'], 'contact');
  assert.equal(button.attrs['session-from'], 'youju|from=ai_result|doc=42|reason=AI_UNAVAILABLE');
  assert.equal(button.attrs['show-message-card'], true);
  assert.equal(button.attrs['send-message-title'], '关于组织申报的通知');
  assert.equal(button.attrs['send-message-path'], '/pages/detail/index?id=42');
  assert.equal(textOf(c.root()).trim(), '联系客服 一般当天或第二天回复 客服只解答小程序怎么用，不代办申报、不判断能不能报');
  // 不认的内容不带（企业名称、长文字、任务编号这类不该进来的）
  const odd = contact({ from: '我的客户 甲公司', docId: 'task-7', reason: 'x'.repeat(80), docTitle: '标题' });
  assert.equal(odd.view.data.sessionFrom, 'youju'); assert.equal(odd.view.data.showCard, false);
  assert.equal(findAll(odd.root(), (n) => n.tag === 'button')[0].attrs['send-message-path'], '');
  // 卡片标题最多 60 个字
  assert.equal(contact({ docId: '1', docTitle: '长'.repeat(80) }).view.data.cardTitle.length, 60);
  // 鸿蒙版微信不支持客服按钮：一行字，给使用帮助和协议里的邮箱
  for (const platform of ['ohos', 'ohos_pc']) {
    for (const getDeviceInfo of [true, false]) {
      const harmony = contact({ from: 'ai_home' }, { platform, getDeviceInfo });
      assert.equal(findAll(harmony.root(), (n) => n.tag === 'button').length, 0);
      assert.match(textOf(harmony.root()), /这台手机暂不能打开微信客服。可以先看使用帮助，或发邮件到 help@example\.invalid。/);
      harmony.view.help(); assert.deepEqual(harmony.wx.urls, ['/pages/help/index']);
    }
  }
  // "我的"页那一行
  const row = contact({ variant: 'row', from: 'me' });
  const [rowButton] = findAll(row.root(), (n) => n.tag === 'button');
  assert.equal(rowButton.attrs['open-type'], 'contact'); assert.equal(rowButton.attrs['session-from'], 'youju|from=me');
  assert.equal(textOf(row.root()).trim(), '联系客服一般当天或第二天回复');
  assert.match(textOf(contact({ variant: 'row' }, { platform: 'ohos' }).root()), /这台手机暂不能打开，看使用帮助/);
  // 微信 style v2 的按钮要 !important 才认宽度和外边距
  assert.match(read('components/contact-button/index.wxss'), /width: 100% !important;/);
  // 放在哪：AI 首页、两个结果页底部和出错时、使用帮助、"我的"
  for (const [file, from] of [['pages/ai/index.wxml', 'ai_home'], ['pages/advisor-interpret/index.wxml', 'ai_result'], ['pages/advisor-clause/index.wxml', 'clause_result'], ['pages/help/index.wxml', 'help'], ['pages/me/index.wxml', 'me']]) {
    assert.match(read(file), new RegExp(`<contact-button[^>]*from="${from}"`), file);
  }
});

test('使用帮助：固定文字；AI 那几条只在 AI 对这个人开着时显示，关闭登录时不写关注；底部是联系客服和投诉举报', async () => {
  const help = require('../content/help.js');
  const page = (state, final = true) => {
    const access = control(state);
    const p = load('pages/help/index.js', { '../../content/help.js': help, '../../utils/ai-access.js': access, '../../utils/consent.js': { loginOpen: () => final } },
      { wx: { setNavigationBarTitle() {}, navigateTo: (o) => p.urls.push(o.url) } });
    p.urls = []; p.onLoad();
    return { p, access, text: () => textOf(render('pages/help/index.wxml', p.data)) };
  };
  const closed = page(CLOSED);
  assert.doesNotMatch(closed.text(), /\bAI\b/);
  assert.match(closed.text(), /有据是什么？.*截止日期怎么看？.*遇到问题找谁？.*还没解决？/);
  assert.match(closed.text(), /怎么知道有新的通知？/);
  assert.doesNotMatch(page(CLOSED, false).text(), /怎么知道有新的通知？/);
  const open = page(OPEN);
  assert.match(open.text(), /AI 解读和条款对照是做什么的？.*为什么有的通知不能 AI 解读？.*AI 每天能用几次？/);
  // 后台说关了：AI 那几条收起
  open.access.set(CLOSED); assert.doesNotMatch(open.text(), /\bAI\b/);
  open.p.report(); assert.deepEqual(open.p.urls, ['/pages/complaint/index']);
  open.p.onUnload(); assert.equal(open.access.listeners.size, 0);
  // 和页面上的说法一致：次数、原因、客服时间
  const all = help.items.map((item) => item.a.join('')).join('');
  assert.match(all, /每天各能用 5 次，第二天 0 点恢复/); assert.match(all, /一般当天或第二天回复/); assert.match(all, /15 个工作日内答复/);
  for (const reason of ['名单', '不是申报通知或政策文件', '完整正文', '2.4 万字']) assert.ok(all.includes(reason), reason);
});

test('不能解读的原因：每种代码一种说法和下一步；没有附件时不给"看附件"；不认的代码写通用的话', () => {
  assert.deepEqual(Object.keys(reasons.REASONS).sort(), ['kind', 'link_gone', 'list', 'no_body', 'source', 'too_long', 'unavailable']);
  assert.deepEqual(reasons.reasonView('no_body', true), { text: '还没有拿到完整正文，正文可能在附件里。', next: 'attachments', nextText: '看附件' });
  assert.deepEqual(reasons.reasonView('no_body', false), { text: '还没有拿到完整正文，正文可能在附件里。', next: 'read', nextText: '站内阅读正文' });
  assert.deepEqual(reasons.reasonView('whatever', false), { text: '这份文件暂不能解读，请查看原文。', next: 'read', nextText: '站内阅读正文' });
  assert.equal(reasons.reasonView('too_long').nextText, '');
  // 后台的原因代码和这里一一对应（backend/src/youju/ai/interpret.py 的 InterpretRefusal）
  const backend = fs.readFileSync(path.join(ROOT, '..', '..', 'backend', 'src', 'youju', 'models.py'), 'utf8');
  const codes = /AI_REASONS = \(([^)]*)\)/.exec(backend);
  if (codes) assert.deepEqual(codes[1].match(/"(\w+)"/g).map((s) => s.slice(1, -1)).sort(), Object.keys(reasons.REASONS).sort());
});

test('解读页、对照页：新提交前后台处理不了或全站满了先拦下、不提交（打开已有结果不拦）；底部和出错时有联系客服；被拒说不可用时重新问 AI 开没开', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    for (const [extra, gate, text] of [[{ available: false }, 'paused', 'AI 暂时不可用，请稍后再试。'], [{ siteFull: true }, 'full', '今天 AI 的总用量满了，明天 0 点恢复；可以先看原文。']]) {
      for (const taskId of ['', '7']) {
        let created = 0; const saved = new Map([[interpret.CONSENT_KEY, true]]);
        const access = control(OPEN);
        const deps = {
          '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON(extra)), createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 1, status: 'queued' }); }, advisorTask: (id) => Promise.resolve({ id, status: 'done', result: {} }) },
          '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': access,
        };
        if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
        const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
        p.onLoad(taskId ? { taskId } : { docId: '42', title: '通知' }); p.onShow(); await settle();
        if (taskId) {
          assert.equal(p.data.aiEnabled, true, `${page} 打开已有结果不拦`);
        } else {
          assert.equal(p.data.gate, gate); assert.equal(p.data.gateText, text); assert.equal(created, 0, `${page} 不提交`);
          const out = textOf(render(`pages/${page}/index.wxml`, p.data));
          assert.ok(out.includes(text));
          assert.equal(findAll(render(`pages/${page}/index.wxml`, p.data), (n) => n.tag === 'contact-button').length, gate === 'paused' ? 1 : 0);
        }
        p.onHide();
      }
    }
  }
  // 提交时后台说处理不了（429 AI_UNAVAILABLE）：页面写暂时不可用，并让 AI 板块重新问一次
  const access = control(OPEN); const saved = new Map([[interpret.CONSENT_KEY, true]]);
  const p = load('pages/advisor-interpret/index.js', {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON()), createAdvisorTask: () => Promise.reject(Object.assign(new Error('AI 暂时不可用，请稍后再试'), { status: 429, code: 'AI_UNAVAILABLE' })) },
    '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': access,
  }, { wx: { setNavigationBarTitle() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
  p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
  assert.equal(p.data.phase, 'failed'); assert.equal(p.data.error, 'AI 暂时不可用，请稍后再试。'); assert.equal(access.asks.refresh, 1);
  const out = render('pages/advisor-interpret/index.wxml', p.data);
  assert.deepEqual(findAll(out, (n) => n.tag === 'contact-button').map((n) => [n.attrs.from, n.attrs['doc-id'], n.attrs.reason]), [['ai_result', '42', 'AI_UNAVAILABLE']]);
  assert.ok(findAll(out, tapsTo('original')).length >= 1, '不能重试时给"看原文"');
});

test('Codex 第 1 轮：注销以前发出去、注销以后才回来的答复不用；擦掉以后再问是新的一次', async () => {
  let release;
  const h = accessWith({ mode: () => new Promise((done) => { release = done; }) });
  const asked = h.access.raw.ensure();
  h.access.clear();
  release({ open: true, available: true }); await asked; await settle();
  assert.deepEqual(h.access.current(), { visible: false, confirmed: false, available: false }, '旧账号的结果不画回去');
  assert.equal(h.wx.saved.has(h.access.CACHE_KEY), false);
  const again = accessWith({ mode: { open: true, available: true } });
  again.access.clear(); assert.deepEqual(await again.access.ensure(), OPEN);
});

test('Codex 第 1 轮：搜索带 ai=1 打开时还没问到后台，确认开着以后再加上这一项重查；用户去掉以后不再加回来', async () => {
  const query = { kind: encodeURIComponent('申报通知'), ai: '1' };
  const { page, calls, access } = searchPage({ visible: false, confirmed: false, available: false });
  page.onLoad(query); await settle();
  assert.equal(page.data.aiOnly, false); assert.equal(calls.at(-1).ai, undefined);
  access.set(OPEN); await settle();
  assert.equal(page.data.aiOnly, true); assert.equal(calls.at(-1).ai, 1);
  page.removeAi(); await settle();
  access.set(Object.assign({}, OPEN)); access.set(CLOSED); access.set(OPEN); await settle();
  assert.equal(page.data.aiOnly, false); assert.equal(calls.at(-1).ai, undefined);
});

test('Codex 第 1 轮：AI 首页确认开着之前页头没有 AI 字样；刚才看的那份先核对还能不能解读，不能了就不显示', async () => {
  // 还没问到（旧链接直接打开、连不上）：页头只有蓝底
  const waiting = hubPage({ state: { visible: false, confirmed: false, available: false } });
  assert.doesNotMatch(waiting.text(), /AI/);
  waiting.page.onShow(); await settle();
  assert.equal(waiting.page.data.offline, true); assert.doesNotMatch(waiting.text(), /AI/);
  const last = { id: '42', title: '刚才看的那份', issuer: '科技厅', date: '2026-10-16' };
  // 还能解读：给按钮（核对之前不给）
  let h = hubPage({ last });
  h.page.onShow();
  assert.equal(h.page.data.lastDoc.ai, null, '核对之前不给按钮');
  await settle();
  assert.deepEqual(plain(h.page.data.lastDoc.ai), { interpret: true, clause: true }); assert.equal(h.calls.detail, 1);
  // 不能解读了、下架了：不显示，也不再记着
  for (const detail of [() => Promise.resolve({ id: '42', title: 'x', ai: { interpret: false, clause: false, reason: 'no_body' } }),
    () => Promise.reject(Object.assign(new Error('该文件已下架'), { status: 404 }))]) {
    h = hubPage({ last, detail }); h.page.onShow(); await settle();
    assert.equal(h.page.data.lastDoc, null); assert.equal(h.access.stored.forgot, 1);
    assert.doesNotMatch(h.text(), /刚才看的通知/);
  }
  // 连不上：照样列出、点标题能看，但不给按钮
  h = hubPage({ last, detail: () => Promise.reject(Object.assign(new Error('网络'), { retryable: true })) }); h.page.onShow(); await settle();
  assert.match(h.text(), /刚才看的通知 刚才看的那份/);
  assert.equal(findAll(render('pages/ai/index.wxml', h.page.data), (n) => n.tag === 'ai-button').length, 0);
  h.page.clauseLast(); assert.deepEqual(h.navigations, []);
});

test('Codex 第 1 轮：工具中心在后台处理不了、全站满了时不给开始的按钮、写明原因；"和我的企业对一下"统一叫"条款对照"', async () => {
  for (const [extra, text] of [[{ available: false }, 'AI 暂时不可用，请稍后再试。'], [{ siteFull: true }, '今天 AI 的总用量满了，明天 0 点恢复；可以先看原文。'], [{}, '']]) {
    const page = load('pages/advisor/index.js', {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON(extra)), advisorTasks: () => Promise.resolve({ items: [] }) },
      '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/boost.js': require('../utils/boost.js'), '../../utils/ai-access.js': control(OPEN),
    }, { wx: { setNavigationBarTitle() {} } });
    page.onLoad({ docId: '42', title: '通知' }); page.onShow(); await settle();
    const root = render('pages/advisor/index.wxml', page.data);
    const out = textOf(root);
    if (text) {
      assert.ok(out.includes(text), text);
      assert.equal(findAll(root, (n) => n.tag === 'button' && /开始解读|开始对照|去找通知/.test(textOf(n))).length, 0);
      assert.doesNotMatch(out, /今天还能用/);
    } else {
      assert.match(out, /开始解读/); assert.match(out, /今天还能用 5 次/);
    }
    page.onHide();
  }
  for (const file of ['pages/advisor/index.wxml', 'pages/advisor-interpret/index.wxml', 'pages/advisor-clause/index.wxml', 'components/ai-entry/index.wxml']) {
    assert.doesNotMatch(read(file), /和我的企业对一下/, file);
  }
});

test('Codex 第 1 轮：任务列表按错误代码写失败原因（不重复"没扣次数"，下面另有一行"这次不算次数"）；排队后被移出白名单的让 AI 板块重新问', async () => {
  const views = advisor.taskViews([
    { id: 1, kind: 'interpret', status: 'failed', error: '今天的 AI 使用次数已满，请明天再试', errorCode: 'AI_DAILY_CAP' },
    { id: 2, kind: 'clause', status: 'failed', error: 'AI 暂时不可用，请稍后再试', errorCode: 'AI_UNAVAILABLE' },
    { id: 3, kind: 'interpret', status: 'failed', error: '功能未开放', errorCode: 'AI_OFF' },
    { id: 4, kind: 'interpret', status: 'failed', error: '通知正文已变化，请重新解读', errorCode: null },
    { id: 5, kind: 'compare', status: 'failed', error: '名单文件读不出来' },
    { id: 6, kind: 'interpret', status: 'done', error: null },
  ]);
  assert.deepEqual(views.map((v) => v.errorText), [
    '今天 AI 的总用量满了，明天 0 点恢复。', 'AI 暂时不可用，请稍后再试。', 'AI 暂时不可用，请稍后再试。', '通知正文已变化，请重新解读', '名单文件读不出来', '',
  ]);
  assert.deepEqual(views.map((v) => v.refundNote), ['这次不算次数', '这次不算次数', '这次不算次数', '这次不算次数', '这次不算次数', '']);
  const list = textOf(render('pages/advisor-tasks/index.wxml', { loading: false, toolsEnabled: true, aiEnabled: true, items: plain(views), busyId: null }));
  assert.doesNotMatch(list, /功能未开放|今天的 AI 使用次数已满/);
  assert.match(list, /今天 AI 的总用量满了，明天 0 点恢复。 这次不算次数/);
  // 解读页看到排队后被移出白名单（AI_OFF）的结果：让 AI 板块重新问一次
  for (const errorCode of ['AI_OFF', 'AI_UNAVAILABLE', 'AI_DAILY_CAP']) {
    const access = control(OPEN); const saved = new Map([[interpret.CONSENT_KEY, true]]);
    const p = load('pages/advisor-interpret/index.js', {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON()), advisorTask: (id) => Promise.resolve({ id, status: 'failed', error: 'x', errorCode }) },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': access,
    }, { wx: { setNavigationBarTitle() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
    p.onLoad({ taskId: '9' }); p.onShow(); await settle();
    assert.equal(p.data.phase, 'failed');
    assert.equal(access.asks.refresh, errorCode === 'AI_DAILY_CAP' ? 0 : 1, errorCode);
  }
});

test('Codex 第 1 轮：从真实的接口回包到页面文字——422 不能解读带原因、429 暂时不可用、429 全站满了（后台按 {detail: {code, reason, message}} 回）', async () => {
  const pending = [];
  const api = load('utils/api.js', { './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {} },
    { wx: { request(options) { pending.push(options); }, removeStorageSync() {} } });
  const cases = [
    [422, { code: 'AI_NOT_ELIGIBLE', reason: 'list', message: '文件属于名单类，无法解读' }, '名单不需要 AI 解读。要查企业在不在名单上，可以用“查名单”。'],
    [422, { code: 'AI_NOT_ELIGIBLE', reason: 'source', message: '文件不是一级公开来源，无法解读' }, '这个来源暂不用于 AI 解读。'],
    [429, { code: 'AI_UNAVAILABLE', message: 'AI 暂时不可用，请稍后再试' }, 'AI 暂时不可用，请稍后再试。'],
    [429, { code: 'AI_DAILY_CAP', message: '今天的 AI 使用次数已满，请明天再试' }, '今天 AI 的总用量满了，明天 0 点恢复；可以先看原文。'],
    // 输入不合要求的 422（没有 code）：照旧把逐项说明拼起来
    [422, [{ msg: '注册地不在列表里' }], '注册地不在列表里'],
  ];
  for (const [status, detail, text] of cases) {
    const result = api.createAdvisorTask({ kind: 'interpret', doc_id: 42 }, 'k').then(() => null, (err) => err);
    await settle();
    pending.pop().success({ statusCode: status, data: { detail } });
    const err = await result;
    assert.equal(err.status, status);
    assert.equal(interpret.errorText(err), text, JSON.stringify(detail));
    if (detail.code) assert.equal(err.code, detail.code);
    if (detail.reason) assert.equal(err.reason, detail.reason);
  }
});

test('Codex 第 2 轮：搜索带 ai=1 时，本机记的"上次是显示的"不算，后台确认开着才加筛选；确认关着就不加', async () => {
  const query = { ai: '1' };
  const cached = { visible: true, confirmed: false, available: true };
  let h = searchPage(cached);
  h.page.onLoad(query); await settle();
  assert.equal(h.page.data.aiOnly, false); assert.equal(h.calls.at(-1).ai, undefined);
  h.access.set(CLOSED); await settle();
  assert.equal(h.page.data.aiOnly, false); assert.equal(findAll(render('pages/search/index.wxml', h.page.data), (n) => n.tag === 'ai-only').length, 0);
  h = searchPage(cached); h.page.onLoad(query); await settle();
  h.access.set(OPEN); await settle();
  assert.equal(h.page.data.aiOnly, true); assert.equal(h.calls.at(-1).ai, 1);
});

test('Codex 第 2 轮：注销时能力接口在途的、缓存的答复也作废；注销以后再问，不会拿到注销以前的白名单权限', async () => {
  const pending = [];
  const api = load('utils/api.js', { './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {} },
    { wx: { request(options) { pending.push(options); }, removeStorageSync() {} } });
  const raw = load('utils/ai-access.js', { './api.js': api }, { wx: memoryWx() });
  const first = raw.ensure(); await settle();
  pending.shift().success({ statusCode: 404, data: { detail: 'Not Found' } }); await settle(); // 开放状态（旧后台）
  const oldCaps = pending.shift(); // 能力接口还在途
  assert.equal(oldCaps.url, 'https://test.invalid/api/v1/advisor/capabilities');
  raw.clear();
  const second = raw.ensure(); await settle();
  pending.shift().success({ statusCode: 404, data: { detail: 'Not Found' } }); await settle();
  const newCaps = pending.shift();
  assert.ok(newCaps, '注销以后重新发能力请求，不复用在途的那个');
  oldCaps.success({ statusCode: 200, data: ON() }); await settle(); // 注销以前的答复晚到
  newCaps.success({ statusCode: 403, data: { detail: '请先阅读并同意用户协议和隐私保护指引' } }); await settle();
  await first; await second;
  assert.deepEqual(plain(raw.current()), CLOSED);
});

test('Codex 第 2 轮："刚才看的通知"的条款对照照这份文件本身能不能做；解读页、对照页在自己今天次数用完时先拦下、不提交', async () => {
  const last = { id: '42', title: '刚才看的那份', issuer: '科技厅', date: '2026-10-16' };
  for (const [clause, caps, shown] of [[true, ON(), true], [false, ON(), false], [true, ON({ remaining: { interpret: 5, clause: 0 } }), false], [true, ON({ available: false }), false]]) {
    const h = hubPage({ last, caps, detail: (id) => Promise.resolve({ id, title: '刚才看的那份', ai: { interpret: true, clause, reason: null } }) });
    h.page.onShow(); await settle();
    const root = render('pages/ai/index.wxml', h.page.data);
    assert.equal(findAll(root, tapsTo('clauseLast')).length, shown ? 1 : 0, JSON.stringify([clause, caps.ai_advisor.remaining, caps.ai_advisor.available]));
    h.page.clauseLast(); assert.equal(h.navigations.length, shown ? 1 : 0);
  }
  for (const [page, kind, name] of [['advisor-interpret', 'interpret', 'AI 解读'], ['advisor-clause', 'clause', '条款对照']]) {
    for (const taskId of ['', '7']) {
      let created = 0; const saved = new Map([[interpret.CONSENT_KEY, true]]); const urls = [];
      const deps = {
        '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON({ remaining: { interpret: 5, clause: 5, [kind]: 0 } })), createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 1, status: 'queued' }); }, advisorTask: (id) => Promise.resolve({ id, status: 'done', result: {} }) },
        '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      };
      if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
      const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo: (o) => urls.push(o.url), getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
      p.onLoad(taskId ? { taskId } : { docId: '42', title: '通知' }); p.onShow(); await settle();
      if (taskId) { assert.equal(p.data.aiEnabled, true, `${page} 打开已有结果不拦`); p.onHide(); continue; }
      assert.equal(p.data.gate, 'used'); assert.equal(created, 0);
      const out = textOf(render(`pages/${page}/index.wxml`, p.data));
      assert.ok(out.includes(`今天的${name} 5 次用完了，明天 0 点恢复。`), out); assert.doesNotMatch(out, /以前的结果/);
      p.records(); assert.deepEqual(urls, ['/pages/advisor-tasks/index']);
      p.onHide();
    }
  }
});

test('Codex 第 3 轮：第二次进 AI 首页、详情比能力接口先回来时，不按上次的状态给"条款对照"；全站满了、后台处理不了时两项能力照样各占一行、置灰写状态', async () => {
  const last = { id: '42', title: '刚才看的那份', issuer: '科技厅', date: '2026-10-16' };
  let releaseCaps = null;
  let capsAnswer = ON();
  const h = hubPage({ last, caps: () => (releaseCaps ? new Promise((done) => { const go = releaseCaps; releaseCaps = null; go(done); }) : Promise.resolve(capsAnswer)) });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.clauseOpen, true);
  // 第二次进来：能力接口晚到（这次条款对照的次数用完了），详情先回来
  let finish;
  capsAnswer = ON({ remaining: { interpret: 5, clause: 0 } });
  releaseCaps = (done) => { finish = () => done(capsAnswer); };
  h.page.onHide(); h.page.onShow(); await settle();
  assert.equal(h.page.data.clauseOpen, false, '能力接口回来之前不给');
  assert.equal(findAll(render('pages/ai/index.wxml', h.page.data), tapsTo('clauseLast')).length, 0);
  finish(); await settle();
  assert.equal(h.page.data.clauseOpen, false, '今天条款对照用完了');
  for (const [extra, status, text] of [[{ siteFull: true }, '今天满了', '今天 AI 的总用量满了，明天 0 点恢复；可以先看原文。'], [{ available: false }, '暂时不可用', 'AI 暂时不可用，请稍后再试。']]) {
    const g = hubPage({ caps: ON(extra) }); g.page.onShow(); await settle();
    const root = render('pages/ai/index.wxml', g.page.data);
    const rows = findAll(root, hasClass('ability'));
    assert.deepEqual(rows.map((n) => textOf(n).trim().replace(/\s+/g, ' ')), [`AI 解读 把一份通知的原句分好类，约半分钟 ${status}`, `条款对照 申报条件和一家企业的情况并排列出 ${status}`]);
    assert.ok(rows.every(hasClass('ability--out')), '置灰');
    assert.ok(textOf(root).includes(text));
  }
});

test('Codex 第 3 轮：解读页、对照页被拦下时（满了、用完了、暂时不可用）给"看原文"，联系客服带上这份通知的编号和标题', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    for (const [extra, gate] of [[{ siteFull: true }, 'full'], [{ remaining: { interpret: 0, clause: 0 } }, 'used'], [{ available: false }, 'paused']]) {
      const saved = new Map([[interpret.CONSENT_KEY, true]]); const urls = [];
      const deps = {
        '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON(extra)), createAdvisorTask: () => Promise.reject(new Error('不该提交')) },
        '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      };
      if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
      const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo: (o) => urls.push(o.url), getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
      p.onLoad({ docId: '42', title: encodeURIComponent('关于组织申报的通知') }); p.onShow(); await settle();
      assert.equal(p.data.gate, gate, `${page} ${gate}`);
      const root = render(`pages/${page}/index.wxml`, p.data);
      const look = findAll(root, tapsTo('original'));
      assert.equal(look.length, 1, `${page} ${gate} 有"看原文"`);
      p.original(); assert.equal(urls.at(-1), '/pages/detail/index?id=42');
      const contacts = findAll(root, (n) => n.tag === 'contact-button');
      if (gate === 'paused') assert.deepEqual(contacts.map((n) => [n.attrs['doc-id'], n.attrs['doc-title']]), [['42', '关于组织申报的通知']]);
      else assert.equal(contacts.length, 0);
      p.onHide();
    }
  }
});

test('Codex 第 4 轮：再次进 AI 首页时先清掉上次的能力和次数、显示加载中；首页里的"AI 解读"按钮看这次的额度（没问到、满了、用完了都不给）', async () => {
  const doc = (id) => ({ id: String(id), title: `通知${id}`, kind: '申报通知', ai: { interpret: true, clause: true, reason: null } });
  const last = { id: '42', title: '刚才看的那份', issuer: '科技厅', date: '2026-10-16' };
  // 第二次进来、开放状态复核很慢：先显示加载中，不留上次的"今天还剩"
  const access = control(OPEN);
  let slow = false;
  access.ensure = () => (slow ? new Promise(() => {}) : Promise.resolve(Object.assign({}, OPEN)));
  const real = load('pages/ai/index.js', {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON()), search: () => Promise.resolve({ items: [doc(1), doc(2)] }), advisorTasks: () => Promise.resolve({ items: [] }),
      detail: (id) => Promise.resolve({ id, title: '刚才看的那份', ai: { interpret: true, clause: true, reason: null } }) },
    '../../utils/ai-access.js': Object.assign(access, { lastDoc: () => last }), '../../utils/advisor.js': advisor, '../../utils/ai-interpret.js': interpret, '../../utils/tool-context.js': context,
    '../../utils/consent.js': { ensureConsent: () => Promise.resolve(true) }, '../../utils/format.js': format,
  }, { wx: { switchTab() {}, navigateTo() {} } });
  real.getTabBar = () => ({ setData() {} });
  real.onLoad(); real.onShow(); await settle();
  assert.match(textOf(render('pages/ai/index.wxml', real.data)), /今天还剩 5 次/);
  assert.equal(findAll(render('pages/ai/index.wxml', real.data), (n) => n.tag === 'ai-button').length, 1);
  real.onHide(); slow = true; real.onShow(); await settle();
  assert.equal(real.data.capsLoading, true); assert.deepEqual(plain(real.data.kinds), []);
  assert.doesNotMatch(textOf(render('pages/ai/index.wxml', real.data)), /今天还剩/);
  assert.equal(findAll(render('pages/ai/index.wxml', real.data), (n) => n.tag === 'ai-button').length, 0, '还没问到时不给按钮');
  // 满了、解读用完了：刚才看的那份和近 7 天那几份都不给"AI 解读"；条款对照还有次数时照样给条款对照
  for (const [caps, clauseShown] of [[ON({ siteFull: true }), false], [ON({ remaining: { interpret: 0, clause: 3 } }), true], [ON(), true]]) {
    const g = hubPage({ last, caps, search: () => Promise.resolve({ items: [doc(1), doc(2)] }) });
    g.page.onShow(); await settle();
    const ok = caps.ai_advisor.siteFull !== true && caps.ai_advisor.remaining.interpret !== 0;
    const root = render('pages/ai/index.wxml', g.page.data);
    assert.equal(findAll(root, (n) => n.tag === 'ai-button').length, ok ? 1 : 0, JSON.stringify(caps.ai_advisor));
    assert.deepEqual(g.page.data.recent.map((d) => !!d.ai), [ok, ok]);
    assert.equal(findAll(root, tapsTo('clauseLast')).length, clauseShown ? 1 : 0);
  }
});

test('Codex 第 4 轮：解读页、对照页发现 AI 对这个人没开着（off）时，回首页前让入口重新问一次；列表行放不下时操作换行', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    const access = control(OPEN); const switched = [];
    const deps = {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(OFF('off')) },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': access,
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, switchTab: (o) => switched.push(o.url), getStorageSync: () => undefined, setStorageSync() {} } });
    p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
    assert.deepEqual(switched, ['/pages/home/index']); assert.equal(access.asks.refresh, 1, page);
  }
  assert.match(read('components/doc-row/index.wxss'), /\.meta \{[^}]*flex-wrap: wrap;/);
  assert.match(read('components/doc-row/index.wxss'), /\.meta-date \{ white-space: nowrap; \}/);
});

test('没同意协议时 AI 首页的列表照样给"AI 解读"（点了先走同意），两项能力换成同意入口', async () => {
  const last = { id: '42', title: '刚才看的那份', issuer: '科技厅', date: '2026-10-16' };
  const doc = { id: '1', title: '通知1', kind: '申报通知', ai: { interpret: true, clause: true, reason: null } };
  const h = hubPage({ last, caps: OFF('consent'), search: () => Promise.resolve({ items: [doc] }) });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.gate, 'consent');
  assert.equal(h.page.data.interpretOpen, true);
  assert.deepEqual(h.page.data.recent.map((d) => !!d.ai), [true]);
  const root = render('pages/ai/index.wxml', h.page.data);
  assert.equal(findAll(root, (n) => n.tag === 'ai-button').length, 1);
  assert.equal(findAll(root, tapsTo('clauseLast')).length, 1);
  assert.match(textOf(root), /使用 AI 前，请先阅读并同意/);
});

test('Codex 第 5 轮：AI 首页再次进来先清掉"我的 AI 记录"，离开后回来的旧结果不写页面；注销（状态被擦掉）时清掉记录和刚才看的那份', async () => {
  const last = { id: '42', title: '刚才看的那份', issuer: '科技厅', date: '2026-10-16' };
  const tasks = [{ id: 1, kind: 'interpret', status: 'done', docTitle: '旧账号的通知', createdAt: '2026-10-18T02:00:00Z' }];
  let held = null;
  const h = hubPage({ last, tasks: () => (held ? new Promise((done) => { held.push(done); }) : Promise.resolve({ items: tasks })) });
  h.page.onShow(); await settle();
  assert.deepEqual(h.page.data.records.map((r) => r.title), ['旧账号的通知']);
  // 再次进来：先清掉，记录接口晚到
  held = [];
  h.page.onHide(); h.page.onShow();
  assert.deepEqual(plain(h.page.data.records), [], '进来时先清掉');
  await settle();
  // 离开以后旧请求才回来：不写页面
  h.page.onHide();
  held.forEach((done) => done({ items: tasks })); await settle();
  assert.deepEqual(plain(h.page.data.records), []);
  // 注销：状态被擦掉，页面上的记录和刚才看的那份都清掉
  held = null; h.page.onShow(); await settle();
  assert.equal(h.page.data.records.length, 1);
  h.access.set({ visible: false, confirmed: false, available: false });
  assert.deepEqual(plain(h.page.data.records), []); assert.equal(h.page.data.lastDoc, null);
});

test('Codex 第 5 轮：任务因全站满了、暂时不可用、被移出白名单失败时不给"重试"；其他失败照旧能重试；AI 首页列表提示只在有按钮时说"点 AI 解读"', async () => {
  for (const [errorCode, canRetry] of [['AI_DAILY_CAP', false], ['AI_UNAVAILABLE', false], ['AI_OFF', false], [null, true]]) {
    const states = [];
    const saved = new Map([[interpret.CONSENT_KEY, true]]);
    const session = interpret.createSession({
      api: { advisorTask: (id) => Promise.resolve({ id, status: 'failed', error: '后台的话', errorCode, docId: '42', docTitle: '通知' }) },
      storage: { getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) },
      update: (value) => states.push(value), docId: '42', title: '通知', taskId: '9',
    });
    session.show(); await settle();
    const lastState = Object.assign({}, ...states);
    assert.equal(lastState.phase, 'failed'); assert.equal(lastState.canRetry, canRetry, String(errorCode));
    session.hide();
  }
  for (const [caps, mentions] of [[ON(), true], [ON({ siteFull: true }), false], [ON({ remaining: { interpret: 0, clause: 5 } }), false]]) {
    const h = hubPage({ caps }); h.page.onShow(); await settle();
    assert.equal(/点“AI 解读”直接开始/.test(textOf(render('pages/ai/index.wxml', h.page.data))), mentions, JSON.stringify(caps.ai_advisor));
  }
});

test('Codex 第 6 轮：没同意协议、后台又暂时处理不了时不给按钮也不说"点 AI 解读"；注销后马上收起页头回到加载中；再次进来列表回到加载中、不留上次的错误', async () => {
  const last = { id: '42', title: '刚才看的那份', issuer: '科技厅', date: '2026-10-16' };
  const doc = { id: '1', title: '通知1', kind: '申报通知', ai: { interpret: true, clause: true, reason: null } };
  const h = hubPage({ state: { visible: true, confirmed: true, available: false }, last, caps: OFF('consent'), search: () => Promise.resolve({ items: [doc] }) });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.interpretOpen, false); assert.equal(h.page.data.clauseOpen, false);
  let root = render('pages/ai/index.wxml', h.page.data);
  assert.doesNotMatch(textOf(root), /点“AI 解读”直接开始/);
  assert.equal(findAll(root, tapsTo('clauseLast')).length, 0);
  // 注销：页头、说明马上收起
  const g = hubPage(); g.page.onShow(); await settle();
  assert.match(textOf(render('pages/ai/index.wxml', g.page.data)), /AI 生成 · 以原文为准/);
  g.access.set({ visible: false, confirmed: false, available: false });
  root = render('pages/ai/index.wxml', g.page.data);
  assert.equal(g.page.data.ready, false); assert.doesNotMatch(textOf(root), /AI/);
  // 再次进来、后台重新确认很慢（还没开始查列表）：近 7 天的列表先回到加载中，不留上次的错误、不写"没有通知"
  const r = hubPage({ search: () => Promise.reject(new Error('网络')) });
  r.page.onShow(); await settle();
  assert.equal(r.page.data.recentError, '近 7 天的通知没有加载出来');
  r.access.ensure = () => new Promise(() => {});
  r.page.onHide(); r.page.onShow(); await settle();
  assert.equal(r.page.data.recentLoading, true); assert.equal(r.page.data.recentError, '');
  assert.doesNotMatch(textOf(render('pages/ai/index.wxml', r.page.data)), /没有加载出来|近 7 天还没有能解读的申报通知/);
});

test('Codex 第 7 轮：结果页吸顶条开头固定写"AI 生成："（后端提示里没有这几个字也写，有的不写两遍）', async () => {
  const backendNotice = 'AI 生成，仅供参考，以原文和主管部门解释为准';
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    for (const [notice, expected] of [['固定原文提示', 'AI 生成：固定原文提示'], [backendNotice, 'AI 生成：仅供参考，以原文和主管部门解释为准']]) {
      const saved = new Map([[interpret.CONSENT_KEY, true]]);
      const result = kind === 'clause'
        ? { notice, rows: [{ type: '条件', text: '条件原句', refs: [{ pid: 'P001' }], company: [] }] }
        : { notice, items: [{ type: '对象', text: '对象原句', refs: [{ pid: 'P001', quote: '对象原句' }] }] };
      const deps = {
        '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON()), advisorTask: (id) => Promise.resolve({ id, docId: 42, docTitle: '通知', status: 'done', result }) },
        '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      };
      if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
      const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
      p.onLoad({ taskId: '7' }); p.onShow(); await settle();
      assert.equal(p.data.phase, 'done', `${page} 出了结果`);
      const bars = findAll(render(`pages/${page}/index.wxml`, p.data), hasClass('interpret-notice'));
      assert.equal(bars.length, 1, `${page} 有吸顶条`);
      assert.equal(textOf(bars[0]).trim(), expected, `${page} ${notice}`);
      p.onHide();
    }
  }
});

test('Codex 第 7 轮：AI 首页用着用着后台处理不了，马上收起"AI 解读""条款对照"、能力行写暂时不可用，点了也不进；恢复了照这次的额度放出来', async () => {
  const last = { id: '42', title: '刚才看的那份', issuer: '科技厅', date: '2026-10-16' };
  const doc = { id: '1', title: '通知1', kind: '申报通知', ai: { interpret: true, clause: true, reason: null } };
  const h = hubPage({ last, search: () => Promise.resolve({ items: [doc] }) });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.interpretOpen, true); assert.equal(h.page.data.clauseOpen, true);
  assert.equal(findAll(render('pages/ai/index.wxml', h.page.data), tapsTo('clauseLast')).length, 1);
  // 后台说处理不了（另一个页面重新问到的）：不用离开再进来，马上收起
  h.access.set({ available: false });
  assert.equal(h.page.data.gate, 'paused'); assert.equal(h.page.data.interpretOpen, false); assert.equal(h.page.data.clauseOpen, false);
  let root = render('pages/ai/index.wxml', h.page.data);
  let out = textOf(root);
  assert.equal(findAll(root, tapsTo('clauseLast')).length, 0, '不给条款对照');
  assert.doesNotMatch(out, /点“AI 解读”直接开始|今天还剩/);
  assert.equal((out.match(/暂时不可用/g) || []).length >= 2, true, '两项能力都写暂时不可用');
  assert.ok(out.includes('AI 暂时不可用，请稍后再试。'));
  assert.deepEqual(h.page.data.recent.map((item) => item.ai), [null], '近 7 天那几份上不放按钮');
  assert.deepEqual(findAll(root, (n) => n.tag === 'contact-button').map((n) => n.attrs.reason), ['paused', undefined]);
  // 页面上还是旧的样子时点了，也不进对照页
  h.page.setData({ clauseOpen: true }); h.page.clauseLast();
  assert.deepEqual(h.navigations, []);
  // 恢复了：照这次能力接口的结果（今天还剩几次）放出来
  h.access.set({ available: true });
  assert.equal(h.page.data.gate, ''); assert.equal(h.page.data.interpretOpen, true); assert.equal(h.page.data.clauseOpen, true);
  out = textOf(render('pages/ai/index.wxml', h.page.data));
  assert.match(out, /今天还剩 5 次/); assert.match(out, /点“AI 解读”直接开始/);
  h.page.clauseLast(); assert.equal(h.navigations.length, 1);
  // 全站满了的时候后台又处理不了：改写暂时不可用；恢复了回到"今天满了"
  const f = hubPage({ caps: ON({ siteFull: true }) }); f.page.onShow(); await settle();
  assert.equal(f.page.data.gate, 'full');
  f.access.set({ available: false }); assert.equal(f.page.data.gate, 'paused'); assert.equal(f.page.data.gateText, 'AI 暂时不可用，请稍后再试。');
  f.access.set({ available: true }); assert.equal(f.page.data.gate, 'full'); assert.equal(f.page.data.interpretOpen, false);
  // 没同意协议：照样是同意入口，只是按钮收起
  const c = hubPage({ caps: OFF('consent'), search: () => Promise.resolve({ items: [doc] }) }); c.page.onShow(); await settle();
  assert.equal(c.page.data.interpretOpen, true);
  c.access.set({ available: false });
  assert.equal(c.page.data.gate, 'consent'); assert.equal(c.page.data.interpretOpen, false); assert.equal(c.page.data.clauseOpen, false);
  // 能力接口还没回来时后台的状态先变了：回来以后照最新的状态算
  let answer;
  const s = hubPage({ caps: () => new Promise((done) => { answer = done; }) }); s.page.onShow(); await settle();
  s.access.set({ available: false }); answer(ON()); await settle();
  assert.equal(s.page.data.gate, 'paused'); assert.equal(s.page.data.interpretOpen, false);
  // 再次进来、这次的能力接口还没回来时后台状态变了：不拿上次的结果放出按钮
  let hang = false;
  const a = hubPage({ caps: () => (hang ? new Promise(() => {}) : Promise.resolve(ON())) }); a.page.onShow(); await settle();
  assert.equal(a.page.data.interpretOpen, true);
  a.page.onHide(); hang = true; a.page.onShow(); await settle();
  a.access.set({ available: false }); a.access.set({ available: true });
  assert.equal(a.page.data.interpretOpen, false); assert.equal(a.page.data.clauseOpen, false); assert.equal(a.page.data.gate, '');
  // 注销以后又显示了（换了账号）：重新问到之前不拿注销以前的结果
  const o = hubPage(); o.page.onShow(); await settle();
  o.access.set({ visible: false, confirmed: false, available: false });
  o.access.set({ visible: true, confirmed: true, available: true });
  assert.equal(o.page.data.interpretOpen, false); assert.equal(o.page.data.gate, '');
});

test('Codex 第 7 轮：我的 AI 记录没加载出来时卡片照样留着，写明没加载出来、给重试，"全部"照样能点；一条都没有时不占地方', async () => {
  let tasksOk = false;
  const record = { id: 3, kind: 'interpret', status: 'done', docTitle: '通知3', createdAt: '2026-10-19T09:00:00+08:00' };
  const h = hubPage({ tasks: () => (tasksOk ? Promise.resolve({ items: [record] }) : Promise.reject(new Error('网络'))) });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.recordsError, '我的 AI 记录没有加载出来');
  const root = render('pages/ai/index.wxml', h.page.data);
  const card = findAll(root, hasClass('card')).find((node) => textOf(node).includes('我的 AI 记录'));
  assert.ok(card, '卡片还在');
  assert.match(textOf(card), /我的 AI 记录没有加载出来/);
  assert.equal(findAll(card, tapsTo('retry')).length, 1, '有重试');
  assert.equal(findAll(card, tapsTo('allRecords')).length, 1, '有"全部"');
  h.page.allRecords(); assert.equal(h.navigations.at(-1), '/pages/advisor-tasks/index');
  // 点重试、这次加载出来了：错误去掉、列出记录
  tasksOk = true; h.page.retry(); await settle();
  assert.equal(h.page.data.recordsError, '');
  assert.match(textOf(render('pages/ai/index.wxml', h.page.data)), /我的 AI 记录.*通知3/);
  // 没有记录：不显示这张卡
  const e = hubPage(); e.page.onShow(); await settle();
  assert.doesNotMatch(textOf(render('pages/ai/index.wxml', e.page.data)), /我的 AI 记录/);
  // 再次进来：上次的错误先清掉
  const r = hubPage({ tasks: () => Promise.reject(new Error('网络')) }); r.page.onShow(); await settle();
  r.access.ensure = () => new Promise(() => {});
  r.page.onHide(); r.page.onShow(); await settle();
  assert.equal(r.page.data.recordsError, '');
});

test('Codex 第 8 轮：出了结果吸顶条就一直有——后端没给提示、只有空白或只有"AI 生成"时，用页面自己的说明；上面那条普通提示不再重复', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    for (const notice of [undefined, '', '   ', 'AI 生成', ' AI 生成： ']) {
      const saved = new Map([[interpret.CONSENT_KEY, true]]);
      const result = kind === 'clause'
        ? { rows: [{ type: '条件', text: '条件原句', refs: [{ pid: 'P001' }], company: [] }] }
        : { items: [{ type: '对象', text: '对象原句', refs: [{ pid: 'P001', quote: '对象原句' }] }] };
      if (notice !== undefined) result.notice = notice;
      const deps = {
        '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON()), advisorTask: (id) => Promise.resolve({ id, docId: 42, docTitle: '通知', status: 'done', result }) },
        '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      };
      if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
      const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
      p.onLoad({ taskId: '7' }); p.onShow(); await settle();
      assert.equal(p.data.phase, 'done', `${page} 出了结果`);
      const root = render(`pages/${page}/index.wxml`, p.data);
      const bars = findAll(root, hasClass('interpret-notice'));
      assert.equal(bars.length, 1, `${page} ${JSON.stringify(notice)} 有吸顶条`);
      assert.equal(textOf(bars[0]).trim(), `AI 生成：${p.data.aiNotice}`, `${page} ${JSON.stringify(notice)}`);
      assert.ok(p.data.aiNotice.length > 0);
      assert.equal(findAll(root, hasClass('ai-internal')).length, 0, `${page} 出结果后不再有上面那条`);
      p.onHide();
    }
  }
});

test('Codex 第 8 轮：AI 首页第一次问到的就是"暂时不可用"，停在页面上时后台恢复了：再问一次能力接口，照新的次数放出按钮；页面不在前面时不问，又说处理不了时不反复问', async () => {
  const paused = { visible: true, confirmed: true, available: false };
  let answers = [ON({ available: false }), ON({ remaining: { interpret: 3, clause: 4 } })];
  const h = hubPage({ state: paused, caps: () => Promise.resolve(answers.shift() || ON({ available: false })) });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.gate, 'paused'); assert.equal(h.page.data.interpretOpen, false); assert.equal(h.calls.caps, 1);
  h.access.set({ available: true }); await settle();
  assert.equal(h.calls.caps, 2, '恢复了再问一次'); assert.equal(h.calls.refresh, true);
  assert.equal(h.page.data.gate, ''); assert.equal(h.page.data.interpretOpen, true); assert.equal(h.page.data.clauseOpen, true);
  assert.match(textOf(render('pages/ai/index.wxml', h.page.data)), /今天还剩 3 次/);
  // 又说处理不了：照旧收起，不再问能力接口
  h.access.set({ available: false }); await settle();
  assert.equal(h.calls.caps, 2); assert.equal(h.page.data.gate, 'paused');
  // 能力接口再问一次还是"处理不了"：停在暂时不可用，只问这一次
  answers = [ON({ available: false })];
  const g = hubPage({ state: paused, caps: () => Promise.resolve(answers.shift() || ON({ available: false })) });
  g.page.onShow(); await settle();
  g.access.set({ available: true }); await settle();
  assert.equal(g.calls.caps, 2); assert.equal(g.page.data.gate, 'paused'); assert.equal(g.page.data.interpretOpen, false);
  // 能力接口说处理不了、公开状态接口又说处理不了（还没恢复）：不问
  answers = [ON({ available: false })];
  const n = hubPage({ caps: () => Promise.resolve(answers.shift() || ON()) });
  n.page.onShow(); await settle();
  assert.equal(n.page.data.gate, 'paused');
  n.access.set({ available: false }); await settle();
  assert.equal(n.calls.caps, 1, '没恢复不问');
  // 页面不在前面时恢复了：不问，回来时 onShow 照常重新问
  answers = [ON({ available: false }), ON()];
  const b = hubPage({ state: paused, caps: () => Promise.resolve(answers.shift() || ON()) });
  b.page.onShow(); await settle(); b.page.onHide();
  b.access.set({ available: true }); await settle();
  assert.equal(b.calls.caps, 1, '不在前面不问');
  b.page.onShow(); await settle();
  assert.equal(b.calls.caps, 2); assert.equal(b.page.data.gate, ''); assert.equal(b.page.data.interpretOpen, true);
});

test('Codex 第 9 轮：后台说暂时处理不了时，页面显示过了 15 秒就再问（不等 10 分钟）；连不上时不管以前问到过没有，15 秒内都不重复问', async () => {
  let answer = { open: true, available: false };
  const h = accessWith({ mode: () => (answer ? Promise.resolve(answer) : NETWORK()) });
  assert.deepEqual(await h.access.ensure(), { visible: true, confirmed: true, available: false }); assert.equal(h.calls.mode, 1);
  await h.access.ensure(); assert.equal(h.calls.mode, 1, '15 秒内不再问');
  answer = { open: true, available: true };
  h.clock.now += h.access.RETRY_AFTER; assert.deepEqual(await h.access.ensure(), OPEN); assert.equal(h.calls.mode, 2, '15 秒后再问，恢复了');
  h.clock.now += h.access.RETRY_AFTER; await h.access.ensure(); assert.equal(h.calls.mode, 2, '能处理时照旧 10 分钟问一次');
  // 问过超过 10 分钟、这次连不上：照旧当作开着，15 秒内不再问
  answer = null; h.clock.now += h.access.RECHECK; assert.deepEqual(await h.access.ensure(), OPEN); assert.equal(h.calls.mode, 3);
  await h.access.ensure(); assert.equal(h.calls.mode, 3, '连不上后 15 秒内不重复问');
  answer = { open: true, available: true };
  h.clock.now += h.access.RETRY_AFTER; await h.access.ensure(); assert.equal(h.calls.mode, 4);
  await h.access.refresh(); assert.equal(h.calls.mode, 5, '"重试"马上再问');
  // AI 关着（首发版，所有人都是这样）：2 分钟问一次，不因为"处理不了"每 15 秒问
  const off = accessWith({ mode: { open: false, available: false }, caps: OFF() });
  assert.deepEqual(await off.access.ensure(), CLOSED); assert.deepEqual([off.calls.mode, off.calls.caps], [1, 1]);
  off.clock.now += off.access.RETRY_AFTER * 4; await off.access.ensure();
  assert.deepEqual([off.calls.mode, off.calls.caps], [1, 1], '关着时 2 分钟内不再问');
});

test('Codex 第 9 轮：AI 首页用真的 ai-access——恢复以后离开再回来、15 秒内回来、停在页面上点"重试"，按钮都能出来；还没恢复时不乱问', async () => {
  let up = false;
  const make = () => {
    const real = accessWith({ mode: () => Promise.resolve({ open: true, available: up }) });
    const h = hubPage({ realAccess: real.access, caps: () => Promise.resolve(up ? ON() : ON({ available: false })) });
    return { real, h };
  };
  // 离开 15 秒以后再回来：重新问开放状态，按钮出来
  let { real, h } = make();
  h.page.onShow(); await settle();
  assert.equal(h.page.data.gate, 'paused'); assert.equal(h.page.data.interpretOpen, false); assert.equal(real.calls.mode, 1, '还没恢复时只问一次');
  up = true; h.page.onHide(); real.clock.now += real.access.RETRY_AFTER; h.page.onShow(); await settle();
  assert.equal(real.calls.mode, 2); assert.equal(h.page.data.gate, ''); assert.equal(h.page.data.interpretOpen, true);
  // 15 秒内就回来、能力接口说能处理：马上再问一次开放状态，按钮出来
  up = false; ({ real, h } = make());
  h.page.onShow(); await settle(); assert.equal(h.page.data.gate, 'paused');
  up = true; h.page.onHide(); real.clock.now += 5000; h.page.onShow(); await settle();
  assert.equal(real.calls.mode, 2); assert.equal(h.page.data.gate, ''); assert.equal(h.page.data.interpretOpen, true); assert.equal(h.page.data.clauseOpen, true);
  // 停在页面上：暂时不可用那里有"重试"，点了马上再问，恢复了按钮出来
  up = false; ({ real, h } = make());
  h.page.onShow(); await settle();
  const gate = findAll(render('pages/ai/index.wxml', h.page.data), hasClass('gate'))[0];
  assert.ok(gate && textOf(gate).includes('AI 暂时不可用，请稍后再试。'));
  assert.equal(findAll(gate, tapsTo('retry')).length, 1, '暂时不可用时有重试');
  assert.equal(findAll(gate, (n) => n.tag === 'contact-button').length, 1, '也能联系客服');
  up = true; h.page.retry(); await settle();
  assert.equal(h.page.data.gate, ''); assert.equal(h.page.data.interpretOpen, true);
  assert.match(textOf(render('pages/ai/index.wxml', h.page.data)), /今天还剩 5 次/);
});

test('Codex 第 10 轮：列表行按钮、详情那一行提示在页面回到前台时再问一次（用真的 ai-access）：后台恢复了、过了 15 秒，按钮和提示出来', async () => {
  for (const [file, props, deps] of [
    ['components/ai-button/index.js', { docId: '42', docTitle: '通知', ai: { interpret: true, clause: true, reason: null } }, { '../../utils/tool-context.js': context }],
    ['components/ai-hint/index.js', { docId: '42', docTitle: '通知', issuer: '科技厅', date: '2026-10-16', ai: { interpret: true, clause: true, reason: null } }, {}],
  ]) {
    let up = false;
    const real = accessWith({ mode: () => Promise.resolve({ open: true, available: up }) });
    const view = load(file, { '../../utils/ai-access.js': real.access, ...deps }, { wx: { navigateTo() {} } });
    Object.assign(view.properties, props);
    view.triggerEvent = () => {};
    view.lifetimes.attached.call(view); await settle();
    assert.equal(view.data.shown, false, `${file} 暂时不可用时不显示`); assert.equal(real.calls.mode, 1);
    // 后台恢复了；页面不在前台时没人问
    up = true; real.clock.now += real.access.RETRY_AFTER;
    assert.equal(typeof view.pageLifetimes.show, 'function', `${file} 有页面显示时的处理`);
    view.pageLifetimes.show.call(view); await settle();
    assert.equal(real.calls.mode, 2, `${file} 回到前台再问`); assert.equal(view.data.shown, true, `${file} 恢复后显示`);
    // 能处理时回到前台：10 分钟内不再问
    view.pageLifetimes.show.call(view); await settle(); assert.equal(real.calls.mode, 2);
    view.lifetimes.detached.call(view);
  }
});

test('Codex 第 10 轮：对所有人开放、还没同意协议时，"我的 AI 记录"接口拒绝（401、403、404）不算没加载出来：不显示这张卡、不给重试；连不上照旧写没加载出来', async () => {
  for (const status of [401, 403, 404]) {
    const h = hubPage({ caps: OFF('consent'), tasks: () => Promise.reject(Object.assign(new Error('请先同意'), { status })) });
    h.page.onShow(); await settle();
    assert.equal(h.page.data.gate, 'consent'); assert.equal(h.page.data.recordsError, '', String(status));
    const out = textOf(render('pages/ai/index.wxml', h.page.data));
    assert.doesNotMatch(out, /我的 AI 记录|没有加载出来/, String(status));
    assert.match(out, /阅读并同意/);
  }
  const n = hubPage({ caps: OFF('consent'), tasks: () => Promise.reject(Object.assign(new Error('网络不太好'), { retryable: true })) });
  n.page.onShow(); await settle();
  assert.equal(n.page.data.recordsError, '我的 AI 记录没有加载出来');
  const s = hubPage({ tasks: () => Promise.reject(Object.assign(new Error('服务出错'), { status: 500 })) });
  s.page.onShow(); await settle();
  assert.equal(s.page.data.recordsError, '我的 AI 记录没有加载出来', '服务出错也写没加载出来');
});

// 真的 utils/consent.js，接上真的 ai-access（accessWith 建的 raw）
function consentWith(aiAccess, extraApi = {}) {
  const stored = {}; const toasts = []; const modals = []; const navigations = [];
  const wx = {
    getStorageSync: (k) => stored[k] || '', setStorageSync: (k, v) => { stored[k] = v; }, removeStorageSync: (k) => { delete stored[k]; },
    clearStorageSync: () => { Object.keys(stored).forEach((k) => delete stored[k]); }, showToast: (o) => toasts.push(o.title), showModal: (o) => modals.push(o),
    navigateTo: (o) => navigations.push(o.url),
  };
  const mod = load('utils/consent.js', {
    '../content/legal.js': { final: true, version: '2026-10' }, './config.js': { templates: {} },
    './api.js': { isLive: () => true, AGREED_KEY: 'agreed', forgetAgreed() { delete stored.agreed; }, ...extraApi },
    './exports.js': { clear() {} }, './exporter.js': { removeFiles() {} }, './ai-access.js': aiAccess,
  }, { wx });
  return { mod, stored, modals, toasts, navigations };
}

test('Codex 第 11 轮：协议同意状态变了，AI 板块马上重新问——白名单里的人刚同意第 5 格马上出来、同意以前发出的旧答复不用；/me 发现失效也重问，没变不问；注销以后再问一次', async () => {
  // 白名单阶段：没同意时能力接口说没开着
  let agreed = false; let release = null; let hold = false;
  const h = accessWith({ mode: { open: false, available: false }, caps: () => {
    if (hold) return new Promise((done) => { release = () => done(OFF('consent')); });
    return Promise.resolve(agreed ? ON() : OFF('consent'));
  } });
  const c = consentWith(h.access.raw);
  assert.deepEqual(await h.access.ensure(), CLOSED);
  let asked = h.calls.caps;
  // 同意以前发出去、同意以后才回来的答复不用
  hold = true; h.access.raw.refresh(); await settle(); assert.equal(h.calls.caps, asked + 1);
  hold = false; agreed = true; c.mod.markAgreed(); await settle();
  assert.equal(h.calls.caps, asked + 2, '刚同意马上重问'); assert.ok(h.calls.forgot >= 1, '能力接口的缓存不用');
  assert.deepEqual(h.access.current(), OPEN, '第 5 格马上出来');
  release(); await settle();
  assert.deepEqual(h.access.current(), OPEN, '旧答复不用');
  // /me 发现同意已失效（在别处注销、仍认的最早版本调高了）：AI 板块重问，白名单阶段随即收起
  agreed = false; asked = h.calls.caps;
  assert.equal(c.mod.agreedHere(), true);
  assert.equal(c.mod.applyServer(null, c.mod.consentEpoch()), false); await settle();
  assert.equal(h.calls.caps, asked + 1); assert.deepEqual(h.access.current(), CLOSED);
  // 和本机记的一样：不重问
  asked = h.calls.caps;
  c.mod.applyServer(null, c.mod.consentEpoch()); await settle();
  assert.equal(h.calls.caps, asked, '没变不问');
  // 在别的手机同意过：本机补上记录，AI 板块重问
  agreed = true;
  assert.equal(c.mod.applyServer({ version: '2026-10', valid: true }, c.mod.consentEpoch()), true); await settle();
  assert.equal(h.calls.caps, asked + 1); assert.deepEqual(h.access.current(), OPEN);
  // 注销：擦掉以后马上再问一次；对所有人开放时，没同意的人照样看得到板块
  const o = accessWith({ mode: { open: true, available: true } });
  const w = consentWith(o.access.raw, { deleteMe: () => Promise.resolve({}) });
  assert.deepEqual(await o.access.ensure(), OPEN);
  const modes = o.calls.mode;
  const done = w.mod.withdraw(); w.modals[0].success({ confirm: true });
  assert.equal(await done, true); await settle();
  assert.equal(o.calls.mode, modes + 1, '注销以后再问一次'); assert.deepEqual(o.access.current(), OPEN);
});

test('Codex 第 11 轮：AI 关着时 2 分钟问一次——后台打开以后，正在用的人下一次切页面或回到前台（距上次问过 2 分钟以上）时看到第 5 格', async () => {
  let answer = { open: false, available: false };
  const h = accessWith({ mode: () => Promise.resolve(answer), caps: OFF() });
  assert.deepEqual(await h.access.ensure(), CLOSED);
  answer = { open: true, available: true };
  h.clock.now += h.access.RETRY_AFTER; await h.access.ensure();
  assert.equal(h.calls.mode, 1, '15 秒后不问');
  h.clock.now += h.access.CLOSED_RECHECK - h.access.RETRY_AFTER; assert.deepEqual(await h.access.ensure(), OPEN);
  assert.equal(h.calls.mode, 2, '2 分钟后再问，开了');
  assert.ok(h.access.CLOSED_RECHECK < h.access.RECHECK);
});

test('Codex 第 11 轮：搜索页带 ai=1 时，注销后被擦掉（还没重新问到）也去掉这一项重查；搜索页、帮助页回到前台时再问一次', async () => {
  const query = { kind: encodeURIComponent('申报通知'), time: encodeURIComponent('近 7 天'), ai: '1' };
  const s = searchPage(OPEN);
  s.page.onLoad(query); await settle();
  assert.equal(s.page.data.aiOnly, true);
  const before = s.calls.length;
  s.access.set({ visible: false, confirmed: false, available: false }); await settle();
  assert.equal(s.page.data.aiOnly, false); assert.ok(s.calls.length > before, '重查'); assert.equal(s.calls.at(-1).ai, undefined);
  const ensured = s.access.asks.ensure;
  s.page.onShow(); assert.equal(s.access.asks.ensure, ensured + 1, '回到前台再问');
  // 重新问到开着：照旧加回去
  s.access.set(OPEN); await settle();
  assert.equal(s.page.data.aiOnly, true); assert.equal(s.calls.at(-1).ai, 1);
  // 没带 ai=1 的搜索页不问
  const plainSearch = searchPage(OPEN); plainSearch.page.onLoad({}); await settle();
  const n = plainSearch.access.asks.ensure; plainSearch.page.onShow(); assert.equal(plainSearch.access.asks.ensure, n);
  // 帮助页
  const help = require('../content/help.js');
  const access = control(OPEN);
  const p = load('pages/help/index.js', { '../../content/help.js': help, '../../utils/ai-access.js': access, '../../utils/consent.js': { loginOpen: () => true } },
    { wx: { setNavigationBarTitle() {}, navigateTo() {} } });
  p.onLoad(); const m = access.asks.ensure; p.onShow(); assert.equal(access.asks.ensure, m + 1);
  access.set({ visible: false, confirmed: false, available: false });
  assert.doesNotMatch(textOf(render('pages/help/index.wxml', p.data)), /\bAI\b/, '擦掉以后帮助页没有 AI 字样');
});

test('Codex 第 11 轮：解读页、对照页、AI 首页的能力接口说的和 AI 板块记的对不上时，板块马上重问，列表上的按钮跟着收起或放出', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause'], ['ai', 'hub']]) {
    let avail = true;
    const real = accessWith({ mode: () => Promise.resolve({ open: true, available: avail }) });
    assert.deepEqual(await real.access.ensure(), OPEN);
    const btn = load('components/ai-button/index.js', { '../../utils/ai-access.js': real.access, '../../utils/tool-context.js': context }, { wx: { navigateTo() {} } });
    Object.assign(btn.properties, { docId: '42', docTitle: '通知', ai: { interpret: true, clause: true, reason: null } });
    btn.triggerEvent = () => {}; btn.lifetimes.attached.call(btn); await settle();
    assert.equal(btn.data.shown, true, `${page} 列表按钮先显示`);
    // 后台刚变成处理不了：这一页自己的能力接口先知道
    avail = false;
    let p;
    if (kind === 'hub') {
      p = hubPage({ realAccess: real.access, caps: () => Promise.resolve(ON({ available: avail })) }).page;
      p.onShow();
    } else {
      const deps = {
        '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON({ available: avail })), createAdvisorTask: () => Promise.reject(new Error('不该提交')) },
        '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': real.access,
      };
      if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
      p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, switchTab() {}, getStorageSync: () => '', setStorageSync() {} } });
      p.onLoad({ docId: '42', title: '通知' }); p.onShow();
    }
    await settle();
    assert.equal(p.data.gate, 'paused', `${page} 写暂时不可用`);
    assert.equal(real.calls.mode, 2, `${page} 板块马上重问`);
    assert.equal(btn.data.shown, false, `${page} 列表按钮跟着收起`);
    // 恢复了：这一页的能力接口说能处理、板块记的是处理不了，也重问，按钮放出来
    avail = true;
    if (kind === 'hub') { p.onHide(); p.onShow(); } else { p.reload(); }
    await settle();
    assert.equal(real.calls.mode, 3, `${page} 恢复了也重问`); assert.equal(btn.data.shown, true, `${page} 按钮放出来`);
    if (p.onHide) p.onHide();
    btn.lifetimes.detached.call(btn);
  }
});

test('Codex 第 11 轮：解读页、对照页停在"暂时不可用"时有"重试"，后台恢复后点一下就能用', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    let up = false;
    const deps = {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(up ? ON() : ON({ available: false })), createAdvisorTask: () => Promise.reject(new Error('不该提交')) },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, getStorageSync: () => '', setStorageSync() {} } });
    p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
    assert.equal(p.data.gate, 'paused');
    const root = render(`pages/${page}/index.wxml`, p.data);
    assert.equal(findAll(root, tapsTo('reload')).length, 1, `${page} 有重试`);
    assert.ok(textOf(root).includes('重试'));
    up = true; p.reload(); await settle();
    assert.equal(p.data.aiEnabled, true, `${page} 恢复后能用`); assert.equal(p.data.gate, '');
    // 别的情况（满了、用完了）不给重试：重试也没用
    p.setData({ aiEnabled: false, gate: 'full' }); p.reload(); assert.equal(p.data.gate, 'full');
    p.onHide();
  }
});

test('Codex 第 12 轮：能力接口先发现没同意协议时也让板块重问——白名单阶段随即收起，对所有人开放时照样显示、只问一次', async () => {
  // 白名单阶段：板块记的是开着（以前同意过），解读页的能力接口先发现同意失效了
  let lapsed = false;
  const w = accessWith({ mode: { open: false, available: false }, caps: () => Promise.resolve(lapsed ? OFF('consent') : ON()) });
  assert.deepEqual(await w.access.ensure(), OPEN);
  const btn = load('components/ai-button/index.js', { '../../utils/ai-access.js': w.access, '../../utils/tool-context.js': context }, { wx: { navigateTo() {} } });
  Object.assign(btn.properties, { docId: '42', docTitle: '通知', ai: { interpret: true, clause: true, reason: null } });
  btn.triggerEvent = () => {}; btn.lifetimes.attached.call(btn); await settle();
  assert.equal(btn.data.shown, true);
  lapsed = true;
  const deps = {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(OFF('consent')), createAdvisorTask: () => Promise.reject(new Error('不该提交')) },
    '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': w.access,
  };
  const p = load('pages/advisor-interpret/index.js', deps, { wx: { setNavigationBarTitle() {}, switchTab() {}, getStorageSync: () => '', setStorageSync() {} } });
  p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
  assert.equal(p.data.gate, 'consent', '页面引导同意');
  assert.deepEqual(w.access.current(), CLOSED, '白名单阶段板块收起'); assert.equal(btn.data.shown, false, '列表按钮收起');
  p.onHide(); btn.lifetimes.detached.call(btn);
  // 对所有人开放、没同意协议：重问一次开放状态，照样显示，不反复问
  const o = accessWith({ mode: { open: true, available: true } });
  assert.deepEqual(await o.access.ensure(), OPEN);
  const modes = o.calls.mode;
  const h = hubPage({ realAccess: o.access, caps: OFF('consent') });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.gate, 'consent'); assert.deepEqual(o.access.current(), OPEN, '照样显示');
  // 显示时重新问一次（第 24 轮），能力接口说没同意再问一次；之后不再问（不会一直问下去）
  assert.equal(o.calls.mode, modes + 2);
  await settle(); assert.equal(o.calls.mode, modes + 2, '不反复问');
  // 板块本来就没显示（AI 关着）：能力接口说没同意也不问
  const c = accessWith({ mode: { open: false, available: false }, caps: OFF('consent') });
  assert.deepEqual(await c.access.ensure(), CLOSED);
  const calls = [c.calls.mode, c.calls.caps];
  c.access.raw.reconcile(OFF('consent')); await settle();
  assert.deepEqual([c.calls.mode, c.calls.caps], calls);
});

test('Codex 第 12 轮：发现对不上时在途的旧查询作废——旧的先回来、后回来都不用它的答复，等它的页面拿到新发的那个的结果', async () => {
  for (const order of ['old-first', 'new-first']) {
    const answers = [];
    const h = accessWith({ mode: () => new Promise((done) => answers.push(done)) });
    const first = h.access.raw.ensure(); answers.shift()({ open: true, available: true }); assert.deepEqual(plain(await first), OPEN);
    // 在途一个（变以前发的），这时解读页的能力接口说后台处理不了
    h.clock.now += h.access.RECHECK;
    const stale = h.access.raw.ensure(); await settle();
    assert.equal(answers.length, 1, '旧查询在途');
    h.access.raw.reconcile(ON({ available: false })); await settle();
    assert.equal(answers.length, 2, '对不上：新发一个，不复用在途的');
    const [oldDone, newDone] = answers;
    if (order === 'old-first') { oldDone({ open: true, available: true }); await settle(); newDone({ open: true, available: false }); }
    else { newDone({ open: true, available: false }); await settle(); oldDone({ open: true, available: true }); }
    await settle();
    assert.deepEqual(h.access.current(), { visible: true, confirmed: true, available: false }, `${order} 按新的`);
    assert.deepEqual(plain(await stale), { visible: true, confirmed: true, available: false }, `${order} 等旧查询的页面拿到新的结果`);
  }
  // 注销以后：在途的作废，没有新发的就是擦掉以后的样子（原来的规矩不变）
  let release;
  const z = accessWith({ mode: () => new Promise((done) => { release = done; }) });
  const asked = z.access.raw.ensure(); z.access.clear(); release({ open: true, available: true });
  assert.deepEqual(plain(await asked), { visible: false, confirmed: false, available: false });
});

test('Codex 第 13 轮：从 AI 板块点"阅读并同意"——弹窗写明是为了用 AI 解读和条款对照、只能去读两份全文在最后同意；别的入口照旧；同意后的提示不写"关注和纠错"', async () => {
  // 真的 consent.js：服务端说没同意
  const ask = (agreed = null) => {
    const c = consentWith(control(OPEN), { me: () => Promise.resolve({ agreed }) });
    return { c, wxModals: c.modals };
  };
  const a = ask();
  const answer = a.c.mod.ensureConsent({ purpose: 'ai' });
  await settle();
  assert.equal(a.wxModals.length, 1);
  const modal = a.wxModals[0];
  assert.equal(modal.title, '请先阅读并同意');
  assert.match(modal.content, /使用AI 解读和条款对照前，请先阅读《用户协议》和《隐私保护指引》全文（里面有 AI 功能的约定），在全文最后同意/);
  assert.equal(modal.confirmText, '阅读全文', '不能直接点同意'); assert.equal(modal.cancelText, '暂不');
  assert.doesNotMatch(modal.content, /关注和纠错/);
  assert.equal(await answer, false);
  modal.success({ confirm: true });
  assert.deepEqual(a.c.navigations, ['/pages/agreement/index?consent=1&for=ai'], '点"阅读全文"去两份全文，带上 for=ai');
  // 别的入口（关注、纠错）照旧：写"关注和纠错"，可以直接同意
  const d = ask();
  d.c.mod.ensureConsent(); await settle();
  assert.equal(d.wxModals[0].confirmText, '同意'); assert.match(d.wxModals[0].content, /使用关注和纠错前/);
  // 协议更新过、从 AI 板块来：标题写已更新，用途写 AI 解读和条款对照
  const u = ask({ version: '2026-09', valid: true });
  u.c.mod.ensureConsent({ purpose: 'ai' }); await settle();
  assert.equal(u.wxModals[0].title, '协议已更新'); assert.match(u.wxModals[0].content, /再使用AI 解读和条款对照/);
  u.wxModals[0].success({ confirm: true }); assert.deepEqual(u.c.navigations, ['/pages/agreement/index?consent=1&for=ai']);
  // 别的入口点"先看协议"：去两份全文，不带 for=ai
  d.wxModals[0].success({ confirm: false }); assert.deepEqual(d.c.navigations, ['/pages/agreement/index?consent=1']);
  // AI 首页、解读页、对照页都带上 purpose
  const h = hubPage({ caps: OFF('consent') }); h.page.onShow(); await settle();
  h.page.consent(); assert.deepEqual(plain(h.calls.consentOpts), { purpose: 'ai' });
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    let opts = null;
    const deps = {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(OFF('consent')) },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      '../../utils/consent.js': { ensureConsent: (o) => { opts = o; return Promise.resolve(false); } },
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, getStorageSync: () => '', setStorageSync() {} } });
    p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
    assert.equal(p.data.gate, 'consent'); p.consent(); assert.deepEqual(plain(opts), { purpose: 'ai' }, page);
    p.onHide();
  }
  // 协议全文页：从 AI 板块来的（for=ai）同意以后提示"可以继续使用了"，不写"关注和纠错可以用了"
  for (const [query, toast] of [[{ consent: '1', for: 'ai' }, '已同意，可以继续使用了'], [{ consent: '1' }, '已同意，关注和纠错可以用了']]) {
    const toasts = []; let marked = 0;
    const page = load('pages/agreement/index.js', {
      '../../content/legal.js': { final: true, version: '2026-10', status: '', disclaimer: '', user: { title: '用户协议', sections: [] }, privacy: { title: '隐私保护指引', sections: [] } },
      '../../content/pay-terms.js': { title: '', text: '' },
      '../../utils/api.js': { agree: () => Promise.resolve({}) },
      '../../utils/consent.js': { consentOpen: () => true, markAgreed: () => { marked += 1; }, loginOpen: () => true, agreeFailed() {}, featureNames: () => '关注和纠错' },
    }, { wx: { setNavigationBarTitle() {}, showToast: (o) => toasts.push(o.title), navigateBack() {} }, setTimeout: () => {} });
    page.onLoad(query); page.agree(); await settle();
    assert.equal(marked, 1); assert.deepEqual(toasts, [toast]);
  }
});

test('Codex 第 15 轮：AI 对这个人开着时，工具中心、我的记录的"阅读并同意"也写明能用上 AI、只能读完两份全文再同意；AI 关着时照旧不提 AI', async () => {
  const gated = () => Promise.resolve(OFF('consent'));
  for (const name of ['advisor', 'advisor-tasks']) {
    for (const [state, expected] of [[OPEN, { purpose: 'tools' }], [{ visible: true, confirmed: false, available: true }, undefined], [CLOSED, undefined]]) {
      let opts = 'not called';
      const page = load(`pages/${name}/index.js`, {
        '../../utils/api.js': { advisorCapabilities: gated, advisorTasks: () => Promise.resolve({ items: [] }) },
        '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/boost.js': require('../utils/boost.js'), '../../utils/ai-access.js': control(state),
        '../../utils/consent.js': { ensureConsent: (o) => { opts = o === undefined ? undefined : plain(o); return Promise.resolve(false); } },
      }, { wx: { setNavigationBarTitle() {} } });
      if (page.onLoad) page.onLoad({});
      page.onShow(); await settle();
      page.consent(); await settle();
      assert.deepEqual(opts, expected, `${name} ${JSON.stringify(state)}`);
      if (page.onUnload) page.onUnload();
    }
  }
  // 真的 consent.js：tools 写"使用工具（含 AI 解读和条款对照）前"，只能去读全文
  const c = consentWith(control(OPEN), { me: () => Promise.resolve({ agreed: null }) });
  c.mod.ensureConsent({ purpose: 'tools' }); await settle();
  assert.match(c.modals[0].content, /^使用工具（含 AI 解读和条款对照）前，请先阅读《用户协议》和《隐私保护指引》全文（里面有 AI 功能的约定），在全文最后同意。$/);
  assert.equal(c.modals[0].confirmText, '阅读全文');
  c.modals[0].success({ confirm: true }); assert.deepEqual(c.navigations, ['/pages/agreement/index?consent=1&for=ai']);
  // 不认的用途：照旧通用弹窗
  const g = consentWith(control(OPEN), { me: () => Promise.resolve({ agreed: null }) });
  g.mod.ensureConsent({ purpose: 'toString' }); await settle();
  assert.equal(g.modals[0].confirmText, '同意');
});

test('Codex 第 15 轮：AI 首页等后台回话时写"正在连接"（连接慢时写"比平时慢一点"），没连上给"重试"和"联系客服"；都没有 AI 字样', async () => {
  const watched = []; const stopped = [];
  const extraApi = { watchStartup: (page) => watched.push(page), stopStartup: (page) => stopped.push(page) };
  // 后台一直没回话
  const h = hubPage({ extraApi });
  h.access.ensure = () => new Promise(() => {});
  h.page.onShow(); await settle();
  assert.equal(watched.length, 1, '显示时跟着"正在连接"提示');
  let root = render('pages/ai/index.wxml', h.page.data);
  assert.match(textOf(root), /正在连接…/); assert.doesNotMatch(textOf(root), /\bAI\b/);
  h.page.setData({ serviceStarting: true });
  root = render('pages/ai/index.wxml', h.page.data);
  assert.match(textOf(root), /正在连接，比平时慢一点，请稍等…/); assert.doesNotMatch(textOf(root), /\bAI\b/);
  h.page.onHide(); assert.equal(stopped.length, 1, '离开时停掉');
  // 没连上：重试和联系客服
  const o = hubPage({ state: { visible: true, confirmed: false, available: true } });
  o.page.onShow(); await settle();
  assert.equal(o.page.data.offline, true);
  root = render('pages/ai/index.wxml', o.page.data);
  assert.match(textOf(root), /暂时连不上服务器，请稍后重试。/); assert.doesNotMatch(textOf(root), /\bAI\b/);
  assert.equal(findAll(root, tapsTo('retry')).length, 1);
  assert.deepEqual(findAll(root, (n) => n.tag === 'contact-button').map((n) => n.attrs.reason), ['offline']);
  // 能力接口那一步没连上（gate 为 offline）：同样给重试和联系客服
  const c = hubPage({ caps: OFF('offline') });
  c.page.onShow(); await settle();
  assert.equal(c.page.data.gate, 'offline');
  const gate = findAll(render('pages/ai/index.wxml', c.page.data), hasClass('gate'))[0];
  assert.equal(findAll(gate, tapsTo('retry')).length, 1);
  assert.deepEqual(findAll(gate, (n) => n.tag === 'contact-button').map((n) => n.attrs.reason), ['offline']);
  // 能力接口还在问：能力区也写"正在连接"
  const w = hubPage({ caps: () => new Promise(() => {}) });
  w.page.onShow(); await settle();
  assert.equal(w.page.data.capsLoading, true);
  const abilities = findAll(render('pages/ai/index.wxml', w.page.data), hasClass('abilities'))[0];
  assert.match(textOf(abilities), /正在连接…/);
  w.page.onUnload && w.page.onUnload();
});

test('Codex 第 16 轮：AI 首页点"重试"马上回到"正在连接"、不留"连不上"，问到之前再点不重复问；问到了照常加载', async () => {
  const h = hubPage({ state: { visible: true, confirmed: false, available: true } });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.offline, true);
  let release; let asked = 0;
  h.access.refresh = () => { asked += 1; return new Promise((done) => { release = done; }); };
  h.page.retry();
  assert.equal(h.page.data.waiting, true); assert.equal(h.page.data.offline, false);
  const out = textOf(render('pages/ai/index.wxml', h.page.data));
  assert.match(out, /正在连接…/); assert.doesNotMatch(out, /连不上/);
  h.page.retry(); assert.equal(asked, 1, '问到之前再点不重复问');
  // 问到了：照常加载
  h.access.set(OPEN); h.access.refresh = () => Promise.resolve(OPEN);
  release(OPEN); await settle();
  assert.equal(h.page.data.ready, true); assert.equal(h.page.data.waiting, false);
  assert.match(textOf(render('pages/ai/index.wxml', h.page.data)), /今天还剩 5 次/);
  let again = 0; h.access.refresh = () => { again += 1; return Promise.resolve(OPEN); };
  h.page.retry(); await settle(); assert.equal(again, 1, '问完以后又能点'); assert.equal(h.page.data.ready, true);
});

test('Codex 第 16 轮：直接打开工具中心、我的记录（还没问过 AI 开没开）时，点"阅读并同意"先问清楚再选弹窗；连点只弹一次', async () => {
  const gated = () => Promise.resolve(OFF('consent'));
  for (const name of ['advisor', 'advisor-tasks']) {
    for (const [mode, expected] of [[{ open: true, available: true }, { purpose: 'tools' }], [{ open: false, available: false }, undefined], [NETWORK, undefined]]) {
      const real = accessWith({ mode, caps: OFF('consent') });
      assert.equal(real.access.current().confirmed, false, '还没问过');
      let asked = 0; let opts = 'not called';
      const page = load(`pages/${name}/index.js`, {
        '../../utils/api.js': { advisorCapabilities: gated, advisorTasks: () => Promise.resolve({ items: [] }) },
        '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/boost.js': require('../utils/boost.js'), '../../utils/ai-access.js': real.access,
        '../../utils/consent.js': { ensureConsent: (o) => { asked += 1; opts = o === undefined ? undefined : plain(o); return Promise.resolve(false); } },
      }, { wx: { setNavigationBarTitle() {} } });
      if (page.onLoad) page.onLoad({});
      page.onShow(); await settle();
      page.consent(); page.consent(); await settle();
      assert.equal(asked, 1, `${name} 连点只弹一次`);
      assert.deepEqual(opts, expected, `${name} ${JSON.stringify(mode)}`);
      page.consent(); await settle(); assert.equal(asked, 2, `${name} 弹完以后又能点`);
      if (page.onUnload) page.onUnload();
    }
  }
});

test('Codex 第 17 轮：上次没连上，离开再回来、后台还没答复时写"正在连接"，不留"连不上"；上次连上了，切回来不闪加载中', async () => {
  const h = hubPage({ state: { visible: true, confirmed: false, available: true } });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.offline, true);
  h.page.onHide();
  h.access.ensure = () => new Promise(() => {});
  h.page.onShow(); await settle();
  assert.equal(h.page.data.waiting, true); assert.equal(h.page.data.offline, false);
  const out = textOf(render('pages/ai/index.wxml', h.page.data));
  assert.match(out, /正在连接…/); assert.doesNotMatch(out, /连不上/);
  // 上次连上了：切回来时不回到"正在连接"那一页（能力区自己写加载中）
  const g = hubPage(); g.page.onShow(); await settle();
  assert.equal(g.page.data.ready, true);
  g.page.onHide(); g.access.ensure = () => new Promise(() => {}); g.page.onShow(); await settle();
  assert.equal(g.page.data.waiting, false); assert.equal(g.page.data.capsLoading, true);
});

// 跨过零点的替身：记下回调，测试自己调
function dayStub() {
  const watched = []; let stopped = 0; let day = '2026-10-19';
  return {
    watched, stopped: () => stopped, today: () => day, watchDay: (page, fn) => watched.push(fn), stopDay: () => { stopped += 1; },
    // 停在页面上跨过零点：日期变了，回调
    turn: () => { day = '2026-10-20'; watched.at(-1)(day); },
    // 页面不在前面时过了零点：只改日期
    nextDay: () => { day = '2026-10-20'; },
  };
}

test('Codex 第 18 轮：我的 AI 记录里有处理中的，停在 AI 首页时过一会儿再问（5、10、15 秒，之后每 15 秒，最多 15 分钟），处理完显示已完成；离开就停', async () => {
  const running = { id: 3, kind: 'interpret', status: 'running', docTitle: '通知3', createdAt: '2026-10-19T09:00:00+08:00' };
  let answer = [running];
  const clock = { now: 1000 * HOUR };
  const h = hubPage({ tasks: () => Promise.resolve({ items: answer.map((t) => ({ ...t })) }), now: () => clock.now });
  h.page.onShow(); await settle();
  assert.match(textOf(render('pages/ai/index.wxml', h.page.data)), /我的 AI 记录.*处理中/);
  assert.deepEqual([...h.timers.values()].map((t) => t.ms), [5000]);
  // 还在处理：间隔变长
  assert.deepEqual(h.runTimers(), [5000]); await settle();
  assert.deepEqual(h.runTimers(), [10000]); await settle();
  assert.deepEqual(h.runTimers(), [15000]); await settle();
  assert.deepEqual([...h.timers.values()].map((t) => t.ms), [15000], '之后每 15 秒');
  // 处理完：显示已完成，不再问
  answer = [{ ...running, status: 'done', hasResult: true }];
  h.runTimers(); await settle();
  assert.match(textOf(render('pages/ai/index.wxml', h.page.data)), /我的 AI 记录.*已完成/);
  assert.equal(h.timers.size, 0, '都处理完了就停');
  // 离开就停
  answer = [running];
  const g = hubPage({ tasks: () => Promise.resolve({ items: answer.map((t) => ({ ...t })) }), now: () => clock.now });
  g.page.onShow(); await settle(); assert.equal(g.timers.size, 1);
  g.page.onHide(); assert.equal(g.timers.size, 0, '离开就停');
  // 最多问 15 分钟
  const k = hubPage({ tasks: () => Promise.resolve({ items: [{ ...running }] }), now: () => clock.now });
  k.page.onShow(); await settle(); assert.equal(k.timers.size, 1);
  clock.now += 15 * 60 * 1000 + 1; k.runTimers(); await settle();
  assert.equal(k.timers.size, 0, '超过 15 分钟不再问');
});

test('Codex 第 18 轮：停在 AI 首页跨过零点，今天还剩几次重新问；解读页、对照页次数用完或全站满了时跨过零点不自己重新开始，写"可以重新开始了"，点了再问', async () => {
  // AI 首页
  let used = true;
  const day = dayStub();
  const h = hubPage({ caps: () => Promise.resolve(used ? ON({ remaining: { interpret: 0, clause: 0 } }) : ON()), daywatch: day });
  h.page.onShow(); await settle();
  assert.match(h.text(), /5 次用完了/);
  used = false; day.turn(); await settle();
  assert.match(h.text(), /今天还剩 5 次/); assert.doesNotMatch(h.text(), /用完了/);
  h.page.onHide(); assert.ok(day.stopped() >= 1, '离开时停掉');
  // 解读页、对照页
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    for (const [extra, gate] of [[{ remaining: { interpret: 0, clause: 0 } }, 'used'], [{ siteFull: true }, 'full']]) {
      let blocked = true; let capsCalls = 0; let created = 0;
      const d = dayStub();
      const saved = new Map([[interpret.CONSENT_KEY, true]]);
      const deps = {
        '../../utils/api.js': {
          advisorCapabilities: () => { capsCalls += 1; return Promise.resolve(blocked ? ON(extra) : ON()); },
          createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 9, status: 'queued' }); }, advisorTask: (id) => Promise.resolve({ id, status: 'queued' }),
        },
        '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
        '../../utils/daywatch.js': d,
      };
      if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
      const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) }, setTimeout: () => 0, clearTimeout() {} });
      p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
      // 断言失败时也要停掉解读的轮询（它用真的定时器），不然测试进程退不出去
      try {
        assert.equal(p.data.gate, gate);
        blocked = false; const before = capsCalls;
        d.turn(); await settle();
        assert.equal(capsCalls, before, `${page} ${gate} 跨过零点不自己重新问`); assert.equal(created, 0, '不自己提交');
        const root = render(`pages/${page}/index.wxml`, p.data);
        assert.match(textOf(root), /已经过了 0 点，可以重新开始了。/);
        assert.equal(findAll(root, (n) => n.tag === 'button' && textOf(n).trim() === '重新开始').length, 1);
        assert.equal(findAll(root, tapsTo('records')).length, 0, '不再说"看我的记录"');
        p.reload(); await settle();
        assert.equal(capsCalls, before + 1, '点了再问'); assert.equal(p.data.aiEnabled, true, `${page} ${gate} 可以用了`);
      } finally {
        p.onHide();
      }
    }
    // 别的情况（暂时不可用）跨过零点不变
    const d = dayStub();
    const deps = {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON({ available: false })) },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      '../../utils/daywatch.js': d,
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, getStorageSync: () => '', setStorageSync() {} } });
    p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
    d.turn(); assert.notEqual(p.data.gate, 'restart'); assert.equal(p.data.gateText, 'AI 暂时不可用，请稍后再试。');
    p.onHide();
  }
});

test('Codex 第 19、21 轮：解读页、对照页次数用完或全站满了以后离开（锁屏），第二天回来先写"现在可以重新开始了"，不开始也不提交，点了才开始；当天回来、暂时不可用照常', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    for (const [extra, gate] of [[{ remaining: { interpret: 0, clause: 0 } }, 'used'], [{ siteFull: true }, 'full'], [{ available: false }, 'paused']]) {
      let blocked = true; let capsCalls = 0; let created = 0;
      const d = dayStub();
      const saved = new Map([[interpret.CONSENT_KEY, true]]);
      const deps = {
        '../../utils/api.js': {
          advisorCapabilities: () => { capsCalls += 1; return Promise.resolve(blocked ? ON(extra) : ON()); },
          createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 9, status: 'queued' }); }, advisorTask: (id) => Promise.resolve({ id, status: 'queued' }),
        },
        '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
        '../../utils/daywatch.js': d,
      };
      if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
      const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
      p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
      try {
        assert.equal(p.data.gate, gate);
        // 当天回来：照常再问，还是拦着
        p.onHide(); p.onShow(); await settle();
        assert.equal(capsCalls, 2, `${page} ${gate} 当天回来照常再问`); assert.equal(p.data.gate, gate);
        // 锁屏过了一夜再回来
        p.onHide(); d.nextDay(); blocked = false;
        p.onShow(); await settle();
        if (gate === 'paused') {
          assert.equal(capsCalls, 3, `${page} 暂时不可用照常再问`); assert.notEqual(p.data.gate, 'restart');
          continue;
        }
        assert.equal(capsCalls, 3, `${page} ${gate} 第二天回来照常问`); assert.equal(created, 0, '不提交');
        assert.equal(p.data.gate, 'restart'); assert.equal(p.data.aiEnabled, false, '不开始');
        const root = render(`pages/${page}/index.wxml`, p.data);
        assert.match(textOf(root), /现在可以重新开始了。/);
        assert.equal(findAll(root, (n) => n.tag === 'button' && textOf(n).trim() === '重新开始').length, 1);
        // 点了才开始
        p.reload(); await settle();
        assert.equal(capsCalls, 4); assert.equal(p.data.aiEnabled, true, `${page} ${gate} 点了能用`);
        // 再离开回来：不再拦（已经点过了）
        p.onHide(); p.onShow(); await settle();
        assert.notEqual(p.data.gate, 'restart'); assert.equal(capsCalls, 5); assert.equal(p.data.aiEnabled, true);
      } finally {
        p.onHide();
      }
    }
  }
});

test('Codex 第 19 轮：AI 首页"我的 AI 记录"从进来那一刻算最多问 15 分钟——跨零点重新加载不重新算起，落在 15 分钟以外的不再安排', async () => {
  const running = { id: 3, kind: 'interpret', status: 'running', docTitle: '通知3' };
  const clock = { now: 1000 * HOUR };
  const start = clock.now;
  // 快到 15 分钟时：下一次会落在 15 分钟以外，不再安排
  const k = hubPage({ tasks: () => Promise.resolve({ items: [{ ...running }] }), now: () => clock.now });
  k.page.onShow(); await settle(); assert.equal(k.timers.size, 1);
  clock.now = start + 15 * 60 * 1000 - 2000; k.runTimers(); await settle();
  assert.equal(k.timers.size, 0, '下一次落在 15 分钟以外就不安排');
  // 跨零点：重新加载，但照旧从进来那一刻算
  clock.now = start;
  const day = dayStub();
  const h = hubPage({ tasks: () => Promise.resolve({ items: [{ ...running }] }), now: () => clock.now, daywatch: day });
  h.page.onShow(); await settle(); assert.equal(h.timers.size, 1);
  clock.now = start + 10 * 60 * 1000; day.turn(); await settle();
  assert.equal(h.timers.size, 1, '跨零点后照旧问');
  clock.now = start + 15 * 60 * 1000 - 1000; h.runTimers(); await settle();
  assert.equal(h.timers.size, 0, '到 15 分钟就停，不因为跨零点重新算');
});

test('Codex 第 20、21 轮：被拦下以后，过了 0 点才回话、当天额度调高了，都先写"现在可以重新开始了"不开始；点了以后照常，第二天回来也照常', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    let blocked = true; let capsCalls = 0; let created = 0; let hold = null;
    const d = dayStub();
    const saved = new Map([[interpret.CONSENT_KEY, true]]);
    const deps = {
      '../../utils/api.js': {
        advisorCapabilities: () => {
          capsCalls += 1;
          // 回话的内容按回话那一刻算（过了 0 点才回话时已经能用了）
          const value = () => (blocked ? ON({ remaining: { interpret: 0, clause: 0 } }) : ON());
          if (hold) { const wait = hold; hold = null; return wait.then(value); }
          return Promise.resolve(value());
        },
        createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 9, status: 'queued' }); }, advisorTask: (id) => Promise.resolve({ id, status: 'queued' }),
      },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      '../../utils/daywatch.js': d,
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
    p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
    try {
      assert.equal(p.data.gate, 'used');
      // 23:59 再进来，回话时已经过了 0 点（这时已经能用了）
      p.onHide();
      let release; hold = new Promise((done) => { release = done; });
      p.onShow(); await settle();
      blocked = false; d.nextDay(); release(); await settle();
      assert.equal(p.data.gate, 'restart', `${page} 先写可以重新开始了`); assert.equal(p.data.aiEnabled, false); assert.equal(created, 0, '不提交');
      p.reload(); await settle(); assert.equal(p.data.aiEnabled, true, `${page} 点了才开始`);
    } finally {
      p.onHide();
    }
  }
  // 当天又能用了（比如额度调高）：也先写可以重新开始了；点了以后照常，第二天回来照常
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    let blocked = true; let capsCalls = 0;
    const d = dayStub();
    const saved = new Map([[interpret.CONSENT_KEY, true]]);
    const deps = {
      '../../utils/api.js': {
        advisorCapabilities: () => { capsCalls += 1; return Promise.resolve(blocked ? ON({ remaining: { interpret: 0, clause: 0 } }) : ON()); },
        createAdvisorTask: () => Promise.resolve({ id: 9, status: 'queued' }), advisorTask: (id) => Promise.resolve({ id, status: 'queued' }),
      },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      '../../utils/daywatch.js': d,
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
    p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
    try {
      assert.equal(p.data.gate, 'used');
      blocked = false; p.onHide(); p.onShow(); await settle();
      assert.equal(p.data.gate, 'restart', `${page} 当天又能用了也先写可以重新开始了`); assert.equal(p.data.aiEnabled, false);
      assert.match(textOf(render(`pages/${page}/index.wxml`, p.data)), /现在可以重新开始了。/);
      p.reload(); await settle(); assert.equal(p.data.aiEnabled, true, `${page} 点了能用`);
      p.onHide(); d.nextDay(); const before = capsCalls;
      p.onShow(); await settle();
      assert.notEqual(p.data.gate, 'restart', `${page} 第二天回来不被旧记录挡住`); assert.equal(capsCalls, before + 1); assert.equal(p.data.aiEnabled, true);
    } finally {
      p.onHide();
    }
  }
});

test('Codex 第 21 轮：0 点以前的"用完了""满了"答复 0 点以后才到，之后再进来能用了也先写"可以重新开始了"；跨零点重新开始、AI 首页跨零点都不用在途的旧答复', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    for (const extra of [{ remaining: { interpret: 0, clause: 0 } }, { siteFull: true }]) {
      let blocked = true; let created = 0; let forgot = 0; let hold = null;
      const d = dayStub();
      const saved = new Map([[interpret.CONSENT_KEY, true]]);
      const deps = {
        '../../utils/api.js': {
          advisorCapabilities: () => {
            // 答复的内容在发出时就定了（0 点以前生成），可能晚到
            const value = blocked ? ON(extra) : ON();
            if (hold) { const wait = hold; hold = null; return wait.then(() => value); }
            return Promise.resolve(value);
          },
          forgetCapabilities: () => { forgot += 1; },
          createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 9, status: 'queued' }); }, advisorTask: (id) => Promise.resolve({ id, status: 'queued' }),
        },
        '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
        '../../utils/daywatch.js': d,
      };
      if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
      const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
      // 23:59 打开，答复（用完了）0 点以后才到
      let release; hold = new Promise((done) => { release = done; });
      p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
      try {
        d.nextDay(); release(); await settle();
        assert.equal(p.data.aiEnabled, false);
        // 当天再进来，已经能用了：先写可以重新开始了，不自己开始
        blocked = false; p.onHide(); p.onShow(); await settle();
        assert.equal(p.data.gate, 'restart', `${page} 晚到的旧答复以后也先写可以重新开始了`); assert.equal(created, 0, '不提交');
        // 点"重新开始"：在途的、缓存的能力答复都不用
        const before = forgot;
        p.reload(); await settle();
        assert.ok(forgot > before, `${page} 重新开始时不用旧答复`); assert.equal(p.data.aiEnabled, true);
      } finally {
        p.onHide();
      }
    }
  }
  // AI 首页跨零点：先丢掉能力接口在途的、缓存的答复，再重新问
  let forgot = 0;
  const day = dayStub();
  const h = hubPage({ daywatch: day, extraApi: { forgetCapabilities: () => { forgot += 1; } } });
  h.page.onShow(); await settle();
  const asked = h.calls.caps; const forgotBefore = forgot;
  day.turn(); await settle();
  assert.equal(forgot, forgotBefore + 1, 'AI 首页跨零点不用旧答复'); assert.equal(h.calls.caps, asked + 1);
});

test('Codex 第 22 轮：跨零点时还在等能力接口回话就丢掉旧的重新问；每次显示都不用在途的旧答复；"可以重新开始了"那一页只有"重新开始"', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    let answers = []; let forgot = 0; let created = 0; const holds = [];
    const d = dayStub();
    const saved = new Map([[interpret.CONSENT_KEY, true]]);
    const deps = {
      '../../utils/api.js': {
        advisorCapabilities: () => new Promise((done) => holds.push(done)),
        forgetCapabilities: () => { forgot += 1; },
        createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 9, status: 'queued' }); }, advisorTask: (id) => Promise.resolve({ id, status: 'queued' }),
      },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      '../../utils/daywatch.js': d,
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
    p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
    try {
      assert.equal(forgot, 1, `${page} 显示时不用在途的旧答复`); assert.equal(holds.length, 1);
      // 还在等回话就过了 0 点：丢掉旧的、重新问；旧的（用完了）晚到也不用
      d.turn(); await settle();
      assert.equal(holds.length, 2, `${page} 跨零点还在等就重新问`); assert.equal(forgot, 2);
      holds[1](ON({ remaining: { interpret: 0, clause: 0 } })); await settle();
      assert.equal(p.data.gate, 'used');
      holds[0](ON()); await settle();
      assert.equal(p.data.gate, 'used', `${page} 旧回话不用`);
      // 用完了 → 暂时不可用 → 能用了：只写可以重新开始了，只有"重新开始"一个按钮，没有客服、看原文、看我的记录
      p.onHide(); p.onShow(); await settle(); holds[2](ON({ available: false })); await settle();
      assert.equal(p.data.gate, 'paused');
      p.onHide(); p.onShow(); await settle(); holds[3](ON()); await settle();
      assert.equal(p.data.gate, 'restart'); assert.equal(created, 0);
      const root = render(`pages/${page}/index.wxml`, p.data);
      assert.deepEqual(findAll(root, (n) => n.tag === 'button').map((n) => textOf(n).trim()), ['重新开始'], `${page} 只有重新开始`);
      assert.equal(findAll(root, (n) => n.tag === 'contact-button').length, 0, '没有客服入口');
      assert.match(textOf(root), /现在可以重新开始了。/);
      // 点了才开始
      p.reload(); await settle(); holds[4](ON()); await settle();
      assert.equal(p.data.aiEnabled, true, `${page} 点了才开始`);
    } finally {
      p.onHide();
    }
  }
});

test('Codex 第 23 轮：AI 首页每次显示都不用在途的、缓存的能力答复——请求在途时离开、过了 0 点回来，按新问的显示', async () => {
  let forgot = 0; const holds = [];
  const h = hubPage({ caps: () => new Promise((done) => holds.push(done)), extraApi: { forgetCapabilities: () => { forgot += 1; } } });
  h.page.onShow(); await settle();
  assert.equal(forgot, 1); assert.equal(holds.length, 1);
  // 请求在途时离开，回来：不用旧的，重新问
  h.page.onHide(); h.page.onShow(); await settle();
  assert.equal(forgot, 2, '每次显示都不用旧答复'); assert.equal(holds.length, 2);
  holds[1](ON()); await settle();
  holds[0](ON({ remaining: { interpret: 0, clause: 0 }, siteFull: true })); await settle();
  assert.equal(h.page.data.gate, '', '旧回话不用');
  assert.match(h.text(), /今天还剩 5 次/); assert.doesNotMatch(h.text(), /用完了|今天满了/);
});

test('Codex 第 23 轮：工具中心、我的记录也是每次显示都不用在途的、缓存的能力答复', async () => {
  for (const name of ['advisor', 'advisor-tasks']) {
    let forgot = 0;
    const page = load(`pages/${name}/index.js`, {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON()), advisorTasks: () => Promise.resolve({ items: [] }), advisorStatus: () => Promise.resolve({}), forgetCapabilities: () => { forgot += 1; } },
      '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/boost.js': require('../utils/boost.js'), '../../utils/ai-access.js': control(OPEN),
    }, { wx: { setNavigationBarTitle() {} }, setTimeout: () => 0, clearTimeout() {} });
    if (page.onLoad) page.onLoad({});
    page.onShow(); await settle(); assert.equal(forgot, 1, name);
    page.onHide(); page.onShow(); await settle(); assert.equal(forgot, 2, `${name} 回来再问`);
    if (page.onUnload) page.onUnload();
  }
});

test('Codex 第 24 轮：AI 首页每次显示都作废在途的开放状态答复、重新问——旧答复说"没开着"也不会把人送回首页', async () => {
  const holds = []; let answer = { open: true, available: true };
  const real = accessWith({ mode: () => new Promise((done) => holds.push(done)) });
  const first = real.access.raw.ensure(); holds.shift()(answer); await first;
  assert.deepEqual(real.access.current(), OPEN);
  // 在途一个旧查询（会答"没开着"），这时打开 AI 首页
  real.clock.now += real.access.RECHECK; real.access.raw.ensure(); await settle();
  assert.equal(holds.length, 1);
  const h = hubPage({ realAccess: real.access });
  h.page.onShow(); await settle();
  assert.equal(holds.length, 2, '显示时重新问');
  const [stale, fresh] = holds;
  fresh({ open: true, available: true }); await settle();
  stale({ open: false, available: false }); await settle();
  assert.deepEqual(h.switched, [], '旧答复不把人送回首页');
  assert.equal(h.page.data.ready, true); assert.ok(h.calls.caps >= 1, '能力接口照常问');
  assert.deepEqual(real.access.current(), OPEN);
});

test('Codex 第 24 轮：工具中心停在页面上跨过零点重新问；某一项今天用完了写明天恢复、不给开始的按钮，另一项照常；后台没给次数时不写"0 次"', async () => {
  const page = (caps, day = null) => {
    const p = load('pages/advisor/index.js', {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(typeof caps === 'function' ? caps() : caps), advisorTasks: () => Promise.resolve({ items: [] }), forgetCapabilities: () => { p.forgot = (p.forgot || 0) + 1; } },
      '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/boost.js': require('../utils/boost.js'), '../../utils/ai-access.js': control(OPEN),
      ...(day ? { '../../utils/daywatch.js': day } : {}),
    }, { wx: { setNavigationBarTitle() {}, navigateTo: (o) => { p.urls = (p.urls || []).concat(o.url); } } });
    return p;
  };
  // 解读用完了、对照还有 3 次（从通知进来，带着通知编号）
  const p = page(ON({ remaining: { interpret: 0, clause: 3 } }));
  p.onLoad({ docId: '42', title: '通知' }); p.setData({ context: { docId: '42', title: '通知', label: '通知' } });
  p.onShow(); await settle();
  let root = render('pages/advisor/index.wxml', p.data);
  let out = textOf(root);
  const buttons = (r) => findAll(r, (n) => n.tag === 'button').map((n) => textOf(n).trim());
  assert.match(out, /今天的AI 解读 5 次用完了，明天 0 点恢复/); assert.doesNotMatch(out, /今天还能用 0 次/);
  assert.ok(!buttons(root).includes('开始解读') && !buttons(root).includes('去找通知'), '用完了不给开始的按钮');
  assert.match(out, /今天还能用 3 次/); assert.ok(buttons(root).includes('开始对照'));
  p.interpret(); assert.equal(p.urls, undefined, '用完了点不进去');
  p.clause(); assert.equal(p.urls.length, 1);
  // 反过来：对照用完了、解读还有 2 次
  const q = page(ON({ remaining: { interpret: 2, clause: 0 } }));
  q.onLoad({ docId: '42', title: '通知' }); q.setData({ context: { docId: '42', title: '通知', label: '通知' } });
  q.onShow(); await settle();
  root = render('pages/advisor/index.wxml', q.data); out = textOf(root);
  assert.match(out, /今天的条款对照 5 次用完了，明天 0 点恢复/); assert.ok(!buttons(root).includes('开始对照'), '对照用完了不给开始对照');
  assert.match(out, /今天还能用 2 次/); assert.ok(buttons(root).includes('开始解读'));
  q.clause(); assert.equal(q.urls, undefined, '用完了点不进去');
  // 后台没给次数：不写"今天还能用 0 次"，按钮照常
  const u = page(ON({ remaining: {} }));
  u.onLoad({}); u.onShow(); await settle();
  root = render('pages/advisor/index.wxml', u.data); out = textOf(root);
  assert.doesNotMatch(out, /今天还能用|用完了/); assert.ok(buttons(root).includes('去找通知'));
  // 停在页面上跨过零点：丢掉旧答复、重新问，满了的提示跟着变
  let full = true; const day = dayStub();
  const z = page(() => (full ? ON({ siteFull: true }) : ON()), day);
  z.onLoad({}); z.onShow(); await settle();
  assert.match(textOf(render('pages/advisor/index.wxml', z.data)), /今天 AI 的总用量满了/);
  const forgotBefore = z.forgot;
  full = false; day.turn(); await settle();
  assert.equal(z.forgot, forgotBefore + 1);
  assert.doesNotMatch(textOf(render('pages/advisor/index.wxml', z.data)), /总用量满了/);
  z.onHide(); assert.ok(day.stopped() >= 1, '离开时停掉');
});

test('Codex 第 25 轮：后台给的次数是 null、空串、true/false、字符串时当"不知道"，不当 0——四个页面都不写"用完了"、不拦下', async () => {
  assert.equal(advisor.remainingOf({ remaining: { interpret: 3.7 } }, 'interpret'), 3);
  assert.equal(advisor.remainingOf({ remaining: { interpret: -1 } }, 'interpret'), 0);
  for (const odd of [null, '', false, true, '3', undefined, NaN]) {
    const caps = ON({ remaining: { interpret: odd, clause: odd } });
    assert.equal(advisor.remainingOf(caps.ai_advisor, 'interpret'), null, String(odd));
    // AI 首页
    const h = hubPage({ caps });
    h.page.onShow(); await settle();
    assert.equal(h.page.data.kinds.some((k) => k.out), false, `首页 ${String(odd)}`); assert.equal(h.page.data.interpretOpen, true);
    assert.doesNotMatch(h.text(), /用完了|今天还剩/);
    // 工具中心
    const t = load('pages/advisor/index.js', {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(caps), advisorTasks: () => Promise.resolve({ items: [] }) },
      '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/boost.js': require('../utils/boost.js'), '../../utils/ai-access.js': control(OPEN),
    }, { wx: { setNavigationBarTitle() {} } });
    t.onLoad({}); t.onShow(); await settle();
    assert.equal(t.data.interpretOut, false, `工具中心 ${String(odd)}`); assert.doesNotMatch(textOf(render('pages/advisor/index.wxml', t.data)), /用完了|今天还能用/);
    t.onHide();
    // 解读页、对照页：不拦下
    for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
      const deps = {
        '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(caps), createAdvisorTask: () => Promise.resolve({ id: 9, status: 'queued' }), advisorTask: (id) => Promise.resolve({ id, status: 'queued' }) },
        '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      };
      if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
      const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, getStorageSync: () => '', setStorageSync() {} } });
      p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
      try { assert.notEqual(p.data.gate, 'used', `${page} ${String(odd)}`); assert.equal(p.data.aiEnabled, true); } finally { p.onHide(); }
    }
  }
});

test('Codex 第 25、26 轮：次数用完的提示只写明天恢复，不管有没有记录都不说"以前的结果照样能看"（有没有能看的结果说不准）', async () => {
  const usedUp = ON({ remaining: { interpret: 0, clause: 3 } });
  const done = { id: 3, kind: 'interpret', status: 'done', docTitle: '通知3', hasResult: true };
  const queued = { id: 4, kind: 'interpret', status: 'queued', docTitle: '通知4' };
  const failed = { id: 5, kind: 'clause', status: 'failed', docTitle: '通知5' };
  const batch = { id: 6, kind: 'compare', status: 'done', rowCount: 2, hasFile: true };
  for (const items of [[], [done], [queued, failed], [batch]]) {
    const h = hubPage({ caps: usedUp, tasks: () => Promise.resolve({ items }) });
    h.page.onShow(); await settle();
    assert.match(h.text(), /今天的AI 解读 5 次用完了，明天 0 点恢复。/); assert.doesNotMatch(h.text(), /以前的结果/);
    const t = load('pages/advisor/index.js', {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(usedUp), advisorTasks: () => Promise.resolve({ items }) },
      '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/boost.js': require('../utils/boost.js'), '../../utils/ai-access.js': control(OPEN),
    }, { wx: { setNavigationBarTitle() {} } });
    t.onLoad({}); t.onShow(); await settle();
    const out = textOf(render('pages/advisor/index.wxml', t.data));
    assert.match(out, /今天的AI 解读 5 次用完了，明天 0 点恢复。/); assert.doesNotMatch(out, /以前的结果/);
    t.onHide();
  }
});

test('Codex 第 27 轮：第 5 次提交以后离开再回来（次数变成 0），解读页、对照页接着显示这次任务的进度和结果，不写"用完了"', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    let left = 1; let created = 0; let status = 'queued';
    const saved = new Map([[interpret.CONSENT_KEY, true]]);
    const result = kind === 'clause'
      ? { notice: 'AI 生成，仅供参考', rows: [{ type: '条件', text: '条件原句', refs: [{ pid: 'P001' }], company: [] }] }
      : { notice: 'AI 生成，仅供参考', items: [{ type: '对象', text: '对象原句', refs: [{ pid: 'P001', quote: '对象原句' }] }] };
    const deps = {
      '../../utils/api.js': {
        advisorCapabilities: () => Promise.resolve(ON({ remaining: { interpret: left, clause: left } })),
        createAdvisorTask: () => { created += 1; left = 0; return Promise.resolve({ id: 9, status: 'queued' }); },
        advisorTask: (id) => Promise.resolve(status === 'done' ? { id, status: 'done', docId: 42, docTitle: '通知', result } : { id, status: 'queued' }),
      },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
    p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
    try {
      if (kind === 'clause') { p.submit(); await settle(); }
      assert.equal(created, 1, `${page} 第 5 次提交了`);
      // 离开（锁屏、切走），回来时次数已经是 0，任务也处理完了
      p.onHide(); status = 'done';
      p.onShow(); await settle();
      assert.notEqual(p.data.gate, 'used', `${page} 不按新任务拦下`); assert.equal(p.data.aiEnabled, true);
      assert.equal(p.data.phase, 'done', `${page} 接着显示结果`); assert.equal(created, 1, '不再提交');
      assert.doesNotMatch(textOf(render(`pages/${page}/index.wxml`, p.data)), /用完了/);
    } finally {
      p.onHide();
    }
  }
  // 没提交过的新页面：次数是 0 照旧拦下
  const p = load('pages/advisor-interpret/index.js', {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(ON({ remaining: { interpret: 0 } })), createAdvisorTask: () => Promise.reject(new Error('不该提交')) },
    '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
  }, { wx: { setNavigationBarTitle() {}, getStorageSync: () => true, setStorageSync() {} } });
  p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
  assert.equal(p.data.gate, 'used'); p.onHide();
});

test('Codex 第 28 轮：建任务被明确拒绝不算"这一页有任务"，回来照最新的次数拦下；任务失败后次数变 0，不给会建新任务的"重试""重新填写"，写明原因；没连上的照常能接着试', async () => {
  const make = (page, kind, { create, poll, left }) => {
    const state = { left, created: 0 };
    const saved = new Map([[interpret.CONSENT_KEY, true]]);
    const deps = {
      '../../utils/api.js': {
        advisorCapabilities: () => Promise.resolve(ON({ remaining: { interpret: state.left, clause: state.left } })),
        createAdvisorTask: () => { state.created += 1; return create(state); },
        advisorTask: (id) => poll(id, state),
      },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
    return { p, state };
  };
  const start = async (p, kind) => { p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle(); if (kind === 'clause') { p.submit(); await settle(); } };
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    // 1. 建任务被明确拒绝（后台说暂时处理不了），离开再回来时次数已经是 0：照最新的拦下，不显示上次的失败
    {
      const { p, state } = make(page, kind, { left: 1, create: () => Promise.reject(Object.assign(new Error('AI 暂时不可用，请稍后再试'), { status: 429, code: 'AI_UNAVAILABLE' })), poll: () => Promise.reject(new Error('不该查')) });
      await start(p, kind);
      try {
        assert.equal(p.data.phase, 'failed', `${page} 被拒`);
        p.onHide(); state.left = 0; p.onShow(); await settle();
        assert.equal(p.data.gate, 'used', `${page} 明确被拒不算有任务，照最新的拦下`);
      } finally { p.onHide(); }
    }
    // 2. 任务本身失败，回来时次数是 0：看得到失败，但不给会建新任务的重试，写明原因
    {
      const { p, state } = make(page, kind, { left: 1, create: () => Promise.resolve({ id: 9, status: 'failed', errorCode: '', docId: 42, docTitle: '通知' }), poll: (id) => Promise.resolve({ id, status: 'failed', errorCode: '', docId: 42, docTitle: '通知' }) });
      await start(p, kind);
      try {
        assert.equal(p.data.phase, 'failed', `${page} 失败了`); assert.equal(p.data.canRetry, true, `${page} 能重试 ${JSON.stringify({ error: p.data.error, errorCode: p.data.errorCode })}`);
        p.onHide(); state.left = 0; p.onShow(); await settle();
        assert.equal(p.data.phase, 'failed', `${page} 照样看得到这次的失败`); assert.equal(p.data.newGate, 'used');
        const root = render(`pages/${page}/index.wxml`, p.data);
        assert.equal(findAll(root, tapsTo('retry')).length, 0, `${page} 不给会建新任务的重试`);
        assert.match(textOf(root), /5 次用完了，明天 0 点恢复。/);
        p.retry(); await settle();
        assert.equal(state.created, 1, `${page} 点了也不提交`); assert.equal(p.data.phase, 'failed');
      } finally { p.onHide(); }
    }
    // 3. 没连上（服务端可能已经收下），回来时次数是 0：照常给"重试"，接着用原来的请求编号
    {
      let attempt = 0;
      const finished = { id: 9, status: 'done', docId: 42, docTitle: '通知', result: kind === 'clause' ? { rows: [] } : { items: [] } };
      const { p, state } = make(page, kind, { left: 1, create: () => { attempt += 1; return attempt === 1 ? Promise.reject(Object.assign(new Error('网络不太好'), { retryable: true })) : Promise.resolve({ ...finished }); }, poll: () => Promise.resolve({ ...finished }) });
      await start(p, kind);
      try {
        assert.equal(p.data.phase, 'failed'); assert.equal(p.data.resumable, true);
        p.onHide(); state.left = 0; p.onShow(); await settle();
        assert.notEqual(p.data.gate, 'used', `${page} 没连上算有任务`);
        assert.equal(findAll(render(`pages/${page}/index.wxml`, p.data), tapsTo('retry')).length, 1, `${page} 接着试的重试照常给`);
        p.retry(); await settle();
        assert.equal(p.data.phase, 'done', `${page} 接着试成功`);
      } finally { p.onHide(); }
    }
  }
});

test('Codex 第 29 轮：失败卡片上的重试因为次数用完收起后，停在页面上过了 0 点重新问、按钮回来，不自己提交；对照页从记录打开失败任务"重新填写"后，离开再回来照现在的次数拦下，提交前也拦', async () => {
  for (const [page, kind] of [['advisor-interpret', 'interpret'], ['advisor-clause', 'clause']]) {
    let left = 1; let created = 0;
    const d = dayStub();
    const saved = new Map([[interpret.CONSENT_KEY, true]]);
    const failedTask = { id: 9, status: 'failed', errorCode: '', docId: 42, docTitle: '通知' };
    const deps = {
      '../../utils/api.js': {
        advisorCapabilities: () => Promise.resolve(ON({ remaining: { interpret: left, clause: left } })),
        createAdvisorTask: () => { created += 1; return Promise.resolve({ ...failedTask }); },
        advisorTask: () => Promise.resolve({ ...failedTask }),
      },
      '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
      '../../utils/daywatch.js': d,
    };
    if (kind === 'clause') Object.assign(deps, { '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format });
    const p = load(`pages/${page}/index.js`, deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
    p.onLoad({ docId: '42', title: '通知' }); p.onShow(); await settle();
    try {
      if (kind === 'clause') { p.submit(); await settle(); }
      assert.equal(p.data.phase, 'failed'); assert.equal(created, 1);
      p.onHide(); left = 0; p.onShow(); await settle();
      assert.equal(p.data.newGate, 'used'); assert.equal(findAll(render(`pages/${page}/index.wxml`, p.data), tapsTo('retry')).length, 0);
      // 停在页面上过了 0 点：重新问，按钮回来，不自己提交
      left = 5; d.turn(); await settle();
      assert.equal(p.data.newGate, '', `${page} 过了 0 点重新问`); assert.equal(created, 1, '不自己提交');
      assert.equal(findAll(render(`pages/${page}/index.wxml`, p.data), tapsTo('retry')).length, 1, `${page} 重试回来了`);
    } finally { p.onHide(); }
  }
  // 对照页：从"我的记录"打开失败任务，点"重新填写"回到表单以后，离开再回来照现在的次数拦下
  let left = 5; let created = 0;
  const saved = new Map([[interpret.CONSENT_KEY, true]]);
  const deps = {
    '../../utils/api.js': {
      advisorCapabilities: () => Promise.resolve(ON({ remaining: { interpret: left, clause: left } })),
      createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 10, status: 'queued' }); },
      advisorTask: (id) => Promise.resolve({ id, status: 'failed', errorCode: '', docId: 42, docTitle: '通知' }),
    },
    '../../utils/ai-interpret.js': interpret, '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
    '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format,
  };
  const p = load('pages/advisor-clause/index.js', deps, { wx: { setNavigationBarTitle() {}, navigateTo() {}, getStorageSync: (k) => saved.get(k), setStorageSync: (k, v) => saved.set(k, v) } });
  p.onLoad({ taskId: '7' }); p.onShow(); await settle();
  try {
    assert.equal(p.data.viewing, true); assert.equal(p.data.phase, 'failed'); assert.equal(p.data.canRetry, true);
    p.retry(); await settle();
    assert.equal(p.data.phase, 'form'); assert.equal(p.data.viewing, false, '回到表单就不算打开已有结果');
    p.onHide(); left = 0; p.onShow(); await settle();
    assert.equal(p.data.gate, 'used', '离开再回来照现在的次数拦下'); assert.equal(p.data.aiEnabled, false);
    // 万一表单还在、这时提交会被拦下：不提交，写明原因
    p.setData({ aiEnabled: true, phase: 'form', gate: '', newGate: 'full' });
    p.submit(); await settle();
    assert.equal(created, 0, '不提交'); assert.equal(p.data.gate, 'full'); assert.equal(p.data.aiEnabled, false);
  } finally { p.onHide(); }
});
