const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const boost = require('../utils/boost.js');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');

// 首发版（D-061）的截止显示：已核对 / 原文摘录（机器摘出，8 天以后、带原句）/ 截止时间见原文。依赖都用替身。
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (v) => JSON.parse(JSON.stringify(v));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const daywatch = { watchDay() {}, stopDay() {} };
const DIGITS = /[0-9０-９]/;
const flat = (node) => textOf(node).replace(/\s+/g, '');
// 北京时间今天往后 n 天
const plus = (n) => new Date(Date.now() + 8 * 3600e3 + n * 86400e3).toISOString().slice(0, 10);
const QUOTE = '请于2026年10月30日前将申报材料报送至市科技局。';

function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, setTimeout, clearTimeout,
    require(name) { if (name === '../../utils/regions.js') return require('../utils/regions.js'); if (name === '../../content/pay-terms.js' && !Object.hasOwn(deps, name)) return require('../content/pay-terms.js'); if (name === '../../utils/pay.js' && !Object.hasOwn(deps, name)) return require('../utils/pay.js'); if (!Object.hasOwn(deps, name) && /ai-access\.js$/.test(name)) return AI_OFF; if (!Object.hasOwn(deps, name) && /(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages; if (!Object.hasOwn(deps, name) && /utils\/advisor\.js$/.test(name)) return require('./helpers/package-pages-default.js').advisor; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  return def ? { ...def, data: plain(def.data), setData(values) { Object.assign(this.data, values); } } : module.exports;
}

test('dueView：已核对写日期和"已核对"；机器摘录要带原句、8 天以后才写日期；其余只写截止未核对', () => {
  const verified = format.dueView({ kind: '企业申报截止', source: 'verified', date: plus(3), time: '17:00', quote: null });
  assert.equal(verified.label, '已人工核对'); assert.equal(verified.days, 3); assert.equal(verified.tone, 'urgent');
  assert.match(verified.text, /17:00截止 · 剩3天/);
  assert.equal(format.dueView({ source: 'excerpt', date: plus(30), quote: QUOTE }).source, 'excerpt');
  assert.equal(format.dueView({ source: 'excerpt', date: plus(8), quote: QUOTE }).source, 'excerpt');
  const excerpt = format.dueView({ kind: '企业申报截止', source: 'excerpt', date: plus(12), time: null, quote: QUOTE });
  assert.equal(excerpt.label, '原文摘录'); assert.equal(excerpt.quote, QUOTE); assert.equal(excerpt.tone, 'excerpt');
  assert.match(excerpt.text, /剩12天/);
  // 摘录没带原句、或按手机日期已进入 7 天内：不写日期
  for (const due of [
    { source: 'excerpt', date: plus(12), quote: '' },
    { source: 'excerpt', date: plus(7), quote: QUOTE },
    { source: 'excerpt', date: plus(-2), quote: QUOTE },
    { source: 'excerpt', date: plus(31), quote: QUOTE }, // 机器摘录只在 8–30 天显示日期
    { source: 'excerpt', date: plus(45), quote: QUOTE },
    { source: 'unverified', date: null, quote: null },
    { source: 'unverified', date: plus(20), quote: QUOTE }, // 即使收到日期也不写
  ]) {
    const view = format.dueView(due);
    assert.equal(view.source, 'unverified'); assert.equal(view.text, '截止时间见原文');
    assert.doesNotMatch(JSON.stringify(view), DIGITS);
  }
  assert.equal(format.dueView(null), null);
});

test('列表行：接口带 due 时按来源写；机器摘录另起一行写标签和原句；没有 due 时写见原文', () => {
  const row = load('components/doc-row/index.js', { '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/repost.js': require('../utils/repost.js') });
  const observe = (doc) => { row.observers['doc, q, today'].call(row, { id: '9', title: '关于组织申报的通知', kind: '申报通知', ...doc }, ''); return plain(row.data); };
  const ex = observe({ due: { kind: '企业申报截止', source: 'excerpt', date: plus(15), quote: QUOTE } });
  assert.match(ex.due.text, /剩15天/); assert.equal(ex.dueNote, '');
  assert.deepEqual(ex.excerpt, { label: '原文摘录', quote: QUOTE });
  const root = render('components/doc-row/index.wxml', { ...ex, doc: { kind: '申报通知', title: 'x' } });
  assert.equal(flat(findAll(root, hasClass('excerpt'))[0]), `原文摘录原文：${QUOTE}`);
  const ver = observe({ due: { kind: '企业申报截止', source: 'verified', date: plus(5), quote: QUOTE } });
  assert.match(ver.due.text, /剩5天/); assert.equal(ver.excerpt, null);
  const soon = observe({ due: null, dueSoonUnverified: true });
  assert.equal(soon.due, null); assert.equal(soon.dueNote, '可能 7 天内截止');
  const unv = observe({ due: { kind: '企业申报截止', source: 'unverified', date: null } });
  assert.equal(unv.dueNote, '截止时间见原文'); assert.equal(unv.excerpt, null);
  assert.equal(observe({ due: null }).dueNote, '截止时间见原文');
  // 旧接口（没有 due 字段）照旧：只认核对过的 deadline
  assert.equal(observe({ deadline: null, deadline_state: 'unverified' }).dueNote, '截止时间见原文');
  // 不是申报通知的不写截止
  assert.equal(observe({ kind: '政策', due: null }).dueNote, '');
});

function detailPage(getDoc) {
  return load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => false },
    '../../utils/api.js': { detail: () => Promise.resolve(plain(getDoc())) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/repost.js': require('../utils/repost.js'),
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { wx: {} });
}
const DOC = { id: '5', kind: '申报通知', title: '关于组织申报的通知', issuer: '某省科学技术厅', date: '2026-10-08', deadline: null, deadline_state: 'unverified', tags: [] };

test('详情页：一份通知几种截止逐条写类型；已核对、机器摘录带原句、截止未核对不写日期', async () => {
  const deadlines = [
    { kind: '企业申报截止', source: 'excerpt', date: plus(20), time: '17:00', remainingDays: 20, quote: QUOTE },
    { kind: '推荐或审核截止', source: 'verified', date: plus(25), time: null, remainingDays: 25, quote: null },
    { kind: '材料报送截止', source: 'unverified', date: null, time: null, remainingDays: null, quote: null },
  ];
  const page = detailPage(() => ({ ...DOC, deadlines }));
  page.onLoad({ id: '5' }); await flush();
  assert.deepEqual(page.data.dls.map((v) => [v.kind, v.source, v.label]), [
    ['企业申报截止', 'excerpt', '原文摘录'], ['推荐或审核截止', 'verified', '已人工核对'], ['材料报送截止', 'unverified', ''],
  ]);
  const root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  const rows = findAll(root, hasClass('dl'));
  assert.equal(rows.length, 3);
  assert.match(flat(rows[0]), /^企业申报截止.*17:00剩20天原文摘录原文：请于2026年10月30日前/);
  assert.match(flat(rows[1]), /^推荐或审核截止.*剩25天已人工核对$/);
  assert.equal(flat(rows[2]), '材料报送截止截止时间见原文');
  assert.doesNotMatch(textOf(rows[2]), DIGITS);
  // 有逐条截止时，不再另出旧的"申报截止"说明卡片
  assert.equal(findAll(root, hasClass('due-card')).length, 1);
});

test('详情页：接口给了空的 deadlines（没抽到候选）写"截止时间见原文"；旧接口照旧', async () => {
  let doc = { ...DOC, deadline_state: 'none', deadlines: [] };
  const page = detailPage(() => doc);
  page.onLoad({ id: '5' }); await flush();
  let root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.deepEqual(findAll(root, hasClass('due-card')).map(flat), ['截止时间见原文']);
  doc = { ...DOC }; page.refreshing = false; page.load(); await flush();
  assert.equal(page.data.dls, null);
  root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.deepEqual(findAll(root, hasClass('due-card')).map(flat), ['截止时间见原文']);
});

function deadlinePage(api, globals = {}) {
  return load('pages/report/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': api, '../../utils/exporter.js': { exportDocs() {} }, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { getApp: () => ({ globalData: {} }), wx: { navigateTo() {} }, ...globals });
}
const REPORT = {
  summary: { 7: 1, 14: 2, 30: 3 }, by_province: { 山东: 3 }, verified_total: 1, total: 3,
  items: [
    { id: '1', title: '核对过的通知', issuer: '某省科学技术厅', province: '山东', label: '企业申报截止', date: plus(5), time: '17:00', remaining_days: 5, source: 'verified', quote: null },
    { id: '2', title: '机器摘录的通知', issuer: '某省工业和信息化厅', province: '山东', label: '企业申报截止', date: plus(12), time: null, remaining_days: 12, source: 'excerpt', quote: QUOTE },
    { id: '3', title: '摘录没带原句的通知', issuer: '某市科技局', province: '山东', label: '材料报送截止', date: plus(20), time: null, remaining_days: 20, source: 'excerpt', quote: '' },
  ],
  unverifiedSoon: { total: 2, items: [{ id: '8', title: '可能快截止的通知', issuer: '某省科学技术厅', province: '山东', kind: '企业申报截止' }, { id: '9', title: '另一份', issuer: '某市科技局', province: '山东', kind: '其他' }] },
};

test('截止页（列表）：已核对、机器摘录分开标，摘录带原句；7 天内没核对的只列标题不写日期；"只看已核对"不请求摘录', async () => {
  const calls = [];
  const page = deadlinePage({ deadlineReport: (params, opts) => { calls.push({ params, opts: opts || {} }); return Promise.resolve(plain(opts && opts.verifiedOnly ? { ...REPORT, items: REPORT.items.slice(0, 1), unverifiedSoon: undefined } : REPORT)); } });
  page.onLoad(); await flush();
  const main = calls.filter((c) => c.params.size === 20);
  assert.equal(main[0].params.days, 7); assert.equal(main[0].opts.verifiedOnly, false);
  assert.ok(calls.every((c) => !('support' in c.params)));
  assert.deepEqual(page.data.rows.map((r) => [r.docId, r.source, r.srcLabel]), [
    ['1', 'verified', '已人工核对'], ['2', 'excerpt', '原文摘录'], ['3', 'unverified', ''],
  ]);
  const root = render('pages/report/index.wxml', page.data);
  const items = findAll(root, hasClass('item'));
  assert.match(flat(items[0]), /企业申报截止.*17:00剩5天已人工核对/);
  assert.match(flat(items[1]), /剩12天原文摘录原文：请于2026年10月30日前/);
  assert.match(flat(items[2]), /材料报送截止：截止时间见原文/); assert.doesNotMatch(flat(items[2]), /请以原文为准/); assert.doesNotMatch(flat(items[2]).replace(/^摘录没带原句的通知/, ''), /剩/);
  const soon = findAll(root, hasClass('soon'))[0];
  assert.match(flat(soon), /^可能7天内截止2份可能快截止的通知/);
  for (const item of findAll(soon, hasClass('soon-item'))) {
    // 卡头写了"可能 7 天内截止"，每条不再各写一遍"截止时间见原文"（10/29 独立审阅）
    assert.doesNotMatch(flat(item), /截止时间见原文/); assert.doesNotMatch(flat(item), DIGITS);
  }
  // 顶栏下常驻的"机器摘录仅供参考，以原文为准"10/29 删掉（每条有标签，页底说明写一次以原文为准）
  // 页底只留去使用帮助的入口（截止日期的显示规则写在帮助里，10/29 创始人）
  assert.doesNotMatch(textOf(root), /机器摘录仅供参考|机器摘录·未经人工核对/); assert.match(textOf(root), /截止日期怎么看 ›/);
  page.toggleVerified({ detail: { value: true } }); await flush();
  assert.equal(calls.at(-1).opts.verifiedOnly, true);
  assert.deepEqual(page.data.rows.map((r) => r.docId), ['1']); assert.equal(page.data.soon.total, 0);
});

test('截止页（按月看）：实心点是核对过的、空心点是只有机器摘录的；"只看已核对"在本机去掉摘录；点日子只看当天', async () => {
  const ym = plus(10).slice(0, 7);
  const ex = plus(10); const ver = plus(12);
  const calendar = { items: [
    { id: 'dl1', docId: '1', due: ver, time: '', verified: true, source: 'verified', title: '核对过', what: '企业申报截止', region: '山东 · 科技厅' },
    // 带 excerpts 的接口：这一条的日期在 date，due 是整份通知的主截止
    { id: 'dl2', docId: '2', date: ex, due: { kind: '企业申报截止', source: 'excerpt', date: ex }, time: '', verified: false, source: 'excerpt', quote: QUOTE, title: '摘录', what: '企业申报截止', region: '山东 · 工信厅' },
    { id: 'dl3', docId: '3', due: plus(9), time: '', verified: false, source: 'excerpt', quote: '', title: '没原句', what: '其他', region: '山东' },
  ], past: [], verifiedTotal: 1, unverifiedSoon: { total: 0, items: [] } };
  const page = deadlinePage({ deadlineReport: () => Promise.resolve(plain(REPORT)), calendar: () => Promise.resolve(plain(calendar)) });
  page.onLoad(); await flush();
  page.pickTab({ currentTarget: { dataset: { key: 0 } } }); await flush();
  const [y, m] = ym.split('-').map(Number);
  page.setData({ year: y, month: m }); page.showMonth();
  const cell = (iso) => page.data.cells.find((c) => c.day === Number(iso.slice(8)));
  if (ex.slice(0, 7) === ym) assert.equal(cell(ex).ex, true);
  if (ver.slice(0, 7) === ym) assert.equal(cell(ver).due, true);
  assert.ok(page.data.rows.every((r) => r.docId !== '3'), '没带原句的摘录不放进日历');
  page.toggleVerified({ detail: { value: true } });
  assert.ok(page.data.rows.every((r) => r.source === 'verified'));
  if (ex.slice(0, 7) === ym) assert.equal(cell(ex).ex, false);
  page.toggleVerified({ detail: { value: false } });
  if (ex.slice(0, 7) === ym) {
    page.pickDay({ currentTarget: { dataset: { day: Number(ex.slice(8)), has: true } } });
    assert.deepEqual(page.data.rows.map((r) => r.docId), ['2']);
  }
  const root = render('pages/report/index.wxml', page.data);
  assert.match(flat(root), /实心点：已核对空心点：原文摘录灰点：已截止/);
  assert.equal(findAll(root, hasClass('filters')).length, 1, '按月看保留省市筛选，隐藏部门筛选');
});

test('首页（D-061）：三个入口；"7 天内截止"日期只放核对过的、最多 5 条；没核对的写份数，一份没核对也显示这一块；都没有才不显示', async () => {
  let res = {
    stats: { week_total: 1, apply: 1, lists: 0, other: 0, provinces: 28, sources: 70 }, items: [],
    dueSoon: [
      { id: '1', title: '甲', label: '企业申报截止', date: plus(2), time: '17:00', source: 'verified' },
      { id: '2', title: '乙', label: '企业申报截止', date: plus(3), time: null, source: 'excerpt', quote: QUOTE },
      { id: '3', title: '丙', label: '推荐或审核截止', date: plus(-1), time: null, source: 'verified' },
    ],
    dueSoonUnverifiedTotal: 4,
  };
  const page = load('pages/home/index.js', {
    '../../utils/boost.js': boost,
    '../../utils/api.js': { home: () => Promise.resolve(plain(res)) }, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/consent.js': { loginOpen: () => false },
  }, { getApp: () => ({ globalData: {} }), wx: { navigateTo() {} } });
  page.onLoad(); await flush();
  assert.deepEqual(page.data.soon.map((s) => s.id), ['1']);
  let root = render('pages/home/index.wxml', { ...page.data, loading: false });
  const block = findAll(root, hasClass('soon'))[0];
  // 卡底只写份数、不单独可点：卡头"全部 ›"已经去截止页（10/29 独立审阅：同一张卡两个入口去同一处）
  assert.match(flat(block), /^7天内截止全部甲企业申报截止.*17:00剩2天另有4份可能7天内截止$/);
  assert.equal(findAll(block, (n) => n.attrs && n.attrs.bindtap === 'goEntry').length, 1);
  assert.deepEqual(findAll(root, hasClass('entry-name')).map(textOf), ['截止', '我的客户', '查名单']);
  // 一份都没核对（10/13 线上就是这样）：照样显示这一块，只写份数、不写日期，字号放大
  res = { ...res, dueSoon: [] }; page.lastShow = 0; page.reload(); await flush();
  root = render('pages/home/index.wxml', { ...page.data, loading: false });
  const only = findAll(root, hasClass('soon'));
  assert.equal(only.length, 1);
  assert.equal(flat(only[0]), '7天内截止全部4份通知可能7天内截止');
  assert.equal(findAll(root, hasClass('soon-foot--only')).length, 1);
  // 核对过的和没核对的都没有，才不显示
  res = { ...res, dueSoon: [], dueSoonUnverifiedTotal: 0 }; page.lastShow = 0; page.reload(); await flush();
  root = render('pages/home/index.wxml', { ...page.data, loading: false });
  assert.equal(findAll(root, hasClass('soon')).length, 0);
  assert.match(textOf(root), /最新申报通知/);
});

test('详情：正文卡片不再另写一行提取说明，标题只写"正文"（10/29 创始人：自动提取这层说明写在使用帮助里）', () => {
  const wxml = read('pages/detail/index.wxml');
  assert.doesNotMatch(wxml, /核对过的日期|body-note|截止日期可先看|一切以原文为准|dueDated/);
  assert.match(wxml, /<view class="section-title">正文<\/view>/); assert.doesNotMatch(wxml, /（自动提取/);
});

test('查名单：比对范围取不到、手里也没有上次的，写明没加载出来并能重试；取到后不再提示', async () => {
  const { wx } = storageWx();
  const board = [];
  const page = load('pages/lists/index.js', {
    '../../utils/api.js': { lists: () => new Promise(() => {}), listBoard: () => new Promise((resolve, reject) => { board.push({ resolve, reject }); }) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/cache.js': realCache(wx),
  }, { getApp: () => ({ globalData: {} }), wx });
  page.loadScope();
  board[0].reject(new Error('网络不太好，请稍后再试')); await flush();
  assert.equal(page.data.scopeError, true);
  let root = render('pages/lists/index.wxml', page.data);
  assert.match(textOf(root), /比对范围没加载出来/);
  page.loadScope();
  assert.equal(page.data.scopeError, false);
  board[1].resolve({ stats: { by_type: [{ type: '其他', lists: 9, entries: 1 }] } }); await flush();
  assert.equal(page.data.scope.lists, 9);
  root = render('pages/lists/index.wxml', page.data);
  assert.doesNotMatch(textOf(root), /没加载出来/);
  // 已经有上次的范围：再失败也不提示，照旧显示上次的
  page.loadScope();
  board[2].reject(new Error('网络不太好，请稍后再试')); await flush();
  assert.equal(page.data.scopeError, false); assert.equal(page.data.scope.lists, 9);
});

test('查名单：连点两次重试、回来的顺序反了（后一次先成功，前一次后失败），不提示没加载出来', async () => {
  const { wx } = storageWx();
  const board = [];
  const page = load('pages/lists/index.js', {
    '../../utils/api.js': { lists: () => new Promise(() => {}), listBoard: () => new Promise((resolve, reject) => { board.push({ resolve, reject }); }) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/cache.js': realCache(wx),
  }, { getApp: () => ({ globalData: {} }), wx });
  page.loadScope(); page.loadScope();
  board[1].resolve({ stats: { by_type: [{ type: '其他', lists: 7, entries: 2 }] } }); await flush();
  board[0].reject(new Error('网络不太好，请稍后再试')); await flush();
  assert.equal(page.data.scope.lists, 7); assert.equal(page.data.scopeError, false);
  assert.doesNotMatch(textOf(render('pages/lists/index.wxml', page.data)), /没加载出来/);
  // 两次都成功、先发的晚回来：只认后发的
  page.loadScope(); page.loadScope();
  board[3].resolve({ stats: { by_type: [{ type: '其他', lists: 8, entries: 3 }] } }); await flush();
  board[2].resolve({ stats: { by_type: [{ type: '其他', lists: 5, entries: 1 }] } }); await flush();
  assert.equal(page.data.scope.lists, 8);
});

test('接口：截止页"只看已核对"不带 excerpts；列表、日历、首页、导出都带 excerpts=1', async () => {
  const urls = [];
  const api = load('utils/api.js', {
    './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': format,
  }, { wx: { request: ({ url, success }) => { urls.push(new URL(url)); success({ statusCode: 200, data: { items: [], total: 0, past: [], pastTotal: 0 } }); } } });
  await api.deadlineReport({ days: 7, province: '', dept: '', page: 1, size: 20 });
  await api.deadlineReport({ days: 7, province: '', dept: '', page: 1, size: 20 }, { verifiedOnly: true });
  await api.calendar();
  assert.deepEqual(urls.map((u) => [u.pathname, u.searchParams.get('excerpts')]), [
    ['/api/v1/reports/deadlines', '1'], ['/api/v1/reports/deadlines', null], ['/api/v1/calendar', '1'],
  ]);
  assert.match(read('utils/api.js'), /const body = \{ ids: ids\.map\(String\), format, excerpts: 1 \};/);
});

test('搜索页（D-061）：外面放地区、时间、状态、部门，类型在页签里，其余收进"筛选"；顶部不再有工具入口', () => {
  const template = read('pages/search/index.wxml');
  const chips = template.slice(template.indexOf('<view class="chips">'), template.indexOf('</view>\n\n', template.indexOf('<view class="chips">')));
  assert.deepEqual([...chips.matchAll(/class="chip [^"]*"[^>]*bindtap="openFilter">([^<]+)</g)].map((m) => m[1]), ['地区', '时间', '状态', '部门']);
  assert.match(chips, /class="filter-link[^"]*" bindtap="openFilter"><t-icon[^>]*\/>筛选<\/view>/);
  assert.doesNotMatch(template, /tool-entry|goTool/);
});

test('评审发现的问题（10/12）：被拦下的摘录在详情不显示原句；截止页手机日期已过写"已截止"；卡片上的"看相关通知"总是只看这一家', async () => {
  // 1. 详情：服务端给的摘录按手机日期已在 7 天内 → 不写日期，也不写原句（原句里就有日期）
  const page = detailPage(() => ({ ...DOC, deadlines: [{ kind: '企业申报截止', source: 'excerpt', date: plus(5), time: null, remainingDays: 8, quote: QUOTE }] }));
  page.onLoad({ id: '5' }); await flush();
  assert.deepEqual(plain(page.data.dls.map((d) => [d.source, d.quote, d.dateText])), [['unverified', '', '']]);
  const root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.equal(findAll(root, hasClass('dl-quote')).length, 0);
  // 2. 截止页：接口还算没截止、手机日期已过的核对过的截止 → "已截止"，不写"剩 -1 天"
  const report = deadlinePage({ deadlineReport: () => Promise.resolve(plain({ ...REPORT, items: [{ ...REPORT.items[0], date: plus(-1), time: null }], unverifiedSoon: undefined })) });
  report.onLoad(); await flush();
  const item = findAll(render('pages/report/index.wxml', report.data), hasClass('item'))[0];
  assert.match(flat(item), /已截止/); assert.doesNotMatch(flat(item), /剩-1天/);
  assert.equal(report.data.rows[0].tone, 'past');
});

test('首页先显示上次存在本机的数据（10/12 冷启动诊断）：取新的成功就换掉并存下；失败留着上次的、写明没更新；超过 3 天的不用', async () => {
  const stored = {};
  const local = {
    getStorageSync: (k) => (stored[k] ? plain(stored[k]) : ''), setStorageSync: (k, v) => { stored[k] = plain(v); }, navigateTo() {},
  };
  let next;
  const cacheModule = () => {
    const module = { exports: {} };
    vm.runInNewContext(read('utils/cache.js'), { module, wx: local, require: () => format });
    return module.exports;
  };
  const homePage = () => load('pages/home/index.js', {
    '../../utils/boost.js': boost,
    '../../utils/api.js': { home: () => new Promise((resolve, reject) => { next = { resolve, reject }; }) },
    '../../utils/cache.js': cacheModule(),
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/consent.js': { loginOpen: () => false },
  }, { getApp: () => ({ globalData: {} }), wx: local });
  const fresh = { stats: { week_total: 2, provinces: 28, sources: 70, last_checked_at: null }, items: [{ id: '1', kind: '申报通知', title: '新的' }],
    dueSoon: [{ id: '9', title: '核对过的', label: '企业申报截止', date: plus(3), source: 'verified' }], dueSoonUnverifiedTotal: 4 };
  // 第一次打开：本机没有，显示占位；取到后存下
  let page = homePage(); page.onLoad();
  assert.equal(page.data.loaded, false);
  next.resolve(plain(fresh)); await flush(); await flush();
  assert.equal(page.data.items[0].title, '新的'); assert.equal(page.data.cachedText, '');
  assert.ok(stored.youjuHomeCacheV1 && stored.youjuHomeCacheV1.data.items[0].title === '新的');
  // 再打开：还没回来就先显示上次的，写明是上次的
  page = homePage(); page.onLoad();
  assert.equal(page.data.loaded, true); assert.equal(page.data.items[0].title, '新的');
  assert.match(page.data.cachedText, /^上次更新 /); assert.deepEqual(page.data.soon.map((s) => s.id), ['9']);
  let root = render('pages/home/index.wxml', page.data);
  assert.match(textOf(root), /下面是上次更新 .*的数据，正在更新…/);
  assert.equal(findAll(root, (n) => n.attrs && n.attrs['aria-label'] === '正在加载').length, 0);
  // 取新的失败：留着上次的，写明没更新
  next.reject(new Error('网络不太好，请稍后再试')); await flush(); await flush();
  assert.equal(page.data.items[0].title, '新的'); assert.match(page.data.error, /数据未更新/);
  root = render('pages/home/index.wxml', page.data);
  assert.match(textOf(root), /下面是上次更新 .*的数据/); assert.doesNotMatch(textOf(root), /正在更新…/);
  // 超过 3 天的不用：显示占位，失败时是空的
  stored.youjuHomeCacheV1.at = Date.now() - 4 * 86400000;
  page = homePage(); page.onLoad();
  assert.equal(page.data.loaded, false);
  next.reject(new Error('网络不太好，请稍后再试')); await flush(); await flush();
  assert.equal(page.data.items.length, 0); assert.match(page.data.error, /数据未更新/);
});

function storageWx() {
  const stored = {};
  return { stored, wx: { getStorageSync: (k) => (stored[k] ? plain(stored[k]) : ''), setStorageSync: (k, v) => { stored[k] = plain(v); }, navigateTo() {} } };
}
function realCache(wxStub) {
  const module = { exports: {} };
  vm.runInNewContext(read('utils/cache.js'), { module, wx: wxStub, require: () => format });
  return module.exports;
}

test('截止页先显示上次存在本机的默认视图（7 天内、不筛选）：截止按手机当天重新算；取新的失败留着上次的；换了页签不用缓存', async () => {
  const { stored, wx } = storageWx();
  stored.youjuDeadlineCacheV1 = { at: Date.now() - 3600e3, data: {
    summary: { 7: 1, 14: 2, 30: 3 }, total: 1, verified_total: 1,
    items: [
      { id: '1', title: '核对过', issuer: '某厅', province: '山东', label: '企业申报截止', date: plus(5), source: 'verified' },
      // 存的时候是 8 天以后的机器摘录，按今天算已进入 7 天内：不写日期，也不写原句
      { id: '3', title: '摘录', issuer: '某厅', province: '山东', label: '企业申报截止', date: plus(6), source: 'excerpt', quote: QUOTE },
    ],
    unverifiedSoon: { total: 2, items: [{ id: '8', title: '没核对', issuer: '某厅', province: '山东', kind: '企业申报截止' }] },
  } };
  let next; const calls = [];
  const page = load('pages/report/index.js', {
    '../../utils/api.js': { deadlineReport: (p, o) => { calls.push(p); return new Promise((resolve, reject) => { if (p.size === 20) next = { resolve, reject }; }); } },
    '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/cache.js': realCache(wx),
  }, { getApp: () => ({ globalData: {} }), wx });
  page.onLoad();
  // 还没回来：先显示上次的，份数也照上次的写，不写"—"
  assert.deepEqual(plain(page.data.rows.map((r) => [r.docId, r.source, r.days, r.quote])), [['1', 'verified', 5, ''], ['3', 'unverified', null, '']]);
  assert.match(page.data.cachedText, /^上次更新 /); assert.equal(page.data.loading, true);
  let root = render('pages/report/index.wxml', page.data);
  assert.match(flat(root), /下面是上次更新.*的数据，正在更新…/);
  assert.equal(flat(findAll(root, hasClass('tab'))[0]), '17天内');
  assert.equal(findAll(root, hasClass('soon')).length, 1);
  // 失败：留着上次的
  next.reject(new Error('网络不太好，请稍后再试')); await flush();
  assert.equal(page.data.rows.length, 2); assert.equal(page.data.error, '网络不太好，请稍后再试');
  root = render('pages/report/index.wxml', page.data);
  assert.match(flat(root), /下面是上次更新.*的数据/); assert.doesNotMatch(flat(root), /正在更新…/);
  // 重试成功：换成新的并存下
  page.retry();
  next.resolve(plain({ ...REPORT, items: [REPORT.items[1]] })); await flush();
  assert.equal(page.data.cachedText, ''); assert.deepEqual(page.data.rows.map((r) => r.docId), ['2']);
  assert.deepEqual(stored.youjuDeadlineCacheV1.data.items.map((i) => i.id), ['2']);
  // 换到 14 天：不用缓存，先清空再加载
  page.pickTab({ currentTarget: { dataset: { key: 14 } } });
  assert.equal(page.data.cachedText, ''); assert.equal(page.data.rows.length, 0);
});

test('查名单先显示上次存在本机的最近名单和比对范围；取新的失败写明这次没更新成功', async () => {
  const { stored, wx } = storageWx();
  stored.youjuListsCacheV1 = { at: Date.now() - 600e3, data: { week_total: 3, total: 3, items: [{ id: '5', title: '专精特新名单' }] } };
  stored.youjuListScopeV1 = { at: Date.now() - 600e3, data: { lists: 9, byType: [{ type: '其他', lists: 9, entries: 1 }], byProvince: [], byYear: [] } };
  let next;
  const page = load('pages/lists/index.js', {
    '../../utils/api.js': { lists: () => new Promise((resolve, reject) => { next = { resolve, reject }; }), listBoard: () => new Promise(() => {}) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/cache.js': realCache(wx),
  }, { getApp: () => ({ globalData: {} }), wx });
  page.reload();
  assert.equal(page.data.loading, false); assert.equal(page.data.shown[0].title, '专精特新名单');
  assert.equal(page.data.scope.lists, 9); assert.match(page.data.cachedText, /^上次更新 /);
  next.reject(new Error('网络不太好，请稍后再试')); await flush();
  assert.equal(page.data.staleError, true); assert.ok(!page.data.error);
  assert.match(textOf(render('pages/lists/index.wxml', page.data)), /这次没更新成功/);
  page.reload();
  next.resolve({ week_total: 1, total: 1, items: [{ id: '6', title: '新名单' }] }); await flush();
  assert.equal(page.data.cachedText, ''); assert.equal(page.data.shown[0].title, '新名单');
  assert.equal(stored.youjuListsCacheV1.data.items[0].title, '新名单');
});

test('首屏快照（10/12 冷启动）：只向常驻的 admin 要一次，60 秒内各页共用；取不到返回 null，不重试', async () => {
  const calls = []; let now = 1000; let reply = (o) => o.success({ statusCode: 200, data: { home: { stats: {} }, deadlines: {}, lists: {} } });
  const api = load('utils/api.js', {
    './config.js': { cloudEnv: 'prod-test', cloudService: 'api', devBaseUrl: '', templates: {} },
    '../mock/data.js': {}, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  }, { Date: { now: () => now }, wx: { cloud: { callContainer: (o) => { calls.push([o.path, o.header['X-WX-SERVICE']]); reply(o); } } } });
  const a = await api.snapshotOnce(); const b = await api.snapshotOnce();
  assert.ok(a && a.home); assert.equal(a, b);
  assert.deepEqual(calls, [['/api/v1/snapshot', 'admin']]);
  now += 61000; reply = (o) => o.success({ statusCode: 503, data: {} });
  assert.equal(await api.snapshotOnce(), null);
  assert.equal(calls.length, 2);
  // 演示数据（没配云环境）不取
  const demo = load('utils/api.js', {
    './config.js': { cloudEnv: '', cloudService: '', devBaseUrl: '', templates: {} }, '../mock/data.js': {}, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  }, { wx: {} });
  assert.equal(await demo.snapshotOnce(), null);
});

test('首屏快照：首页、截止页、查名单在实时数据回来前先显示快照，之后实时的失败不报错、成功就换成实时的', async () => {
  const snap = {
    home: { stats: { week_total: 1, provinces: 28, sources: 70 }, items: [{ id: '1', kind: '申报通知', title: '快照里的' }], dueSoon: [], dueSoonUnverifiedTotal: 3 },
    deadlines: { ...plain(REPORT), items: [REPORT.items[1]] },
    lists: { week_total: 2, total: 2, items: [{ id: '7', title: '快照名单' }] },
    scope: { by_type: [{ type: '其他', lists: 5, entries: 9 }], by_province: [], by_year: [] },
  };
  const deferred = () => { let d; const p = new Promise((resolve, reject) => { d = { resolve, reject }; }); return { p, d }; };
  const { stored, wx } = storageWx();
  // 首页
  let live = deferred();
  const home = load('pages/home/index.js', {
    '../../utils/boost.js': boost,
    '../../utils/api.js': { home: () => live.p, snapshotOnce: () => Promise.resolve(plain(snap)) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/consent.js': { loginOpen: () => false },
    '../../utils/cache.js': realCache(wx),
  }, { getApp: () => ({ globalData: {} }), wx });
  home.onLoad(); await flush();
  assert.equal(home.data.items[0].title, '快照里的'); assert.equal(home.data.fresh, true); assert.equal(home.data.cachedText, '');
  assert.equal(stored.youjuHomeCacheV1.data.items[0].title, '快照里的');
  live.d.reject(new Error('网络不太好，请稍后再试')); await flush(); await flush();
  assert.equal(home.data.error, ''); assert.equal(home.data.items[0].title, '快照里的');
  // 截止页
  live = deferred();
  const report = load('pages/report/index.js', {
    '../../utils/api.js': { deadlineReport: (p) => (p.size === 20 ? live.p : new Promise(() => {})), snapshotOnce: () => Promise.resolve(plain(snap)) },
    '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/cache.js': realCache(wx),
  }, { getApp: () => ({ globalData: {} }), wx });
  report.onLoad(); await flush();
  assert.deepEqual(plain(report.data.rows.map((r) => r.docId)), ['2']); assert.equal(report.data.prefilled, true);
  assert.equal(flat(findAll(render('pages/report/index.wxml', report.data), hasClass('tab'))[1]), '214天内');
  live.d.resolve(plain(REPORT)); await flush();
  assert.deepEqual(plain(report.data.rows.map((r) => r.docId)), ['1', '2', '3']); assert.equal(report.data.prefilled, false);
  // 查名单
  live = deferred();
  const lists = load('pages/lists/index.js', {
    '../../utils/api.js': { lists: () => live.p, listBoard: () => new Promise(() => {}), snapshotOnce: () => Promise.resolve(plain(snap)) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/cache.js': realCache(wx),
  }, { getApp: () => ({ globalData: {} }), wx });
  delete stored.youjuListsCacheV1; delete stored.youjuListScopeV1;
  lists.reload(); await flush();
  assert.equal(lists.data.shown[0].title, '快照名单'); assert.equal(lists.data.scope.lists, 5); assert.equal(lists.data.loading, false);
  live.d.reject(new Error('网络不太好，请稍后再试')); await flush();
  assert.ok(!lists.data.error); assert.equal(lists.data.staleError, false);
});

test('实时请求先失败、首屏快照后到：首页和查名单显示快照，并清掉先写上的错误提示（10/18 验收，Codex 指出）', async () => {
  const snap = {
    home: { stats: { week_total: 1, provinces: 28, sources: 70 }, items: [{ id: '1', kind: '申报通知', title: '快照里的' }], dueSoon: [], dueSoonUnverifiedTotal: 0 },
    lists: { week_total: 2, total: 2, items: [{ id: '7', title: '快照名单' }] },
    scope: { by_type: [{ type: '其他', lists: 5, entries: 9 }], by_province: [], by_year: [] },
  };
  const deferred = () => { let d; const p = new Promise((resolve, reject) => { d = { resolve, reject }; }); return { p, d }; };
  const { stored, wx } = storageWx();
  let late = deferred();
  const home = load('pages/home/index.js', {
    '../../utils/boost.js': boost,
    '../../utils/api.js': { home: () => Promise.reject(new Error('网络不太好，请稍后再试')), snapshotOnce: () => late.p },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/consent.js': { loginOpen: () => false },
    '../../utils/cache.js': realCache(wx),
  }, { getApp: () => ({ globalData: {} }), wx });
  home.onLoad(); await flush(); await flush();
  assert.match(home.data.error, /数据未更新/);
  late.d.resolve(plain(snap)); await flush();
  assert.equal(home.data.error, ''); assert.equal(home.data.items[0].title, '快照里的');
  late = deferred();
  const lists = load('pages/lists/index.js', {
    '../../utils/api.js': { lists: () => Promise.reject(new Error('网络不太好，请稍后再试')), listBoard: () => new Promise(() => {}), snapshotOnce: () => late.p },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/cache.js': realCache(wx),
  }, { getApp: () => ({ globalData: {} }), wx });
  delete stored.youjuListsCacheV1; delete stored.youjuListScopeV1;
  lists.reload(); await flush(); await flush();
  assert.match(lists.data.error, /加载失败/);
  late.d.resolve(plain(snap)); await flush();
  assert.ok(!lists.data.error); assert.equal(lists.data.shown[0].title, '快照名单');
});

test('截止核对断档不在首页写（10/29 创始人：这类话不写在页面上；D-061 第八节第 2 条，D-108 待确认）：不管核对过没有、断档多久，首页都不出这一行', async () => {
  assert.equal(format.reviewStaleNote, undefined);
  for (const reviewed of [null, '2026-10-09T18:00:00+08:00', undefined]) {
    const stats = { week_total: 1, provinces: 28, sources: 70 };
    if (reviewed !== undefined) stats.deadline_reviewed_at = reviewed;
    const res = { stats, items: [], dueSoon: [], dueSoonUnverifiedTotal: 4 };
    const page = load('pages/home/index.js', {
      '../../utils/boost.js': boost,
      '../../utils/api.js': { home: () => Promise.resolve(plain(res)) }, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
      '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
      '../../utils/consent.js': { loginOpen: () => false },
    }, { getApp: () => ({ globalData: {} }), wx: { navigateTo() {} } });
    page.onLoad(); await flush();
    const root = render('pages/home/index.wxml', { ...page.data, loading: false });
    assert.equal(findAll(root, hasClass('soon-stale')).length, 0, String(reviewed));
    assert.doesNotMatch(textOf(root), /今日未更新|最近一次核对/, String(reviewed));
    assert.match(textOf(root), /4 份通知可能 7 天内截止/);
  }
});

test('协议页（民法典第 496 条，10/14）：每份文件顶部列出重点条款，正文里对应的段落加粗', () => {
  const legal = require('../content/legal.js');
  const page = load('pages/agreement/index.js', {
    '../../content/legal.js': { ...legal, final: true },
    '../../utils/api.js': { agree: () => Promise.resolve({}) },
    '../../utils/consent.js': { consentOpen: () => true, markAgreed() {}, loginOpen: () => true },
  }, { wx: { setNavigationBarTitle() {}, showToast() {}, navigateBack() {}, switchTab() {} } });
  page.onLoad({ consent: '1' });
  const root = render('pages/agreement/index.wxml', page.data);
  const boxes = findAll(root, hasClass('keybox'));
  assert.equal(boxes.length, 2);
  assert.match(flat(boxes[0]), /请重点阅读.*依法有管辖权的人民法院起诉/);
  const bold = findAll(root, hasClass('sec-p--key')).map((n) => flat(n));
  for (const head of ['3.核对与责任：', '3.账号注销：', '5.上传名单：', '2.若您与我们因本协议', '8.访问日志：', '5.其他请求']) {
    assert.ok(bold.some((t) => t.startsWith(head)), head);
  }
  assert.equal(bold.length, legal.user.keyParagraphs.length + legal.privacy.keyParagraphs.length);
});


test('名单详情显示实际公示或公布状态；缺失状态不编造', () => {
  for (const status of ['公示', '公布']) {
    const tree = render('pages/detail/index.wxml', { doc: { title: '合成名单', status }, loading: false });
    assert.match(textOf(tree), new RegExp(`发布阶段：${status}`));
  }
  assert.doesNotMatch(textOf(render('pages/detail/index.wxml', { doc: { title: '合成通知' }, loading: false })), /发布阶段：/);
});
