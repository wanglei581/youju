const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');

// 仅加载被测代码；所有依赖都用替身，不访问 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let page;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout,
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(def) { page = def; }, ...globals,
  }, { filename: file });
  return page ? { ...page, data: JSON.parse(JSON.stringify(page.data)), setData(values) { Object.assign(this.data, values); } } : module.exports;
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const event = (dataset) => ({ currentTarget: { dataset } });
const daywatch = { watchDay() {}, stopDay() {} };
const app = { getApp: () => ({ globalData: {} }) };
const wxml = (page) => fs.readFileSync(path.join(__dirname, '..', 'pages', page, 'index.wxml'), 'utf8');

function searchPage(api, wx = {}) {
  return load('pages/search/index.js', {
    '../../content/legal.js': { final: false },
    '../../utils/api.js': api, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/exporter.js': {}, '../../utils/consent.js': { ensureConsent: () => Promise.resolve(true) },
    '../../utils/subscribe.js': {}, '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
    '../../utils/conditions.js': conditions,
  }, { wx });
}

test('J-02 保存未截止条件，关注描述和返回搜索完整恢复条件；422 给中文提示', async () => {
  const c = { q: '高企', kind: '申报通知', regions: '北京,辽宁', types: '申报通知', status: '', time: '不限', depts: '科技部门', open: 1 };
  assert.match(conditions.describe(c), /只看未截止/);
  const url = new URL(conditions.searchUrl(c), 'https://test.invalid');
  let saved;
  let message;
  const page = searchPage({ search: () => Promise.resolve({ total: 0, total_all: 0, items: [] }),
    createFollow: (_, value) => { saved = value; return Promise.reject({ status: 422, message: '服务出错了（422）' }); },
  }, { showModal: ({ success }) => success({ confirm: true, content: '关注' }), showToast: ({ title }) => { message = title; } });
  page.onLoad(Object.fromEntries(url.searchParams)); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(page.conditions())), c);
  page.follow(); await flush();
  assert.equal(saved.open, 1);
  assert.match(message, /关注条件或名称不符合要求/);
  assert.doesNotMatch(message, /服务出错|422/);
});

test('J-05 首页返回刷新、保持类型页签，合并在途请求并节流；J-12 带搜索类型', async () => {
  let stamp = 10000;
  let calls = 0;
  let resolve;
  let url;
  const page = load('pages/home/index.js', {
    '../../utils/api.js': { home: () => { calls += 1; return new Promise((done) => { resolve = done; }); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { ...app, Date: { now: () => stamp }, wx: { navigateTo: (r) => { url = r.url; } } });
  page.onLoad(); page.onShow(); assert.equal(calls, 1);
  resolve({ stats: {}, items: [{ id: 1, kind: '政策' }, { id: 2, kind: '申报通知' }] }); await flush();
  page.pickTab(event({ key: '政策' }));
  page.onHide(); stamp += 2000; page.onShow(); assert.equal(calls, 2);
  resolve({ stats: {}, items: [{ id: 3, kind: '政策' }] }); await flush();
  assert.equal(page.data.shown[0].id, 3);
  page.onShow(); assert.equal(calls, 2);
  for (const [index, kind] of [[0, '申报通知'], [2, '政策'], [3, 'all']]) {
    page.pickScope(event({ index })); page.goSearch(event({ q: '高企' }));
    assert.equal(new URL(url, 'https://test.invalid').searchParams.get('kind'), kind);
  }
});

test('J-05 详情返回重新读取核定日期，下架清空正文与操作并提供返回；网络失败标明未更新', async () => {
  let stamp = 10000;
  let calls = 0;
  let result = { id: '1', title: '合成文件', deadline: { date: '2026-10-10', verified: true }, tags: [] };
  let error;
  let back = false;
  const page = load('pages/detail/index.js', {
    '../../utils/api.js': { detail: () => { calls += 1; return error ? Promise.reject(error) : Promise.resolve(result); } },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': {},
    '../../utils/repost.js': require('../utils/repost.js'),
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { Date: { now: () => stamp }, wx: { navigateBack: () => { back = true; } } });
  page.onLoad({ id: 1 }); page.onShow(); await flush(); assert.equal(calls, 1);
  result = { ...result, deadline: null };
  page.onHide(); stamp += 2000; page.onShow(); await flush();
  assert.equal(calls, 2); assert.equal(page.data.due, null);
  error = { status: 404, message: '该文件已下架' };
  stamp += 2000; page.onShow(); await flush();
  assert.equal(page.data.doc, null); assert.equal(page.data.error, '该文件已下架');
  assert.equal(page.data.takenDown, true); page.goBack(); assert.equal(back, true);
  assert.match(wxml('detail'), /bindtap="goBack">返回/);
  error = new Error('网络故障'); stamp += 2000; page.onShow(); await flush();
  assert.match(page.data.error, /数据未更新/);
});

test('J-06 雷达和名单看板复用平台说明组件', () => {
  for (const p of ['report', 'list-board']) assert.match(wxml(p), /<notice-bar\s*\/>/);
});

test('J-04/J-07/J-08 我的数据分开处理和安全状态，每类完整单条复制，历史提醒可删除', async () => {
  const raw = { user: { openid: 'synthetic', agreed: null }, follows: [{ id: 1, name: '未截止', conditions: { open: 1 } }],
    subscriptions: [{ id: 2, kind: 'deadline', status: 'sent', attemptedAt: '2026-10-03T10:00:00+08:00' }],
    feedback: [{ id: 3, content: '合成反馈', checkResult: 'pass', handled: true, handledAt: '2026-10-03T11:00:00+08:00' }], retention: {} };
  const copied = []; let deleted; let modal;
  const page = load('pages/my-data/index.js', {
    '../../utils/api.js': { myData: () => Promise.resolve(raw), deleteSubscription: (id) => { deleted = id; return Promise.resolve(); } },
    '../../utils/conditions.js': conditions, '../../utils/consent.js': {},
  }, { wx: { setClipboardData: ({ data }) => copied.push(JSON.parse(data)), showToast() {},
    showModal: (m) => { modal = m; m.success({ confirm: true }); } } });
  page.load(); await flush();
  assert.equal(page.data.feedback[0].handling, '已处理');
  assert.match(page.data.feedback[0].handledAt, /11:00/);
  assert.equal(page.data.feedback[0].check, '已通过内容安全检查');
  for (const [kind, key, id] of [['follow', 'follows', 1], ['sub', 'subscriptions', 2], ['feedback', 'feedback', 3]]) {
    page.copyOne(event({ kind, id })); assert.deepEqual(copied.at(-1), raw[key][0]);
  }
  page.remove(event({ kind: 'sub', id: 2 })); await flush();
  assert.equal(deleted, 2); assert.equal(modal.title, '删除记录'); assert.match(modal.content, /历史记录/);
  raw.subscriptions[0].status = 'unused'; page.load(); await flush();
  page.remove(event({ kind: 'sub', id: 2 })); assert.equal(modal.title, '取消提醒');
  const template = wxml('my-data');
  assert.equal((template.match(/bindtap="copyOne"/g) || []).length, 3);
  assert.doesNotMatch(template, /wx:if="{{item.unused}}" class="item-ops"/);
});

test('J-09 雷达省份来自无筛选30天范围，筛部门/支持方式后仍能直接切省份', async () => {
  const calls = [];
  const page = load('pages/report/index.js', {
    '../../utils/api.js': { deadlineReport: (p) => { calls.push({ ...p }); return Promise.resolve({ items: [], total: 0, by_province: p.days === 30 && !p.province ? { 北京: 1, 辽宁: 2 } : { 北京: 1 } }); } },
    '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, app);
  page.onLoad(); await flush();
  assert.deepEqual(Array.from(page.data.provinces), ['不限省份', '北京', '辽宁']);
  const depts = Array.from(page.data.depts); const supports = Array.from(page.data.supports);
  page.pickProvince({ detail: { value: 1 } }); await flush();
  page.pickDept({ detail: { value: 1 } }); await flush();
  page.pickSupport({ detail: { value: 1 } }); await flush();
  assert.deepEqual(Array.from(page.data.provinces), ['不限省份', '北京', '辽宁']);
  assert.deepEqual(Array.from(page.data.depts), depts); assert.deepEqual(Array.from(page.data.supports), supports);
  page.pickProvince({ detail: { value: 2 } }); await flush(); assert.equal(calls.at(-1).province, '辽宁');
});

test('J-10 申请覆盖上限495，含前缀恰好500；超长明确提示且不发请求', async () => {
  const calls = []; let message;
  const page = load('pages/feedback/index.js', {
    '../../utils/api.js': { feedback: (_, content) => { calls.push(content); return Promise.resolve({}); } },
    '../../utils/consent.js': { ensureConsent: () => Promise.resolve(true) },
  }, { setTimeout() {}, wx: { setNavigationBarTitle() {}, showToast: (r) => { message = r.title; } } });
  page.onLoad({ type: 'coverage' }); assert.equal(page.data.max, 495);
  page.onInput({ detail: { value: '字'.repeat(496) } }); assert.match(message, /最多输入 495 字/);
  assert.equal(page.data.count, 495); page.submit(); await flush(); assert.equal(calls[0].length, 500);
  page.data.sending = false; page.data.content = '字'.repeat(496); page.submit(); await flush();
  assert.equal(calls.length, 1); assert.match(message, /最多输入 495 字/);
  page.onLoad({}); assert.equal(page.data.max, 500);
});

test('J-03 日历分别收取和分页已截止数据，未来总数不受已截止影响', async () => {
  const api = load('utils/api.js', {
    './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': format,
  }, { wx: { request: ({ url, success }) => {
    const offset = Number(new URL(url).searchParams.get('offset'));
    success({ statusCode: 200, data: { total: 1, items: offset ? [] : [{ id: 'future' }], pastTotal: 201,
      past: Array.from({ length: Math.min(200, 201 - offset) }, (_, i) => ({ id: `past${offset + i}` })) } });
  } } });
  const res = await api.calendar(); assert.equal(res.total, 1); assert.equal(res.items.length, 1); assert.equal(res.past.length, 201);
  assert.equal(res.truncated, false);
});

test('J-11 首次等待及失败时不显示空结果或零月份，六页都有明确加载提示', async () => {
  const pending = () => new Promise(() => {});
  const search = searchPage({ search: pending }); search.onLoad({}); assert.equal(search.data.loading, true);
  const follow = load('pages/follow/index.js', {
    '../../utils/api.js': { isLive: () => true }, '../../content/legal.js': { final: true },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/subscribe.js': {},
    '../../utils/consent.js': { syncConsent: pending }, '../../utils/conditions.js': conditions,
  }); follow.load(); assert.equal(follow.data.loading, true);
  const calendar = load('pages/calendar/index.js', {
    '../../utils/api.js': { calendar: pending }, '../../utils/format.js': format, '../../utils/subscribe.js': {},
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/daywatch.js': daywatch,
  }); calendar.load(); assert.equal(calendar.data.loading, true); assert.equal(calendar.data.loaded, false);
  const recent = load('pages/lists/index.js', { '../../utils/api.js': { lists: pending }, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch }, app);
  recent.onLoad(); assert.equal(recent.data.loading, true);
  for (const p of ['search', 'follow', 'follow-new', 'calendar', 'report']) {
    assert.match(wxml(p), /wx:if="{{loading[^}]*}}"[^>]*>正在加载…/);
  }
  // 查名单的列表改用灰色占位块（骨架屏），读屏软件读"正在加载"
  assert.match(wxml('lists'), /wx:if="{{loading}}" aria-label="正在加载">\s*<view wx:for="{{\[1, 2, 3\]}}" wx:key="\*this" class="sk-row">/);
  assert.match(wxml('calendar'), /wx:if="{{loaded && !loading && !error}}"/);
  assert.match(wxml('report'), /!items.length && !loading && !error/);
  assert.match(wxml('lists'), /!shown.length && !loading && !error/);
});

test('本轮修改页面的 WXML 标签闭合，事件都有页面处理函数', () => {
  for (const page of ['home', 'detail', 'search', 'follow', 'follow-new', 'calendar', 'report', 'list-board', 'lists', 'files', 'my-data', 'feedback', 'me']) {
    const source = wxml(page).replace(/<!--[\s\S]*?-->/g, '');
    const stack = [];
    const tags = /<(\/?)([\w:-]+)\b(?:"[^"]*"|'[^']*'|[^'"<>])*?>/g;
    for (const match of source.matchAll(tags)) {
      const [tag, closing, name] = match;
      if (closing) assert.equal(stack.pop(), name, `${page} 标签不配对 ${tag}`);
      else if (!/\/\s*>$/.test(tag)) stack.push(name);
    }
    assert.equal(stack.length, 0, `${page} 存在未闭合标签 ${stack}`);
    const script = fs.readFileSync(path.join(__dirname, '..', 'pages', page, 'index.js'), 'utf8');
    for (const match of source.matchAll(/(?:bind|catch)(?::?[\w-]+)="([\w]+)"/g)) {
      assert.match(script, new RegExp(`\\b${match[1]}\\s*\\(`), `${page} 未定义 ${match[1]}`);
    }
  }
});

test('J-02 关注页的按此条件搜索入口保留 open 和其余筛选', async () => {
  const c = { q: '高企', kind: '申报通知', regions: '北京', time: '不限', open: 1 };
  let target;
  const page = load('pages/follow/index.js', {
    '../../utils/api.js': { myFollows: () => Promise.resolve({ items: [{ id: 1, name: '未截止', newCount: 0, conditions: c }] }),
      mySubscriptions: () => Promise.resolve({ items: [] }) },
    '../../content/legal.js': { final: true }, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/subscribe.js': {}, '../../utils/consent.js': {}, '../../utils/conditions.js': conditions,
  }, { wx: { navigateTo: ({ url }) => { target = url; } } });
  page.loadLive(); await flush(); page.searchFollow(event({ id: '1' }));
  const params = Object.fromEntries(new URL(target, 'https://test.invalid').searchParams);
  assert.deepEqual(params, { ...c, open: '1' });
  assert.match(page.data.follows[0].desc, /只看未截止/);
});
