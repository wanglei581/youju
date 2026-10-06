const test = require('node:test');
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
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
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
  const row = load('components/doc-row/index.js', { '../../utils/format.js': format, '../../utils/copy.js': {} });
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
    '../../utils/api.js': { detail: () => Promise.resolve(JSON.parse(JSON.stringify(doc))) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': {},
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
    '../../utils/api.js': { deadlineReport: () => Promise.resolve({ items: [], total: 0, summary: { 7: 0, 14: 0, 30: 0 }, by_province: {}, verified_total: 0 }) },
    '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { wx: { navigateTo: (r) => { url = r.url; } } });
  page.onLoad(); await flush();
  assert.equal(page.data.verifyingNote, '截止日期正在人工核对中，目前已核对 0 条');
  page.goSearch();
  assert.deepEqual(Object.fromEntries(new URL(url, 'https://test.invalid').searchParams), { kind: '申报通知' });
  page.data.province = '国家部委'; page.data.dept = '科技部门'; page.goSearch();
  assert.deepEqual(Object.fromEntries(new URL(url, 'https://test.invalid').searchParams), { kind: '申报通知', regions: '国家部委', depts: '科技部门' });
  const template = read('pages/report/index.wxml');
  assert.match(template, /{{verifyingNote}}/);
  assert.match(template, /bindtap="goSearch"[^>]*>去搜索申报通知/);
  assert.equal(format.verifyingNote(12), '截止日期正在人工核对中，目前已核对 12 条');
  assert.equal(format.verifyingNote(undefined), '截止日期正在人工核对中');
});

test('申报日历空状态：写已核对条数，给去搜索申报通知的入口；接口透传已核对条数', async () => {
  let url;
  const page = load('pages/calendar/index.js', {
    '../../utils/api.js': { calendar: () => Promise.resolve({ items: [], past: [], verifiedTotal: 3 }) },
    '../../utils/format.js': format, '../../utils/subscribe.js': {},
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/daywatch.js': daywatch,
  }, { wx: { navigateTo: (r) => { url = r.url; } } });
  page.load(); await flush();
  assert.equal(page.data.items.length, 0);
  assert.equal(page.data.verifyingNote, '截止日期正在人工核对中，目前已核对 3 条');
  page.goSearch();
  assert.equal(new URL(url, 'https://test.invalid').searchParams.get('kind'), '申报通知');
  const template = read('pages/calendar/index.wxml');
  assert.match(template, /wx:if="{{!items.length}}"[\s\S]*{{verifyingNote}}[\s\S]*bindtap="goSearch"[^>]*>去搜索申报通知/);
  const api = load('utils/api.js', {
    './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': format,
  }, { wx: { request: ({ success }) => success({ statusCode: 200, data: { total: 0, items: [], pastTotal: 0, past: [], verifiedTotal: 0 } }) } });
  assert.equal((await api.calendar()).verifiedTotal, 0);
});

test('层级筛选：只有国家部委、省级两项，条件带到接口、关注和返回搜索', async () => {
  const calls = [];
  const page = load('pages/search/index.js', {
    '../../utils/api.js': { search: (p) => { calls.push(p); return Promise.resolve({ total: 0, total_all: 0, items: [] }); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': {},
    '../../utils/consent.js': {}, '../../utils/subscribe.js': {}, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: {} });
  page.onLoad({}); await flush();
  const level = page.data.groups.find((g) => g.key === 'level');
  assert.deepEqual(Array.from(level.options), ['国家部委', '省级']);
  assert.equal(level.multi, true);
  assert.equal(calls.at(-1).levels, undefined); // 没选层级时不带，和以前的请求一样
  page.pickOption(event({ group: 'level', option: '省级' })); await flush();
  assert.equal(calls.at(-1).levels, '省级');
  assert.equal(page.data.chipRegion, '省级');
  page.pickOption(event({ group: 'level', option: '国家部委' })); await flush();
  assert.equal(page.conditions().levels, '省级,国家部委');
  page.pickOption(event({ group: 'region', option: '山东' })); await flush();
  assert.equal(page.data.chipRegion, '山东 · 省级、国家部委');
  assert.match(page.followName(), /山东 · 省级、国家部委/);
  // 返回搜索时恢复层级
  const restored = load('pages/search/index.js', {
    '../../utils/api.js': { search: (p) => { calls.push(p); return Promise.resolve({ total: 0, total_all: 0, items: [] }); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': {},
    '../../utils/consent.js': {}, '../../utils/subscribe.js': {}, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: {} });
  const url = new URL(conditions.searchUrl({ kind: 'all', regions: '山东', levels: '省级', time: '不限' }), 'https://test.invalid');
  restored.onLoad(Object.fromEntries(url.searchParams)); await flush();
  assert.equal(calls.at(-1).levels, '省级'); assert.equal(calls.at(-1).regions, '山东');
  assert.equal(conditions.describe({ kind: '申报通知', regions: '山东', levels: '省级', time: '不限' }), '山东 · 省级 · 申报通知 · 不限');
});

test('地区和层级的合并规则：两组取交集，国家部委在两组里是同一类；没有交集时空结果给提示', async () => {
  const c = conditions.levelConflict;
  assert.equal(c('不限地区', '国家部委'), false);
  assert.equal(c('国家部委', '国家部委'), false);
  assert.equal(c('国家部委,山东', '省级'), false);
  assert.equal(c('山东', '省级,国家部委'), false);
  assert.equal(c('山东', ''), false);
  assert.equal(c('山东,北京', '国家部委'), true);
  assert.equal(c('国家部委', '省级'), true);
  // 演示数据的筛选与服务端同一规则
  const docs = [
    { title: 'a', kind: '申报通知', region: '国家', date: '2026-09-01' },
    { title: 'b', kind: '申报通知', region: '山东 · 省级', date: '2026-09-01' },
    { title: 'c', kind: '申报通知', region: '山东 · 市级', date: '2026-09-01' },
  ];
  const titles = (params) => mockSearch(docs, { time: '不限', ...params }).items.map((d) => d.title);
  assert.deepEqual(titles({ regions: '国家部委' }), titles({ levels: '国家部委' }));
  assert.deepEqual(titles({ levels: '省级' }), ['b']);
  assert.deepEqual(titles({ regions: '山东' }), ['b', 'c']);
  assert.deepEqual(titles({ regions: '山东', levels: '省级' }), ['b']);
  assert.deepEqual(titles({ regions: '山东', levels: '国家部委' }), []);
  // 搜索页：没有交集时提示调整；只看未截止没有结果时说明只含核对过的
  const page = load('pages/search/index.js', {
    '../../utils/api.js': { search: () => Promise.resolve({ total: 0, total_all: 0, items: [] }) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': {},
    '../../utils/consent.js': {}, '../../utils/subscribe.js': {}, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: {} });
  page.onLoad({ regions: '山东', levels: '国家部委' }); await flush();
  assert.match(page.data.emptyHint, /地区和层级没有重叠/);
  page.onLoad({ open: '1' }); await flush();
  assert.match(page.data.emptyHint, /只包含截止日期已经人工核对的申报通知/);
  assert.doesNotMatch(page.data.emptyHint, DIGITS);
  page.onLoad({ regions: '山东' }); await flush();
  assert.equal(page.data.emptyHint, '');
  assert.match(read('pages/search/index.wxml'), /wx:if="{{emptyHint}}" class="empty-note">{{emptyHint}}/);
});
