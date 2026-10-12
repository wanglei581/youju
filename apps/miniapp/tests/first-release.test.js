const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');

// 首发版（D-061）的截止显示：已核对 / 机器摘录（8 天以后、带原句）/ 截止未核对。依赖都用替身。
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
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  return def ? { ...def, data: plain(def.data), setData(values) { Object.assign(this.data, values); } } : module.exports;
}

test('dueView：已核对写日期和"已核对"；机器摘录要带原句、8 天以后才写日期；其余只写截止未核对', () => {
  const verified = format.dueView({ kind: '企业申报截止', source: 'verified', date: plus(3), time: '17:00', quote: null });
  assert.equal(verified.label, '已核对'); assert.equal(verified.days, 3); assert.equal(verified.tone, 'urgent');
  assert.match(verified.text, /17:00截止 · 剩3天/);
  const excerpt = format.dueView({ kind: '企业申报截止', source: 'excerpt', date: plus(12), time: null, quote: QUOTE });
  assert.equal(excerpt.label, '机器摘录·未经人工核对'); assert.equal(excerpt.quote, QUOTE); assert.equal(excerpt.tone, 'excerpt');
  assert.match(excerpt.text, /剩12天/);
  // 摘录没带原句、或按手机日期已进入 7 天内：不写日期
  for (const due of [
    { source: 'excerpt', date: plus(12), quote: '' },
    { source: 'excerpt', date: plus(7), quote: QUOTE },
    { source: 'excerpt', date: plus(-2), quote: QUOTE },
    { source: 'unverified', date: null, quote: null },
    { source: 'unverified', date: plus(20), quote: QUOTE }, // 即使收到日期也不写
  ]) {
    const view = format.dueView(due);
    assert.equal(view.source, 'unverified'); assert.equal(view.text, '截止未核对，请以原文为准');
    assert.doesNotMatch(JSON.stringify(view), DIGITS);
  }
  assert.equal(format.dueView(null), null);
});

test('列表行：接口带 due 时按来源写；机器摘录另起一行写标签和原句；没有 due 时写见原文', () => {
  const row = load('components/doc-row/index.js', { '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/repost.js': require('../utils/repost.js') });
  const observe = (doc) => { row.observers['doc, q, today'].call(row, { id: '9', title: '关于组织申报的通知', kind: '申报通知', ...doc }, ''); return plain(row.data); };
  const ex = observe({ due: { kind: '企业申报截止', source: 'excerpt', date: plus(15), quote: QUOTE } });
  assert.match(ex.due.text, /剩15天/); assert.equal(ex.dueNote, '');
  assert.deepEqual(ex.excerpt, { label: '机器摘录·未经人工核对', quote: QUOTE });
  const root = render('components/doc-row/index.wxml', { ...ex, doc: { kind: '申报通知', title: 'x' } });
  assert.equal(flat(findAll(root, hasClass('excerpt'))[0]), `机器摘录·未经人工核对原文：${QUOTE}`);
  const ver = observe({ due: { kind: '企业申报截止', source: 'verified', date: plus(5), quote: QUOTE } });
  assert.match(ver.due.text, /剩5天/); assert.equal(ver.excerpt, null);
  const soon = observe({ due: null, dueSoonUnverified: true });
  assert.equal(soon.due, null); assert.equal(soon.dueNote, '截止未核对，请以原文为准');
  const unv = observe({ due: { kind: '企业申报截止', source: 'unverified', date: null } });
  assert.equal(unv.dueNote, '截止未核对，请以原文为准'); assert.equal(unv.excerpt, null);
  assert.equal(observe({ due: null }).dueNote, '截止时间见原文');
  // 旧接口（没有 due 字段）照旧：只认核对过的 deadline
  assert.equal(observe({ deadline: null, deadline_state: 'unverified' }).dueNote, '截止未核对，请以原文为准');
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
    ['企业申报截止', 'excerpt', '机器摘录·未经人工核对'], ['推荐或审核截止', 'verified', '已核对'], ['材料报送截止', 'unverified', ''],
  ]);
  const root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  const rows = findAll(root, hasClass('dl'));
  assert.equal(rows.length, 3);
  assert.match(flat(rows[0]), /^企业申报截止.*17:00剩20天机器摘录·未经人工核对原文：请于2026年10月30日前/);
  assert.match(flat(rows[1]), /^推荐或审核截止.*剩25天已核对$/);
  assert.equal(flat(rows[2]), '材料报送截止截止未核对，请以原文为准');
  assert.doesNotMatch(textOf(rows[2]), DIGITS);
  // 有逐条截止时，不再另出旧的"申报截止"说明卡片
  assert.equal(findAll(root, hasClass('due-card')).length, 1);
});

test('详情页：接口给了空的 deadlines（没抽到候选）写"截止时间见原文"；旧接口照旧', async () => {
  let doc = { ...DOC, deadline_state: 'none', deadlines: [] };
  const page = detailPage(() => doc);
  page.onLoad({ id: '5' }); await flush();
  let root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.deepEqual(findAll(root, hasClass('due-card')).map(flat), ['申报截止截止时间见原文']);
  doc = { ...DOC }; page.refreshing = false; page.load(); await flush();
  assert.equal(page.data.dls, null);
  root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.deepEqual(findAll(root, hasClass('due-card')).map(flat), ['申报截止截止未核对，请以原文为准']);
});

function deadlinePage(api, globals = {}) {
  return load('pages/report/index.js', {
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
    ['1', 'verified', '已核对'], ['2', 'excerpt', '机器摘录·未经人工核对'], ['3', 'unverified', ''],
  ]);
  const root = render('pages/report/index.wxml', page.data);
  const items = findAll(root, hasClass('item'));
  assert.match(flat(items[0]), /企业申报截止.*17:00剩5天已核对/);
  assert.match(flat(items[1]), /剩12天机器摘录·未经人工核对原文：请于2026年10月30日前/);
  assert.match(flat(items[2]), /材料报送截止：截止未核对，请以原文为准/); assert.doesNotMatch(flat(items[2]).replace(/^摘录没带原句的通知/, ''), /剩/);
  const soon = findAll(root, hasClass('soon'))[0];
  assert.match(flat(soon), /^可能7天内截止，日期还没核对2份可能快截止的通知/);
  for (const item of findAll(soon, hasClass('soon-item'))) {
    assert.match(flat(item), /截止未核对，请以原文为准$/); assert.doesNotMatch(flat(item), DIGITS);
  }
  assert.match(textOf(root), /机器摘录仅供参考，以原文为准/);
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
  assert.match(flat(root), /实心点：核对过的截止空心点：机器摘录灰点：已截止/);
  assert.equal(findAll(root, hasClass('filters')).length, 0, '按月看不显示省份、部门筛选');
});

test('首页（D-061）：三个入口；"7 天内截止"只放核对过的、最多 5 条，没有就不显示；另写没核对的份数', async () => {
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
    '../../utils/api.js': { home: () => Promise.resolve(plain(res)) }, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/consent.js': { loginOpen: () => false },
  }, { getApp: () => ({ globalData: {} }), wx: { navigateTo() {} } });
  page.onLoad(); await flush();
  assert.deepEqual(page.data.soon.map((s) => s.id), ['1']);
  let root = render('pages/home/index.wxml', { ...page.data, loading: false });
  const block = findAll(root, hasClass('soon'))[0];
  assert.match(flat(block), /^7天内截止全部甲企业申报截止.*17:00剩2天另有4份可能7天内截止，日期还没核对›$/);
  assert.deepEqual(findAll(root, hasClass('entry-name')).map(textOf), ['截止', '我的客户', '查名单']);
  res = { ...res, dueSoon: [] }; page.lastShow = 0; page.reload(); await flush();
  root = render('pages/home/index.wxml', { ...page.data, loading: false });
  assert.equal(findAll(root, hasClass('soon')).length, 0);
  assert.match(textOf(root), /最新申报通知/);
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
