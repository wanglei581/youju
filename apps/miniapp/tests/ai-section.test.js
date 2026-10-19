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
    ensure: () => raw.ensure().then(plain),
    refresh: () => raw.refresh().then(plain),
    lastDoc: () => { const value = raw.lastDoc(); return value ? plain(value) : value; },
    subscribe: (fn) => raw.subscribe((state) => fn(plain(state))),
  });
  return { access, calls, wx, clock };
}

// 页面、组件测试用的"AI 板块状态"替身：set 改状态并通知订阅的页面
function control(initial) {
  let state = Object.assign({}, initial);
  const listeners = new Set();
  const asks = { ensure: 0, refresh: 0 };
  const stored = { doc: null };
  return {
    asks, stored, listeners,
    current: () => Object.assign({}, state),
    subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
    ensure: () => { asks.ensure += 1; return Promise.resolve(Object.assign({}, state)); },
    refresh: () => { asks.refresh += 1; return Promise.resolve(Object.assign({}, state)); },
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
  assert.ok(rowWxml.indexOf('<ai-button') > rowWxml.indexOf('复制原文链接'));
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
  detail = (id) => Promise.resolve({ id, title: '刚才看的那份', ai: { interpret: true, clause: true, reason: null } }) } = {}) {
  const access = control(state); access.stored.last = last;
  const calls = { search: [], caps: 0, consent: 0 };
  const switched = []; const navigations = []; let selected = null;
  const page = load('pages/ai/index.js', {
    '../../utils/api.js': {
      advisorCapabilities: (opts) => { calls.caps += 1; calls.refresh = opts && opts.refresh; return typeof caps === 'function' ? caps() : Promise.resolve(caps); },
      search: (p) => { calls.search.push(p); return search(p); },
      advisorTasks: tasks,
      detail: (id) => { calls.detail = (calls.detail || 0) + 1; return detail(id); },
    },
    '../../utils/ai-access.js': access, '../../utils/advisor.js': advisor, '../../utils/ai-interpret.js': interpret, '../../utils/tool-context.js': context,
    '../../utils/consent.js': { ensureConsent: () => { calls.consent += 1; return Promise.resolve(consentOk); } }, '../../utils/format.js': format,
  }, { wx: { switchTab: (o) => { switched.push(o.url); if (o.complete) o.complete(); }, navigateTo: (o) => navigations.push(o.url) } });
  page.getTabBar = () => ({ setData: (v) => { selected = v.selected; } });
  page.onLoad();
  return { page, access, calls, switched, navigations, selected: () => selected, text: () => textOf(render('pages/ai/index.wxml', page.data)) };
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
  assert.match(text, /今天的条款对照 5 次用完了，明天 0 点恢复；以前的结果在下面“我的 AI 记录”里照样能看。/);
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
  assert.deepEqual(Object.keys(reasons.REASONS).sort(), ['kind', 'list', 'no_body', 'source', 'too_long', 'unavailable']);
  assert.deepEqual(reasons.reasonView('no_body', true), { text: '还没有拿到完整正文，正文可能在附件里。', next: 'attachments', nextText: '看附件' });
  assert.deepEqual(reasons.reasonView('no_body', false), { text: '还没有拿到完整正文，正文可能在附件里。', next: 'copy', nextText: '复制原文链接' });
  assert.deepEqual(reasons.reasonView('whatever', false), { text: '这份文件暂不能解读，请查看原文。', next: 'copy', nextText: '复制原文链接' });
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
      '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/boost.js': require('../utils/boost.js'),
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
      assert.ok(out.includes(`今天的${name} 5 次用完了，明天 0 点恢复；以前的结果在“我的记录”里照样能看。`), out);
      p.records(); assert.deepEqual(urls, ['/pages/advisor-tasks/index']);
      p.onHide();
    }
  }
});
