const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = require('../utils/tool-context.js');
const format = require('../utils/format.js');

const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));
function load(file, dependencies = {}, globals = {}) {
  const module = { exports: {} }; let definition;
  vm.runInNewContext(read(file), {
    module, Date, Math, Promise, Set, setTimeout, clearTimeout,
    require(name) { if (name === '../../utils/consent.js' && !Object.hasOwn(dependencies, name)) return { loginOpen: () => true }; assert.ok(Object.hasOwn(dependencies, name), `未提供依赖 ${name}`); return dependencies[name]; },
    Page(value) { definition = value; }, Component(value) { definition = value; },
    ...globals,
  }, { filename: file });
  if (!definition) return module.exports;
  return { ...definition, data: plain(definition.data), setData(values) { Object.assign(this.data, values); } };
}

function apiHarness(response) {
  const app = { globalData: {} }; const pending = []; let calls = 0;
  const api = load('utils/api.js', {
    './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {},
    '../mock/advisor.js': {}, './mockSearch.js': {}, './format.js': format,
  }, { getApp: () => app, wx: { request(options) {
    calls += 1;
    if (response) options.success(response);
    else pending.push(options);
  } } });
  return { api, app, pending, calls: () => calls };
}

function fab(api, properties = {}, options = {}) {
  let keyboard; let overlay; let resize; let navigation; const removed = [];
  let height = options.height || 70;
  const query = {
    in(owner) { this.owner = owner; return this; },
    select(selector) { this.selector = selector; return this; },
    boundingClientRect(callback) { this.callback = callback; return this; },
    exec() { this.callback({ height }); },
  };
  const owner = options.owner || { getTabBar: () => ({ createSelectorQuery: () => query }) };
  const wx = {
    onKeyboardHeightChange(fn) { keyboard = fn; }, offKeyboardHeightChange(fn) { removed.push(fn); },
    onWindowResize(fn) { resize = fn; }, offWindowResize(fn) { removed.push(fn); },
    getWindowInfo: () => ({ screenHeight: 800, safeArea: { bottom: 780 } }),
    createSelectorQuery: () => query,
    navigateTo(value) { navigation = value; },
  };
  const view = load('components/tool-fab/index.js', {
    '../../utils/api.js': api, '../../utils/tool-context.js': context,
    '../../utils/consent.js': { loginOpen: () => options.loginOpen !== false },
    '../../utils/tool-overlays.js': { subscribe(fn) { overlay = fn; fn(false); return () => removed.push(fn); } },
  }, { wx, getCurrentPages: () => [owner] });
  view.properties = Object.fromEntries(Object.entries(view.properties).map(([key, value]) => [key, value.value]));
  Object.assign(view.properties, properties); Object.assign(view, view.methods);
  view.lifetimes.attached.call(view); view.lifetimes.ready.call(view);
  const expression = /<button wx:if="\{\{(.*?)\}\}"/.exec(read('components/tool-fab/index.wxml'))[1];
  return {
    view, query, keyboard: (n) => keyboard({ height: n }), overlay: (value) => overlay(value),
    resize(n) { height = n; resize(); }, navigation: () => navigation, removed,
    visible: () => vm.runInNewContext(expression, { ...view.data, ...view.properties }),
  };
}

test('app 级状态缓存：多个页面共用在途请求和结果，工具刷新额度才重新请求', async () => {
  const harness = apiHarness();
  const one = fab(harness.api); const two = fab(harness.api);
  assert.equal(harness.calls(), 1); assert.equal(one.visible(), false);
  harness.pending[0].success({ statusCode: 200, data: { enabled: true } }); await flush();
  assert.equal(one.visible(), true); assert.equal(two.visible(), true);
  const three = fab(harness.api); await flush(); assert.equal(three.visible(), true); assert.equal(harness.calls(), 1);
  const updated = harness.api.advisorStatus({ refresh: true });
  assert.equal(harness.api.advisorStatus({ refresh: true }), updated);
  assert.equal(harness.calls(), 2);
  harness.pending[1].success({ statusCode: 200, data: { enabled: false } }); await updated;
  one.view.pageLifetimes.show.call(one.view); await flush(); assert.equal(one.visible(), false);
});

test('协议未定稿时八页的工具入口均隐藏，不请求状态接口、不导航', async () => {
  let calls = 0;
  const button = fab({ advisorStatus: () => { calls += 1; return Promise.resolve({ enabled: true }); } }, {}, { loginOpen: false });
  await flush();
  button.view.pageLifetimes.show.call(button.view);
  button.view.open();
  assert.equal(button.visible(), false);
  assert.equal(calls, 0);
  assert.equal(button.navigation(), undefined);
  assert.equal(button.view.data.loginOpen, false);
  button.view.lifetimes.detached.call(button.view);
});

test('工具上下文保留主线新增的层级、项目类别、支持方式和产业方向', () => {
  const filters = { region: ['辽宁'], level: ['国家部委', '省级'], category: ['人才'], support: ['资金支持'], industry: ['数字经济'] };
  const parsed = context.read({ filters: context.url({ source: 'search', filters }).split('?filters=')[1] });
  assert.equal(parsed.filters.levels, '国家部委,省级');
  assert.equal(parsed.filters.categories, '人才');
  assert.equal(parsed.filters.supports, '资金支持');
  assert.equal(parsed.filters.industries, '数字经济');
  for (const value of ['国家部委、省级', '人才', '资金支持', '数字经济']) assert.ok(parsed.label.includes(value));
});

test('演示额度按北京时间自然月累计，跨月取消只退回原月；跟进表仍每天一次', async () => {
  const demo = demoHarness(); const api = demo.module('utils/api.js');
  await api.agree(demo.module('utils/advisor.js').VERSION, 'advisor');
  const body = { kind: 'compare', rows: ['示例公司'] };
  const first = await api.createAdvisorTask(body, 'one');
  demo.advance(86400000);
  await api.createAdvisorTask(body, 'two');
  assert.equal((await api.advisorStatus({ refresh: true })).quota.compare.used, 2);
  await api.createAdvisorTask(body, 'three');
  await assert.rejects(api.createAdvisorTask(body, 'four'), /本月/);
  // 演示时钟起点为 10/3 北京时间 09:00，推进到 11/1 00:00。
  demo.advance(28 * 86400000 - 9 * 3600000);
  assert.equal((await api.advisorStatus({ refresh: true })).quota.compare.used, 0);
  const next = await api.createAdvisorTask(body, 'next');
  await api.cancelAdvisorTask(next.id);
  assert.equal((await api.advisorStatus({ refresh: true })).quota.compare.used, 0);
  assert.equal((await api.advisorTask(first.id)).status, 'done');
  const track = { kind: 'track', rows: [{ name: '示例公司', province: '辽宁' }] };
  await api.createAdvisorTask(track, 'track-one');
  await assert.rejects(api.createAdvisorTask(track, 'track-two'), /今天/);
  demo.advance(86400000);
  assert.equal((await api.advisorStatus({ refresh: true })).quota.track.used, 0);
  await api.createAdvisorTask(track, 'track-next');
  const boundary = demoHarness(); const boundaryApi = boundary.module('utils/api.js');
  await boundaryApi.agree(boundary.module('utils/advisor.js').VERSION, 'advisor');
  boundary.advance(29 * 86400000 - 9 * 3600000 - 1000);
  const old = await boundaryApi.createAdvisorTask(body, 'old-month');
  boundary.advance(1000);
  await boundaryApi.createAdvisorTask(body, 'new-month');
  await boundaryApi.cancelAdvisorTask(old.id);
  await boundaryApi.cancelAdvisorTask(old.id);
  assert.equal((await boundaryApi.advisorStatus({ refresh: true })).quota.compare.used, 1);
});

test('关闭、404、请求失败和非布尔 enabled 均静默隐藏；失败也缓存', async () => {
  for (const response of [
    { statusCode: 200, data: { enabled: false } }, { statusCode: 404 }, { statusCode: 500 },
    { statusCode: 200, data: { enabled: 'true' } },
  ]) {
    const harness = apiHarness(response); const button = fab(harness.api);
    await flush(); assert.equal(button.visible(), false);
    button.view.pageLifetimes.show.call(button.view); await flush(); assert.equal(harness.calls(), 1);
    button.view.open(); assert.equal(button.navigation(), undefined);
  }
  const harness = apiHarness(); const button = fab(harness.api);
  harness.pending[0].fail(); await flush(); assert.equal(button.visible(), false);
  fab(harness.api); await flush(); assert.equal(harness.calls(), 1);
});

test('键盘、页面弹层、批量勾选、原生弹窗和隐藏页不显示，销毁后移除监听', async () => {
  const button = fab({ advisorStatus: () => Promise.resolve({ enabled: true }) }); await flush();
  assert.equal(button.visible(), true);
  button.keyboard(260); assert.equal(button.visible(), false); button.view.open(); assert.equal(button.navigation(), undefined);
  button.keyboard(0); assert.equal(button.visible(), true);
  button.view.properties.blocked = true; assert.equal(button.visible(), false);
  button.view.properties.blocked = false; button.overlay(true); assert.equal(button.visible(), false);
  button.overlay(false); assert.equal(button.visible(), true);
  button.view.pageLifetimes.hide.call(button.view); button.keyboard(260); assert.equal(button.visible(), false);
  assert.equal(button.view.data.keyboard, false);
  button.view.pageLifetimes.show.call(button.view); await flush(); assert.equal(button.visible(), true);
  button.view.lifetimes.detached.call(button.view); assert.equal(button.removed.length, 3);
  let resolve;
  const late = fab({ advisorStatus: () => new Promise((done) => { resolve = done; }) });
  late.view.lifetimes.detached.call(late.view); resolve({ enabled: true }); await flush(); assert.equal(late.view.data.enabled, false);
});

test('tab 页和详情页实测底栏，扣除一次安全区；窗口变化后重新测量', async () => {
  const tab = fab({ advisorStatus: () => Promise.resolve({ enabled: true }) }, { tabPage: true, bottomRpx: 108 }, { height: 74 });
  await flush(); assert.equal(tab.query.selector, '.bar'); assert.equal(tab.view.data.barHeight, '54px');
  tab.resize(84); assert.equal(tab.view.data.barHeight, '64px');
  const owner = {};
  const detail = fab({ advisorStatus: () => Promise.resolve({ enabled: true }) }, { bottomSelector: '.bar', bottomRpx: 130 }, { owner, height: 86 });
  await flush(); assert.equal(detail.query.owner, owner); assert.equal(detail.view.data.barHeight, '66px');
  assert.match(read('components/tool-fab/index.wxml'), /env\(safe-area-inset-bottom\) \+ 24rpx/);
  assert.match(read('components/tool-fab/index.wxss'), /z-index: 110/);
  assert.match(read('components/tool-fab/index.wxss'), /min-height: 112rpx/);
});

test('详情上下文与搜索全部筛选可往返，其他入口无上下文；重复点击只导航一次', async () => {
  const doc = fab({ advisorStatus: () => Promise.resolve({ enabled: true }) }, { docId: 42, docTitle: '通知：科技 & 申报（50%）' });
  await flush(); doc.view.open(); const first = doc.navigation(); doc.view.open(); assert.equal(doc.navigation(), first);
  const query = Object.fromEntries(new URL(`https://test.invalid${first.url}`).searchParams);
  // 小程序页面延续现有约定：onLoad 中再解码一次，测试保留 URL 参数的编码。
  query.title = encodeURIComponent(query.title);
  assert.deepEqual(context.read(query), { docId: '42', title: '通知：科技 & 申报（50%）', label: '当前通知：通知：科技 & 申报（50%）' });
  first.complete(); assert.equal(doc.view.data.navigating, false);
  const filters = { region: ['辽宁', '北京'], type: ['申报通知', '政策文件'], status: ['公示', '公布'], time: ['今年'], dept: ['科技部门'], open: ['只看未截止'] };
  const link = context.url({ source: 'search', keyword: ' 高新 & 科技 ', kind: '申报通知', filters });
  const parsed = context.read({ filters: link.split('?filters=')[1] });
  assert.deepEqual(parsed.filters, { q: '高新 & 科技', kind: '申报通知', regions: '辽宁,北京', types: '申报通知,政策文件', status: '公示,公布', time: '今年', depts: '科技部门', open: 1, levels: '', categories: '', supports: '', industries: '' });
  for (const value of ['高新 & 科技', '辽宁、北京', '申报通知、政策文件', '公示、公布', '今年', '科技部门', '只看未截止']) assert.ok(parsed.label.includes(value));
  assert.equal(context.url({}), '/pages/advisor/index'); assert.equal(context.read({}), null);
  assert.equal(context.read({ docId: '42', title: '原文标题 50%' }).title, '原文标题 50%');
  assert.equal(context.read({ filters: JSON.stringify({ q: '50%', kind: '政策' }) }).filters.q, '50%');
  for (const filters of ['bad-json', '%', encodeURIComponent('null')]) assert.equal(context.read({ filters }), null);
});

test('雷达和看板选择器打开时隐藏，选择或取消后恢复', async () => {
  for (const name of ['report', 'list-board']) {
    const page = load(`pages/${name}/index.js`, {
      '../../utils/api.js': {}, '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': {},
    });
    page.load = () => {};
    page.openToolPicker(); assert.equal(page.data.toolPickerOpen, true);
    page.closeToolPicker(); assert.equal(page.data.toolPickerOpen, false);
    page.openToolPicker(); page.pickProvince({ detail: { value: 0 } }); assert.equal(page.data.toolPickerOpen, false);
    assert.match(read(`pages/${name}/index.wxml`), /blocked="\{\{toolPickerOpen\}\}"/);
    assert.match(read(`pages/${name}/index.wxml`), /bindcancel="closeToolPicker"/);
  }
});

test('原生弹窗追踪取消、嵌套、抛错、loading 失败，保持原回调', () => {
  const overlays = load('utils/tool-overlays.js'); const seen = []; let modal; let sheet; let loading;
  const wx = {
    showModal(options) { modal = options; }, showActionSheet(options) { sheet = options; },
    requestSubscribeMessage() { throw new Error('调用失败'); },
    showLoading(options) { loading = options; }, hideLoading() {},
  };
  overlays.install(wx); const installed = wx.showModal; overlays.install(wx); assert.equal(wx.showModal, installed);
  const unsubscribe = overlays.subscribe((value) => seen.push(value)); let completed = 0; let succeeded = 0;
  wx.showModal({ success: () => { succeeded += 1; }, complete: () => { completed += 1; } });
  wx.showActionSheet({}); modal.success({ confirm: true }); modal.complete({}); modal.complete({});
  assert.equal(seen.at(-1), true); assert.equal(succeeded, 1); assert.equal(completed, 2);
  sheet.complete({ errMsg: 'cancel' }); assert.equal(seen.at(-1), false);
  assert.throws(() => wx.requestSubscribeMessage({}), /调用失败/); assert.equal(seen.at(-1), false);
  wx.showLoading({}); assert.equal(seen.at(-1), true); wx.hideLoading(); assert.equal(seen.at(-1), false);
  let failed = false; wx.showLoading({ fail: () => { failed = true; } }); loading.fail({}); assert.equal(failed, true); assert.equal(seen.at(-1), false);
  unsubscribe(); const count = seen.length; wx.showModal({}); assert.equal(seen.length, count);
});

const entryPages = ['home', 'search', 'detail', 'calendar', 'report', 'lists', 'list-board', 'follow'];
test('只在指定八页放工具入口，参数绑定完整，移除首页旧入口；改动 WXML 闭合', () => {
  const allPages = JSON.parse(read('app.json')).pages;
  for (const route of allPages) {
    const name = route.split('/')[1]; const template = read(`${route}.wxml`);
    if (entryPages.includes(name)) {
      assert.match(template, /<tool-fab\b/);
      assert.equal(JSON.parse(read(`${route}.json`)).usingComponents['tool-fab'], '/components/tool-fab/index');
    } else assert.doesNotMatch(template, /<tool-fab\b/);
  }
  assert.doesNotMatch(read('pages/home/index.wxml'), /data-url="\/pages\/advisor/);
  assert.doesNotMatch(read('pages/home/index.js'), /advisorStatus/);
  assert.match(read('pages/search/index.wxml'), /blocked="\{\{filterOpen \|\| selecting\}\}".*keyword="\{\{q\}\}".*filters="\{\{picked\}\}"/);
  assert.match(read('pages/detail/index.wxml'), /doc-id="\{\{doc.id\}\}" doc-title="\{\{doc.title\}\}"/);
  const changed = entryPages.concat(['advisor', 'advisor-form', 'advisor-tasks', 'advisor-terms', 'files']).map((name) => `pages/${name}/index.wxml`).concat('components/tool-fab/index.wxml');
  for (const file of changed) {
    const stack = [];
    for (const tag of read(file).replace(/<!--[^]*?-->/g, '').replace(/\{\{[^]*?\}\}/g, '').matchAll(/<(\/)?([\w-]+)\b[^>]*?(\/?)>/g)) {
      if (tag[1]) assert.equal(stack.pop(), tag[2], `${file}: ${tag[2]}`);
      else if (!tag[3]) stack.push(tag[2]);
    }
    assert.equal(stack.length, 0, file);
  }
  for (const file of ['pages/advisor/index.wxml', 'pages/advisor-tasks/index.wxml', 'components/tool-fab/index.wxml']) assert.doesNotMatch(read(file), /AI|智能|即将推出/);
});

function demoHarness() {
  let now = Date.parse('2026-10-03T01:00:00Z');
  class Clock extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
  const storage = new Map(); const files = new Map(); const navigation = []; const timers = [];
  let opened; let failOpen = false; let failStorage = false;
  const filesystem = {
    writeFile(options) { files.set(options.filePath, Buffer.from(options.data)); options.success(); },
    readdirSync: () => Array.from(files.keys()).map((file) => file.split('/').at(-1)),
    unlinkSync(file) { files.delete(file); },
    accessSync(file) { if (!files.has(file)) throw new Error('不存在'); },
    readFile(options) { if (files.has(options.filePath)) options.success({ data: files.get(options.filePath) }); else options.fail(); },
  };
  const wx = {
    env: { USER_DATA_PATH: '/demo' },
    getStorageSync: (key) => storage.get(key),
    setStorageSync(key, value) { if (failStorage) throw new Error('存储已满'); storage.set(key, value); },
    removeStorageSync: (key) => storage.delete(key),
    base64ToArrayBuffer: (value) => Uint8Array.from(Buffer.from(value, 'base64')).buffer,
    getFileSystemManager: () => filesystem,
    openDocument(options) { opened = options.filePath; if (failOpen) options.fail(); else options.success(); },
    showModal(options) { options.success({ confirm: true }); },
    showToast() {}, setNavigationBarTitle() {}, navigateBack() {},
    navigateTo(options) { navigation.push(options.url); }, redirectTo(options) { navigation.push(options.url); },
  };
  const app = { globalData: {} };
  const cache = {
    'utils/config.js': {}, 'utils/mockSearch.js': {},
    'mock/data.js': { files: [{ badge: 'XLSX', name: '静态演示记录', meta: '示例' }] },
    'utils/consent.js': { loginOpen: () => true, ensureConsent: () => Promise.resolve(true) },
  };
  const globals = { wx, Date: Clock, getApp: () => app, setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {} };
  function module(file) {
    assert.notEqual(file, 'content/legal.js', '绝不读取占位协议');
    if (!Object.hasOwn(cache, file)) cache[file] = load(file, {}, { ...globals, require: (name) => module(path.posix.normalize(path.posix.join(path.posix.dirname(file), name))) });
    return cache[file];
  }
  function page(name) {
    const file = `pages/${name}/index.js`;
    return load(file, {}, { ...globals, require: (name) => module(path.posix.normalize(path.posix.join(path.posix.dirname(file), name))) });
  }
  return { page, module, storage, files, navigation, timers, opened: () => opened, advance: (ms) => { now += ms; }, failOpen: (value) => { failOpen = value; }, failStorage: (value) => { failStorage = value; } };
}

test('整页任务板块保留并清除上下文，只列最近三条；刷新额度、近期失败可单独重试', async () => {
  const tasks = [1, 4, 2, 3].map((id) => ({ id, kind: 'compare', status: 'done', rowCount: 1, createdAt: `2026-10-03T0${id}:00:00Z` }));
  const helpers = { taskView: (task) => ({ ...task, kindLabel: '名单比对', stateLabel: '已完成' }) };
  let taskError = true; let refreshed;
  const api = {
    advisorStatus(options) { refreshed = options.refresh; return Promise.resolve({ enabled: true, quota: { compare: { used: 1, limit: 3, maxRows: 50 }, track: { used: 0, limit: 1, maxRows: 50 } } }); },
    advisorTasks: () => taskError ? Promise.reject(new Error('暂时连不上')) : Promise.resolve({ items: tasks }),
  };
  const urls = [];
  const page = load('pages/advisor/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers, '../../utils/tool-context.js': context }, { wx: { navigateTo: ({ url }) => urls.push(url) } });
  page.onLoad({ docId: '7', title: encodeURIComponent('通知标题') }); assert.equal(page.data.context.label, '当前通知：通知标题');
  page.onShow(); await flush(); assert.equal(refreshed, true); assert.deepEqual(Array.from(page.data.skills, (skill) => skill.remaining), [2, 1]);
  assert.match(page.data.recentError, /暂时连不上/); assert.equal(page.data.enabled, true);
  taskError = false; page.retryRecent(); await flush(); assert.deepEqual(Array.from(page.data.recent, (item) => item.id), [4, 3, 2]);
  page.tasks({ currentTarget: { dataset: { id: 3 } } }); assert.equal(urls.at(-1), '/pages/advisor-tasks/index?taskId=3');
  page.tasks(); assert.equal(urls.at(-1), '/pages/advisor-tasks/index');
  page.clearContext(); assert.equal(page.data.context, null);
  page.form({ currentTarget: { dataset: { kind: 'track' } } }); assert.equal(urls.at(-1), '/pages/advisor-form/index?kind=track');
  assert.match(read('pages/advisor/index.wxml'), /以下工具按你提交的客户名单处理，与当前通知无关/);
  page.onLoad({ filters: context.url({ source: 'search', keyword: '关键词' }).split('?filters=')[1] }); assert.match(page.data.context.label, /当前筛选：.*关键词/);
  page.onLoad({}); assert.equal(page.data.context, null);
  const gated = load('pages/advisor/index.js', { '../../utils/api.js': { advisorStatus: () => Promise.resolve({ enabled: false }) }, '../../utils/advisor.js': helpers, '../../utils/tool-context.js': context });
  gated.onShow(); await flush(); assert.equal(gated.data.enabled, false); assert.equal(gated.data.error, '');
});

test('页面演示闭环：同意、两种工具提交、进度、任务结果、文件记录与清理后重新导出', async () => {
  const demo = demoHarness(); const api = demo.module('utils/api.js');
  const terms = demo.page('advisor-terms'); terms.onShow(); await flush();
  assert.equal(terms.data.demo, true); assert.equal(terms.data.final, true);
  assert.match(terms.data.text, /虚构企业信息/);
  assert.equal(demo.module('content/advisor-terms.js').final, false);
  terms.check({ detail: { value: ['agree'] } }); terms.agree(); await flush();
  assert.equal(terms.data.agreed, true);
  const board = demo.page('advisor'); board.onLoad({}); board.onShow(); await flush(); assert.equal(board.data.skills[0].remaining, 3);
  for (const kind of ['compare', 'track']) {
    const form = demo.page('advisor-form'); form.onLoad({ kind }); form.onShow(); await flush();
    assert.equal(form.data.agreed, true);
    form.input({ detail: { value: kind === 'compare' ? '虚构示例科技有限公司' : '虚构示例科技有限公司,辽宁,科技,资金支持' } });
    form.submit(); await flush(); assert.equal(form.data.inputError, '');
    const id = new URL(`https://test.invalid${demo.navigation.at(-1)}`).searchParams.get('taskId'); assert.ok(id);
    const detail = demo.page('advisor-tasks'); detail.onLoad({ taskId: id }); detail.onShow(); await flush();
    assert.equal(detail.data.items.length, 1); assert.equal(detail.data.items[0].status, 'queued');
    demo.advance(3100); detail.poll(detail.epoch); await flush(); assert.equal(detail.data.items[0].status, 'running');
    demo.advance(3000); detail.poll(detail.epoch); await flush(); assert.equal(detail.data.items[0].status, 'done');
    detail.open({ currentTarget: { dataset: { id: Number(id) } } }); await flush();
    assert.equal(detail.data.actionError, ''); assert.equal(demo.files.get(demo.opened()).readUInt32LE(0), 0x04034b50);
    detail.lists(); assert.equal(demo.navigation.at(-1), '/pages/lists/index');
    detail.radar(); assert.equal(demo.navigation.at(-1), '/pages/report/index'); detail.onHide();
  }
  board.onShow(); await flush(); assert.equal(board.data.recent.length, 2);
  assert.deepEqual(Array.from(board.data.skills, (skill) => skill.remaining), [2, 0]);
  const files = demo.page('files'); files.onShow(); await flush();
  assert.equal(files.data.files.length, 3); assert.equal(files.data.files[0].name, '在报项目跟进表');
  assert.equal(files.data.files[0].expired, false); assert.equal(files.data.files[1].name, '名单比对'); assert.equal(files.data.files[1].expired, true);
  const records = demo.module('utils/exports.js').list(); assert.equal(records.length, 2); assert.equal(records[0].source, 'advisor');
  assert.equal(records[0].count, 1); assert.equal(demo.files.size, 1);
  files.open({ currentTarget: { dataset: { index: 0 } } }); await flush(); assert.ok(demo.files.has(demo.opened()));
  files.reexport({ currentTarget: { dataset: { index: 1 } } }); await flush();
  assert.equal(files.data.files[0].name, '名单比对'); assert.equal(files.data.files[0].expired, false); assert.equal(demo.module('utils/exports.js').list().length, 2);
  const sample = await api.downloadAdvisorTask(2); const zip = demo.module('utils/advisor-file.js').zipFiles(new Uint8Array(Buffer.from(sample.contentBase64, 'base64')));
  const sheet = demo.module('utils/advisor-file.js').utf8(zip['xl/worksheets/sheet1.xml']);
  assert.match(sheet, /申报通知/); assert.match(sheet, /不代表真实查询/);
  files.clearAll(); await flush(); assert.equal(demo.files.size, 0); assert.equal(demo.module('utils/exports.js').list().length, 0);
});

test('保存记录后打开失败仍能在文件页重试；记录写入失败不清理上一份文件', async () => {
  const demo = demoHarness(); const api = demo.module('utils/api.js');
  await api.agree(demo.module('utils/advisor.js').VERSION, 'advisor');
  const task = await api.createAdvisorTask({ kind: 'compare', rows: ['示例公司'] }, 'demo'); demo.advance(7000);
  demo.failOpen(true);
  await assert.rejects(demo.module('utils/advisor.js').openResult(task.id, { task }), /打开文件失败/);
  const saved = demo.module('utils/exports.js').list()[0]; assert.ok(demo.files.has(saved.path));
  demo.failOpen(false); demo.failStorage(true);
  await assert.rejects(demo.module('utils/advisor.js').openResult(task.id, { task }), /导出记录没能写入/);
  assert.ok(demo.files.has(saved.path)); assert.equal(demo.module('utils/exports.js').list()[0].path, saved.path);
});
