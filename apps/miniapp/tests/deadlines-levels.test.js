const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const { mockSearch } = require('../utils/mockSearch.js');

// 截止状态说明、空状态说明、层级筛选。依赖都用替身，不读取 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout,
    require(name) { if (name === '../../utils/regions.js') return require('../utils/regions.js'); if (!Object.hasOwn(deps, name) && /ai-access\.js$/.test(name)) return AI_OFF; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  return def ? { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, values); } } : module.exports;
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const event = (dataset) => ({ currentTarget: { dataset } });
const daywatch = { watchDay() {}, stopDay() {} };
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const DIGITS = /[0-9０-９]/;

const UNVERIFIED = { id: '1', kind: '申报通知', title: '关于组织申报的通知', deadline: null, deadline_state: 'unverified' };
const NONE = { id: '2', kind: '申报通知', title: '关于组织申报的通知', deadline: null, deadline_state: 'none' };
const VERIFIED = { id: '3', kind: '申报通知', title: '关于组织申报的通知', deadline_state: 'verified', deadline: { date: '2026-10-18', time: '', verified: true } };

test('截止说明：未核对、无候选、已核对三种情况，未核对时不出现任何日期', () => {
  assert.equal(format.deadlineNote(UNVERIFIED), '截止未核对，请以原文为准');
  assert.equal(format.deadlineNote(NONE), '截止时间见原文');
  assert.equal(format.deadlineNote({ ...NONE, deadline_state: undefined }), '截止时间见原文'); // 旧接口没有这个字段
  assert.equal(format.deadlineNote(VERIFIED), ''); // 有核对过的日期时由 dueInfo 显示
  assert.equal(format.deadlineNote({ ...UNVERIFIED, kind: '政策' }), '');
  // 即使收到没核对的日期，也只给文字
  const leaked = { ...UNVERIFIED, deadline: { date: '2026-11-23', time: '16:30', verified: false } };
  assert.equal(format.dueInfo(leaked.deadline), null);
  assert.doesNotMatch(format.deadlineNote(leaked), DIGITS);
  for (const word of ['最', '保证', '智能', 'AI']) assert.ok(!format.deadlineNote(UNVERIFIED).includes(word));
});

test('列表行：显示截止状态说明，模板不再写死文字', () => {
  const row = load('components/doc-row/index.js', { '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/repost.js': require('../utils/repost.js') });
  const observe = (doc) => { row.observers['doc, q, today'].call(row, doc, ''); return row.data; };
  assert.equal(observe(UNVERIFIED).dueNote, '截止未核对，请以原文为准');
  assert.equal(observe(UNVERIFIED).due, null);
  assert.equal(observe(NONE).dueNote, '截止时间见原文');
  const verified = observe(VERIFIED);
  assert.equal(verified.dueNote, ''); assert.match(verified.due.text, /10月18日/);
  const leaked = observe({ ...UNVERIFIED, deadline: { date: '2026-11-23', verified: false } });
  assert.equal(leaked.due, null); assert.doesNotMatch(leaked.dueNote, DIGITS);
  const template = read('components/doc-row/index.wxml');
  assert.match(template, /wx:if="{{due}}"[\s\S]*wx:elif="{{dueNote}}" class="due">{{dueNote}}</);
  assert.doesNotMatch(template, /截止时间见原文/);
});

test('详情页：未核对写说明不给日期，无候选写见原文', async () => {
  let doc = UNVERIFIED;
  const page = load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { detail: () => Promise.resolve(JSON.parse(JSON.stringify(doc))) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/repost.js': require('../utils/repost.js'),
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { wx: {} });
  page.onLoad({ id: '1' }); await flush();
  assert.equal(page.data.dueNote, '截止未核对，请以原文为准');
  assert.equal(page.data.due, null); assert.equal(page.data.dueDate, '');
  doc = { ...UNVERIFIED, deadline: { date: '2026-11-23', verified: false, label: '企业申报截止' } };
  page.refreshing = false; page.load(); await flush();
  assert.equal(page.data.due, null); assert.equal(page.data.dueDate, ''); assert.doesNotMatch(page.data.dueNote, DIGITS);
  doc = NONE; page.refreshing = false; page.load(); await flush();
  assert.equal(page.data.dueNote, '截止时间见原文');
  doc = VERIFIED; page.refreshing = false; page.load(); await flush();
  assert.equal(page.data.dueNote, ''); assert.equal(page.data.dueDate, '2026年10月18日');
  assert.match(read('pages/detail/index.wxml'), /wx:elif="{{dueNote && !changes.length}}"[\s\S]*{{dueNote}}/);
});

test('截止雷达空状态：写已核对条数，给去搜索申报通知的入口', async () => {
  let url;
  const page = load('pages/report/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': { deadlineReport: () => Promise.resolve({ items: [], total: 0, summary: { 7: 0, 14: 0, 30: 0 }, by_province: {}, verified_total: 0 }) },
    '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { getApp: () => ({ globalData: {} }), wx: { navigateTo: (r) => { url = r.url; } } });
  page.onLoad(); await flush();
  assert.equal(page.data.verifyingNote, '截止日期正在人工核对中，目前已核对 0 条');
  page.goSearch();
  assert.deepEqual(Object.fromEntries(new URL(url, 'https://test.invalid').searchParams), { kind: '申报通知' });
  page.data.province = '国家部委'; page.data.dept = '科技部门'; page.goSearch();
  assert.deepEqual(Object.fromEntries(new URL(url, 'https://test.invalid').searchParams), { kind: '申报通知', levels: '国家部委', depts: '科技部门' });
  page.data.province = '山东'; page.goSearch();
  assert.deepEqual(Object.fromEntries(new URL(url, 'https://test.invalid').searchParams), { kind: '申报通知', regions: '山东', depts: '科技部门' });
  const template = read('pages/report/index.wxml');
  assert.match(template, /{{verifyingNote}}/);
  assert.match(template, /bindtap="goSearch"[^>]*>去搜索申报通知/);
  assert.equal(format.verifyingNote(12), '截止日期正在人工核对中，目前已核对 12 条');
  assert.equal(format.verifyingNote(undefined), '截止日期正在人工核对中');
});

test('截止页按月看的空状态：写已核对条数，给去搜索申报通知的入口；接口透传已核对条数', async () => {
  let url;
  const page = load('pages/report/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': {
      deadlineReport: () => Promise.resolve({ items: [], total: 0, summary: { 7: 0, 14: 0, 30: 0 }, by_province: {}, verified_total: 3 }),
      calendar: () => Promise.resolve({ items: [], past: [], verifiedTotal: 3 }),
    },
    '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { getApp: () => ({ globalData: {} }), wx: { navigateTo: (r) => { url = r.url; } } });
  page.onLoad(); await flush();
  page.pickTab(event({ key: 0 })); await flush();
  assert.equal(page.data.rows.length, 0);
  assert.equal(page.data.verifyingNote, '截止日期正在人工核对中，目前已核对 3 条');
  assert.equal(page.data.emptyTitle, `${page.data.month}月没有核对过或机器摘录的截止日期`);
  page.goSearch();
  assert.equal(new URL(url, 'https://test.invalid').searchParams.get('kind'), '申报通知');
  const template = read('pages/report/index.wxml');
  assert.match(template, /wx:if="{{!rows.length && !loading && !error && !sel}}"[\s\S]*{{verifyingNote}}[\s\S]*bindtap="goSearch"[^>]*>去搜索申报通知/);
  const api = load('utils/api.js', {
    './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': format,
  }, { wx: { request: ({ success }) => success({ statusCode: 200, data: { total: 0, items: [], pastTotal: 0, past: [], verifiedTotal: 0 } }) } });
  assert.equal((await api.calendar()).verifiedTotal, 0);
});

test('按月看只能翻到有数据的月份：近 30 天到一年后，翻出去不误显示"没有截止日期"（10/15 全面测试）', async () => {
  const page = load('pages/report/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': {
      deadlineReport: () => Promise.resolve({ items: [], total: 0, summary: { 7: 0, 14: 0, 30: 0 }, by_province: {}, verified_total: 0 }),
      calendar: () => Promise.resolve({ items: [], past: [], verifiedTotal: 0 }),
    },
    '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { getApp: () => ({ globalData: {} }), wx: {} });
  page.onLoad(); await flush();
  page.pickTab(event({ key: 0 })); await flush();
  const start = page.data.year * 12 + page.data.month;
  assert.equal(page.data.canNext, true);
  for (let i = 0; i < 20; i += 1) page.shiftMonth(event({ step: 1 }));
  const last = page.data.year * 12 + page.data.month;
  assert.ok(last - start >= 11 && last - start <= 12, String(last - start));
  assert.equal(page.data.canNext, false);
  for (let i = 0; i < 20; i += 1) page.shiftMonth(event({ step: -1 }));
  const first = page.data.year * 12 + page.data.month;
  assert.ok(start - first >= 0 && start - first <= 1, String(start - first));
  assert.equal(page.data.canPrev, false);
  assert.match(read('pages/report/index.wxml'), /arrow {{canPrev \? '' : 'arrow--off'}}/);
});

test('层级筛选：国家部委、省级、市级、区县级四项，条件带到接口、关注和返回搜索', async () => {
  const calls = [];
  const page = load('pages/search/index.js', {
    '../../content/legal.js': { final: false },
    '../../utils/api.js': { search: (p) => { calls.push(p); return Promise.resolve({ total: 0, total_all: 0, items: [] }); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': {},
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/subscribe.js': { remindOpen: () => true }, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: {} });
  page.onLoad({}); await flush();
  const level = page.data.groups.find((g) => g.key === 'level');
  assert.deepEqual(Array.from(level.options), ['国家部委', '省级', '市级', '区县级']);
  assert.equal(level.multi, true);
  assert.equal(calls.at(-1).levels, undefined); // 没选层级时不带，和以前的请求一样
  page.pickOption(event({ group: 'level', option: '省级' })); await flush();
  assert.equal(calls.at(-1).levels, '省级');
  assert.equal(page.data.entries.region.on, true); // 入口"地区"管地区和层级
  page.pickOption(event({ group: 'level', option: '国家部委' })); await flush();
  assert.equal(page.conditions().levels, '省级,国家部委');
  page.pickOption(event({ group: 'region', option: '山东' })); await flush();
  // 层级的国家部委不受地区限制：请求的地区里加上国家部委
  assert.equal(page.conditions().regions, '国家部委,山东');
  assert.equal(page.conditions().levels, '省级,国家部委');
  assert.match(page.followName(), /山东 · 省级、国家部委/);
  // 返回搜索时恢复层级
  const restored = load('pages/search/index.js', {
    '../../content/legal.js': { final: false },
    '../../utils/api.js': { search: (p) => { calls.push(p); return Promise.resolve({ total: 0, total_all: 0, items: [] }); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': {},
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/subscribe.js': { remindOpen: () => true }, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: {} });
  const url = new URL(conditions.searchUrl({ kind: 'all', regions: '山东', levels: '省级', time: '不限' }), 'https://test.invalid');
  restored.onLoad(Object.fromEntries(url.searchParams)); await flush();
  assert.equal(calls.at(-1).levels, '省级'); assert.equal(calls.at(-1).regions, '山东');
  assert.equal(conditions.describe({ kind: '申报通知', regions: '山东', levels: '省级', time: '不限' }), '山东 · 省级 · 申报通知 · 不限');
});

test('地区和层级：层级的国家部委不受地区限制，省级只列所选地区的；演示数据与服务端同一规则', async () => {
  const scope = (region, level) => JSON.parse(JSON.stringify(conditions.requestScope(region, level)));
  assert.deepEqual(scope(['不限地区'], []), { regions: '不限地区' });
  assert.deepEqual(scope(['不限地区'], ['国家部委']), { regions: '不限地区', levels: '国家部委' });
  assert.deepEqual(scope(['山东'], []), { regions: '山东' });
  assert.deepEqual(scope(['山东'], ['省级']), { regions: '山东', levels: '省级' });
  assert.deepEqual(scope(['山东', '北京'], ['国家部委']), { regions: '国家部委,山东,北京' });
  assert.deepEqual(scope(['山东'], ['国家部委', '省级']), { regions: '国家部委,山东', levels: '国家部委,省级' });
  // 演示数据的筛选与服务端同一规则（组间取交集）
  const docs = [
    { title: 'a', kind: '申报通知', region: '国家', date: '2026-09-01' },
    { title: 'b', kind: '申报通知', region: '山东 · 省级', date: '2026-09-01' },
    { title: 'c', kind: '申报通知', region: '山东 · 市级', date: '2026-09-01' },
    { title: 'd', kind: '申报通知', region: '北京 · 省级', date: '2026-09-01' },
  ];
  const titles = (region, level) => mockSearch(docs, { time: '不限', ...conditions.requestScope(region, level) }).items.map((d) => d.title);
  assert.deepEqual(titles(['不限地区'], ['国家部委']), ['a']);
  assert.deepEqual(titles(['不限地区'], ['省级']), ['b', 'd']);
  assert.deepEqual(titles(['山东'], []), ['b', 'c']);
  assert.deepEqual(titles(['山东'], ['省级']), ['b']);
  assert.deepEqual(titles(['山东'], ['国家部委']), ['a', 'b', 'c']); // 国家部委 + 山东（包括已收录市级）
  assert.deepEqual(titles(['山东'], ['国家部委', '省级']), ['a', 'b']);
  // 搜索页：地区 + 层级国家部委不再是空结果的冲突；只看未截止没有结果时说明只含核对过的
  const calls = [];
  const page = load('pages/search/index.js', {
    '../../content/legal.js': { final: false },
    '../../utils/api.js': { search: (p) => { calls.push(p); return Promise.resolve({ total: 0, total_all: 0, items: [] }); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': {},
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/subscribe.js': { remindOpen: () => true }, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: {} });
  page.onLoad({ regions: '山东', levels: '国家部委' }); await flush();
  assert.equal(calls.at(-1).regions, '国家部委,山东');
  assert.equal(calls.at(-1).levels, undefined);
  assert.equal(page.data.emptyHint, '');
  page.onLoad({ open: '1' }); await flush();
  // 10/22 走查起：只看未截止的口径写在"去掉'只看未截止'"按钮下面（和筛选面板同一句），这里不再另写一遍
  assert.equal(page.data.emptyHint, '');
  assert.match(page.data.openNote, /没写明截止日期、或截止日期还没理清的不在里面/);
  assert.match(read('pages/search/index.wxml'), /wx:if="{{picked.open.length}}" class="empty-note exit-note">“只看未截止”：{{openNote}}<\/view>/);
  page.onLoad({ regions: '山东' }); await flush();
  assert.equal(page.data.emptyHint, '');
  assert.match(read('pages/search/index.wxml'), /wx:if="{{emptyHint}}" class="empty-note">{{emptyHint}}/);
});

test('截止列表和按月视图均携带省市，换省清城市，未收录城市反馈错误而不扩大查询', async () => {
  const requests = []; let unknown = false; let target;
  const page = load('pages/report/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': {
      deadlineReport: p => { requests.push(['report', p]); return Promise.resolve({ items: [{ id: 1, title: '合成通知', issuer: '合成机关', province: '山东', city: '青岛', date: '2026-12-01' }], total: 1, summary: {}, verified_total: 1 }); },
      calendar: p => { requests.push(['calendar', p]); return unknown ? Promise.reject(new Error('城市未收录')) : Promise.resolve({ items: [], past: [], verifiedTotal: 1 }); },
    },
    '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { getApp: () => ({ globalData: {} }), wx: { navigateTo: r => { target = r.url; } } });
  page.onLoad(); await flush(); page.chooseCity({ detail: { value: ['山东省', '青岛市'] } }); await flush();
  assert.equal(requests.at(-1)[1].province, '山东'); assert.equal(requests.at(-1)[1].city, '青岛');
  assert.match(page.data.rows[0].meta, /山东 · 青岛/);
  page.goSearch(); assert.equal(new URL(target, 'https://test.invalid').searchParams.get('cities'), '山东/青岛');
  page.pickTab(event({ key: 0 })); await flush(); assert.equal(requests.at(-1)[0], 'calendar'); assert.equal(requests.at(-1)[1].city, '青岛');
  page.pickProvince({ detail: { value: page.data.provinces.indexOf('江苏') } }); await flush();
  assert.equal(requests.at(-1)[1].province, '江苏'); assert.equal(requests.at(-1)[1].city, '');
  unknown = true; page.chooseCity({ detail: { value: ['江苏省', '苏州市'] } }); await flush();
  assert.equal(page.data.error, '加载失败：城市未收录'); assert.equal(requests.at(-1)[1].city, '苏州'); assert.equal(page.data.rows.length, 0);
});
