const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { render } = require('./helpers/wxml.js');
const ROOT = path.join(__dirname, '..');
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const status = { enabled: true, quota: { compare: { limit: 3, used: 1, maxRows: 50 }, track: { limit: 3, used: 2, maxRows: 50 } } };
const caps = { ai_advisor: { enabled: true, features: ['interpret', 'clause'], remaining: { interpret: 4, clause: 2 }, disclosure: { modelLabel: '测试模型', filingNo: '测试编号' } } };
const tasks = ['compare', 'interpret', 'track', 'clause'].map((kind, index) => ({
  id: index + 1, kind, createdAt: `2026-10-11T0${index}:00:00Z`, status: 'done', hasFile: ['compare', 'track'].includes(kind), rowCount: 2,
}));
function load(file, deps = {}, globals = {}) {
  const filename = path.join(ROOT, file); const localRequire = createRequire(filename);
  let definition; const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), {
    module, Date, Math, Promise, Set, setTimeout, clearTimeout,
    require(name) { return Object.hasOwn(deps, name) ? deps[name] : localRequire(name); },
    Page(value) { definition = value; }, Component(value) { definition = value; }, ...globals,
  }, { filename });
  return definition ? { ...definition, data: plain(definition.data), setData(value) { Object.assign(this.data, plain(value)); } } : module.exports;
}
function page(name, overrides = {}, deps = {}) {
  const urls = []; const modals = []; const files = [];
  const api = { isLive: () => true, advisorStatus: () => Promise.resolve(status), advisorCapabilities: () => Promise.resolve(caps),
    payProducts: () => Promise.resolve({ items: [] }), advisorTasks: () => Promise.resolve({ items: tasks }), ...overrides };
  const helpers = { ...require('../utils/advisor.js'), openResult: (id) => { files.push(id); return Promise.resolve(); } };
  const view = load(`pages/${name}/index.js`, { '../../utils/api.js': api, '../../utils/advisor.js': helpers, ...deps }, {
    wx: { setNavigationBarTitle() {}, navigateTo: ({ url }) => urls.push(url), showModal(options) { modals.push(options); options.success({ confirm: true }); } },
  });
  view.onLoad({ docId: '42', title: encodeURIComponent('通知 & 标题') }); view.onShow();
  return { view, urls, modals, files, text: () => render(fs.readFileSync(path.join(ROOT, `pages/${name}/index.wxml`), 'utf8'), view.data) };
}

test('合并首页同时显示两区块、独立次数与模型，最近任务混排并按四种类型跳转', async () => {
  const h = page('advisor'); await flush();
  for (const text of ['数据工具', '批量名单比对', '客户相关通知表', 'AI 内部测试', '今天剩余 4 次', '今天剩余 2 次', '模型：测试模型｜备案号：测试编号']) assert.ok(h.text().includes(text), text);
  assert.deepEqual(h.view.data.skills.map((skill) => skill.remaining), [2, 1]);
  assert.deepEqual(h.view.data.recent.map((task) => task.id), [4, 3, 2]);
  for (const id of [4, 3, 2]) h.view.tasks({ currentTarget: { dataset: { id } } });
  assert.deepEqual(h.urls, ['/pages/advisor-clause/index?taskId=4', '/pages/advisor-tasks/index?taskId=3', '/pages/advisor-interpret/index?taskId=2']);
  h.view.setData({ recent: [tasks[0]] }); h.view.tasks({ currentTarget: { dataset: { id: 1 } } });
  assert.equal(h.urls.at(-1), '/pages/advisor-tasks/index?taskId=1');
  h.view.interpret(); assert.equal(h.urls.at(-1), '/pages/advisor-interpret/index?docId=42&title=' + encodeURIComponent('通知 & 标题'));
  h.view.clause(); assert.equal(h.urls.at(-1), '/pages/advisor-clause/index?docId=42&title=' + encodeURIComponent('通知 & 标题'));
  h.view.onUnload();
});

test('数据状态失败或一直未返回时 AI 照常显示，商品未返回也不阻塞数据卡片', async () => {
  for (const advisorStatus of [() => Promise.reject(new Error('数据失败')), () => new Promise(() => {})]) {
    const h = page('advisor', { advisorStatus }); await flush();
    assert.equal(h.view.data.aiEnabled, true); assert.equal(h.view.data.aiLoading, false);
    assert.ok(h.text().includes('今天剩余 4 次')); assert.deepEqual(h.view.data.recent.map((task) => task.kind), ['clause', 'interpret']); h.view.onUnload();
  }
  const h = page('advisor', { payProducts: () => new Promise(() => {}) }); await flush();
  assert.equal(h.view.data.enabled, true); assert.equal(h.view.data.loading, false); assert.equal(h.view.data.skills.length, 2); h.view.onUnload();
});

test('AI 关闭、失败或一直未返回时数据卡片与历史任务照常显示', async () => {
  for (const advisorCapabilities of [() => Promise.resolve({}), () => Promise.reject(new Error('AI 失败')), () => new Promise(() => {})]) {
    const h = page('advisor', { advisorCapabilities }); await flush();
    assert.equal(h.view.data.enabled, true); assert.equal(h.view.data.loading, false);
    assert.deepEqual(h.view.data.recent.map((task) => task.kind), ['track', 'compare']);
    assert.doesNotMatch(h.text(), /AI|内部测试|政策解读|条款对照/); h.view.onUnload();
  }
});

test('四种任务各自打开结果，取消和删除保留各自的说明及接口调用', async () => {
  const cancelled = []; const removed = [];
  const h = page('advisor-tasks', { cancelAdvisorTask: (id) => { cancelled.push(id); return Promise.resolve(); }, deleteAdvisorTask: (id) => { removed.push(id); return Promise.resolve(); } });
  await flush(); assert.deepEqual(h.view.data.items.map((task) => task.kind), ['compare', 'interpret', 'track', 'clause']);
  for (const task of tasks) { h.view.open({ currentTarget: { dataset: { id: task.id } } }); await flush(); }
  assert.deepEqual(h.files, [1, 3]); assert.deepEqual(h.urls, ['/pages/advisor-interpret/index?taskId=2', '/pages/advisor-clause/index?taskId=4']);
  for (const task of tasks) { h.view.cancel({ currentTarget: { dataset: { id: task.id } } }); await flush(); }
  assert.deepEqual(cancelled, [1, 2, 3, 4]); assert.match(h.modals[0].content, /任务提交月份/); assert.match(h.modals[1].content, /今天/); assert.match(h.modals[2].content, /任务提交当天/);
  for (const task of tasks) { h.view.remove({ currentTarget: { dataset: { id: task.id } } }); await flush(); }
  assert.deepEqual(removed, [1, 2, 3, 4]); assert.match(h.modals[4].content, /本机文件/); assert.equal(h.modals[5].content, '删除任务和结果后不能恢复。'); h.view.onUnload();
});

test('AI 权限关闭时仍列数据任务；列表失败时清空旧 AI 任务且禁止操作', async () => {
  let enabled = true; let fail = false; let cancelled = 0;
  const h = page('advisor-tasks', { advisorCapabilities: () => Promise.resolve(enabled ? caps : {}),
    advisorTasks: () => fail ? Promise.reject(new Error('列表失败')) : Promise.resolve({ items: tasks }),
    cancelAdvisorTask: () => { cancelled += 1; return Promise.resolve(); } });
  await flush(); enabled = false; h.view.load(); await flush();
  assert.deepEqual(h.view.data.items.map((task) => task.kind), ['compare', 'track']);
  h.view.openTask({ currentTarget: { dataset: { id: 2 } } }); assert.deepEqual(h.urls, []);
  fail = true; h.view.load(); await flush(); assert.deepEqual(h.view.data.items, []); assert.doesNotMatch(h.text(), /AI|政策解读|条款对照/);
  h.view.cancel({ currentTarget: { dataset: { id: 2 } } }); assert.equal(cancelled, 0); h.view.onUnload();
});

test('悬浮按钮两侧任一开放就显示，两侧失败则隐藏', async () => {
  for (const [data, ai] of [[false, false], [true, false], [false, true], [true, true]]) {
    const view = load('components/tool-fab/index.js', { '../../utils/api.js': {
      advisorStatus: () => data ? Promise.resolve({ enabled: true }) : Promise.reject(new Error('关闭')),
      advisorCapabilities: () => ai ? Promise.resolve(caps) : Promise.reject(new Error('关闭')),
    }, '../../utils/tool-overlays.js': { subscribe: () => () => {} } }, {
      getCurrentPages: () => [], wx: { onKeyboardHeightChange() {}, offKeyboardHeightChange() {}, onWindowResize() {}, offWindowResize() {} },
    });
    Object.assign(view, view.methods); view.properties = Object.fromEntries(Object.entries(view.properties).map(([key, value]) => [key, value.value]));
    view.lifetimes.attached.call(view); view.lifetimes.ready.call(view); await flush();
    const text = render(fs.readFileSync(path.join(ROOT, 'components/tool-fab/index.wxml'), 'utf8'), { ...view.data, ...view.properties });
    assert.equal(text, ai ? 'AI 内测' : data ? '工具' : ''); view.lifetimes.detached.call(view);
  }
});

test('未开放能力只缓存 15 秒，登录或加入白名单后能重新查到；已开放共用缓存', async () => {
  const timers = []; const calls = []; let enabled = false;
  const api = load('utils/api.js', { './config.js': { devBaseUrl: 'https://test.invalid' } }, {
    setTimeout(fn, ms) { const timer = { fn, ms }; timers.push(timer); return timer; }, clearTimeout() {},
    wx: { request(options) { calls.push(options); options.success({ statusCode: 200, data: enabled ? caps : {} }); } },
  });
  const first = api.advisorCapabilities(); assert.equal((await first).ai_advisor.enabled, false);
  assert.equal(api.advisorCapabilities(), first); assert.equal(calls.length, 1);
  enabled = true; timers.filter((timer) => timer.ms === 15000).at(-1).fn();
  const second = api.advisorCapabilities(); assert.notEqual(second, first); assert.equal((await second).ai_advisor.enabled, true);
  assert.equal(api.advisorCapabilities(), second); assert.equal(calls.length, 2);
});
