// AI 内测入口的显示和隐藏（只上传体验版、不合并）：只有 capabilities.ai_advisor.enabled 严格为 true 才出现，
// 其余情况（关闭、不在白名单、未同意协议 403、接口 404/503、网络失败、演示数据）什么都不渲染、不能进入。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = require('../utils/tool-context.js');
const helpers = require('../utils/advisor.js');
const { render, assertAiGuarded } = require('./helpers/wxml.js');

const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const AI_PAGES = ['advisor', 'advisor-interpret', 'advisor-clause', 'advisor-tasks'];
const NOTICE = '内部测试：结果由 AI 模型根据原文生成，每条都附原文引用，可能不全，请以原文为准，不作为能否申报的判断。';
const caps = (enabled, extra = {}) => ({
  data_tools: { enabled: false, features: [], remaining: {} },
  ai_advisor: enabled === false ? { enabled: false, features: [], remaining: {} }
    : { enabled, features: ['interpret', 'clause'], remaining: { interpret: 5, clause: 5 }, disclosure: { modelLabel: '', filingNo: '' }, ...extra },
});

function load(file, dependencies = {}, globals = {}) {
  const module = { exports: {} }; let definition;
  vm.runInNewContext(read(file), {
    module, Date, Math, Promise, Set, setTimeout, clearTimeout,
    require(name) { assert.ok(Object.hasOwn(dependencies, name), `未提供依赖 ${name}`); return dependencies[name]; },
    Page(value) { definition = value; }, Component(value) { definition = value; },
    ...globals,
  }, { filename: file });
  if (!definition) return module.exports;
  return { ...definition, data: plain(definition.data), setData(values) { Object.assign(this.data, plain(values)); } };
}

// 用真实的 utils/api.js，模拟线上 wx.request 的各种返回。
function apiHarness(config = { devBaseUrl: 'https://test.invalid' }) {
  const pending = []; let calls = 0;
  const api = load('utils/api.js', {
    './config.js': config, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {},
  }, { wx: { request(options) { calls += 1; pending.push(options); }, removeStorageSync() {} } });
  return { api, pending, calls: () => calls };
}

function fab(api, properties = {}) {
  const navigations = [];
  const view = load('components/tool-fab/index.js', { '../../utils/api.js': api, '../../utils/tool-context.js': context },
    { wx: { navigateTo(options) { navigations.push(options); } } });
  view.properties = Object.fromEntries(Object.entries(view.properties).map(([key, value]) => [key, value.value]));
  Object.assign(view.properties, properties); Object.assign(view, view.methods);
  view.lifetimes.attached.call(view);
  return {
    view, navigations,
    text: () => render(read('components/tool-fab/index.wxml'), { ...view.data, ...view.properties }),
  };
}

test('入口只在 ai_advisor.enabled 严格为 true 时显示“AI 内测”，其余返回一律不渲染也不能进入', async () => {
  const cases = [
    [{ statusCode: 200, data: caps(true) }, true],
    [{ statusCode: 200, data: caps(false) }, false],
    [{ statusCode: 200, data: caps('true') }, false],
    [{ statusCode: 200, data: caps(1) }, false],
    [{ statusCode: 200, data: { data_tools: { enabled: true, features: ['compare', 'track'] } } }, false],
    [{ statusCode: 200, data: null }, false],
    [{ statusCode: 403, data: { detail: '请先阅读并同意用户协议和隐私保护指引' } }, false],
    [{ statusCode: 404, data: { detail: '没有找到接口' } }, false],
    [{ statusCode: 503 }, false],
    ['fail', false],
  ];
  for (const [response, shown] of cases) {
    const harness = apiHarness(); const button = fab(harness.api, { docId: '42', docTitle: '通知标题' });
    assert.equal(button.text(), '', '请求返回前不显示');
    const request = harness.pending[0];
    assert.equal(request.url, 'https://test.invalid/api/v1/advisor/capabilities');
    if (response === 'fail') request.fail(); else request.success(response);
    await flush(); await flush();
    assert.equal(button.text(), shown ? 'AI 内测' : '', JSON.stringify(response));
    assert.equal(/AI/.test(button.text()), shown);
    button.view.open();
    assert.equal(button.navigations.length, shown ? 1 : 0);
  }
});

test('入口带当前通知进入；通知没加载完（blocked）时不显示、不导航；重复点击只导航一次', async () => {
  const api = { advisorCapabilities: () => Promise.resolve(caps(true)) };
  const blocked = fab(api, { blocked: true, docId: '42', docTitle: '通知' }); await flush();
  assert.equal(blocked.text(), ''); blocked.view.open(); assert.equal(blocked.navigations.length, 0);
  const button = fab(api, { docId: '42', docTitle: '通知：科技 & 申报（50%）' }); await flush();
  assert.equal(button.text(), 'AI 内测');
  button.view.open(); button.view.open(); assert.equal(button.navigations.length, 1);
  const url = button.navigations[0].url;
  assert.equal(url, `/pages/advisor/index?docId=42&title=${encodeURIComponent('通知：科技 & 申报（50%）')}`);
  const query = Object.fromEntries(new URL(`https://test.invalid${url}`).searchParams);
  query.title = encodeURIComponent(query.title); // 小程序 onLoad 里再解码一次
  assert.deepEqual(context.read(query), { docId: '42', title: '通知：科技 & 申报（50%）', label: '当前通知：通知：科技 & 申报（50%）' });
  button.navigations[0].complete(); assert.equal(button.view.data.navigating, false);
  assert.equal(context.url({}), '/pages/advisor/index'); assert.equal(context.read({}), null);
});

test('同一次启动多个页面共用一个请求，失败也缓存；AI 页面 refresh 才重新请求；关掉后入口随之消失', async () => {
  const harness = apiHarness();
  const one = fab(harness.api); const two = fab(harness.api);
  assert.equal(harness.calls(), 1);
  harness.pending[0].success({ statusCode: 200, data: caps(true) }); await flush(); await flush();
  assert.equal(one.text(), 'AI 内测'); assert.equal(two.text(), 'AI 内测');
  one.view.pageLifetimes.show.call(one.view); await flush(); assert.equal(harness.calls(), 1);
  const refreshed = harness.api.advisorCapabilities({ refresh: true });
  assert.equal(harness.api.advisorCapabilities({ refresh: true }), refreshed); assert.equal(harness.calls(), 2);
  harness.pending[1].success({ statusCode: 200, data: caps(false) }); await refreshed;
  one.view.pageLifetimes.show.call(one.view); await flush(); assert.equal(one.text(), '');
  const failing = apiHarness(); fab(failing.api); failing.pending[0].fail(); await flush();
  fab(failing.api); await flush(); assert.equal(failing.calls(), 1);
});

test('演示数据（没配云托管和本机后端）不请求、不显示', async () => {
  const harness = apiHarness({ cloudEnv: '', devBaseUrl: '' });
  const button = fab(harness.api); await flush(); await flush();
  assert.equal(harness.calls(), 0); assert.equal(button.text(), '');
  assert.equal((await harness.api.advisorCapabilities({ refresh: true })).ai_advisor.enabled, false);
  await assert.rejects(harness.api.createAdvisorTask({ kind: 'interpret', doc_id: 1 }, 'k'), (err) => err.status === 404);
});

test('组件卸载后晚到的结果不再写入', async () => {
  let resolve;
  const late = fab({ advisorCapabilities: () => new Promise((done) => { resolve = done; }) });
  late.view.lifetimes.detached.call(late.view); resolve(caps(true)); await flush();
  assert.equal(late.view.data.aiEnabled, false);
});

test('入口只放在通知详情页，绑定当前通知；其他页面不引用入口组件', () => {
  for (const route of JSON.parse(read('app.json')).pages) {
    const name = route.split('/')[1]; const template = read(`${route}.wxml`);
    const usesFab = !!(JSON.parse(read(`${route}.json`)).usingComponents || {})['tool-fab'];
    if (name === 'detail') {
      assert.match(template, /<tool-fab bottom-rpx="\{\{130\}\}" blocked="\{\{!doc\}\}" doc-id="\{\{doc.id\}\}" doc-title="\{\{doc.title\}\}" \/>/);
      assert.equal(usesFab, true);
    } else {
      assert.doesNotMatch(template, /<tool-fab\b/, route); assert.equal(usesFab, false, route);
    }
  }
  // 详情页原有的底部留白不变，非白名单用户看到的版面和原来一样
  assert.match(read('pages/detail/index.wxml'), /<view style="height: 220rpx"><\/view>/);
  assert.equal(read('app.json').includes('components/tool-fab'), false);
});

test('非白名单用户在任何页面都看不到 AI 字样：AI 文案全部在 aiEnabled 条件里，原有页面没有新增', () => {
  for (const file of ['components/tool-fab/index.wxml'].concat(AI_PAGES.map((name) => `pages/${name}/index.wxml`))) {
    assertAiGuarded(read(file));
    assert.doesNotMatch(read(file), /智能|即将推出/);
  }
  const aiRoutes = AI_PAGES.map((name) => `pages/${name}/index`);
  for (const route of JSON.parse(read('app.json')).pages.filter((item) => !aiRoutes.includes(item))) {
    const visible = read(`${route}.wxml`).replace(/<!--[^]*?-->/g, '');
    assert.doesNotMatch(visible, /\bAI\b|内部测试/, route);
  }
});

test('四个 AI 页面：没开放时只显示“暂时没有可用工具”，开放后顶部醒目标注内部测试和 AI 生成', async () => {
  const interpret = require('../utils/ai-interpret.js');
  const clause = require('../utils/ai-clause.js');
  const format = require('../utils/format.js');
  for (const enabled of [false, true]) {
    const titles = [];
    const api = {
      advisorCapabilities: () => Promise.resolve(caps(enabled)),
      advisorTasks: () => Promise.resolve({ items: [] }),
      advisorTask: (id) => Promise.resolve({ id, status: 'running' }),
    };
    const storage = new Map();
    const wx = { setNavigationBarTitle: ({ title }) => titles.push(title), getStorageSync: (key) => storage.get(key), setStorageSync: (key, value) => storage.set(key, value) };
    const dependencies = { '../../utils/api.js': api, '../../utils/advisor.js': helpers, '../../utils/tool-context.js': context,
      '../../utils/ai-interpret.js': interpret, '../../utils/ai-clause.js': clause, '../../utils/format.js': format };
    for (const name of AI_PAGES) {
      const page = load(`pages/${name}/index.js`, dependencies, { wx });
      if (page.onLoad) page.onLoad({ docId: '42', title: '通知' });
      page.onShow(); await flush(); await flush();
      const output = render(read(`pages/${name}/index.wxml`), page.data);
      if (enabled) {
        // 标注在页面最上方（notice-bar 之后、其他内容之前）
        assert.ok(output.startsWith(NOTICE), name);
        assert.match(titles.at(-1), /内部测试/, name);
      } else {
        assert.doesNotMatch(output, /AI|内部测试|政策解读|条款对照/, name);
        assert.match(output, /暂时没有可用工具/, name);
        assert.ok(titles.every((title) => !/AI|内部测试/.test(title)), name);
      }
      page.onUnload ? page.onUnload() : page.onHide();
    }
  }
});

test('内部测试标注：后端 disclosure 带说明文字时优先显示后端的，否则用固定文字', async () => {
  assert.equal(helpers.INTERNAL_PREFIX + helpers.internalNotice(undefined), NOTICE);
  for (const value of [null, {}, { modelLabel: '模型', filingNo: '编号' }, { text: '  ' }, '', 3]) {
    assert.equal(helpers.INTERNAL_PREFIX + helpers.internalNotice(value), NOTICE);
  }
  assert.equal(helpers.internalNotice('后端说明文字'), '后端说明文字');
  assert.equal(helpers.internalNotice({ text: '后端说明', modelLabel: '模型' }), '后端说明');
  assert.equal(helpers.internalNotice({ text: '内部测试：后端说明' }), '后端说明');
  const api = { advisorCapabilities: () => Promise.resolve(caps(true, { disclosure: { text: '后端给出的 AI 生成说明', modelLabel: '模型甲', filingNo: '编号乙' } })), advisorTasks: () => Promise.resolve({ items: [] }) };
  const page = load('pages/advisor/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers, '../../utils/tool-context.js': context }, { wx: { setNavigationBarTitle() {} } });
  page.onLoad({}); page.onShow(); await flush(); await flush();
  const output = render(read('pages/advisor/index.wxml'), page.data);
  assert.match(output, /内部测试：后端给出的 AI 生成说明/); assert.doesNotMatch(output, /结果由 AI 模型根据原文生成/);
  assert.match(output, /模型：模型甲｜备案号：编号乙/);
});

test('任务页取消、删除走线上接口并刷新；进行中的任务轮询直到结束', async () => {
  const calls = []; let status = 'queued';
  const api = {
    advisorCapabilities: () => Promise.resolve(caps(true)),
    advisorTasks: () => Promise.resolve({ items: [{ id: 7, kind: 'interpret', status, docTitle: '通知' }] }),
    advisorTask: (id) => Promise.resolve({ id, kind: 'interpret', status: 'done', hasResult: true, docTitle: '通知' }),
    cancelAdvisorTask: (id) => { calls.push(['cancel', id]); status = 'cancelled'; return Promise.resolve({}); },
    deleteAdvisorTask: (id) => { calls.push(['delete', id]); return Promise.resolve(); },
  };
  const timers = [];
  const page = load('pages/advisor-tasks/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers }, {
    wx: { setNavigationBarTitle() {}, showModal: (options) => options.success({ confirm: true }), navigateTo() {} },
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
  });
  page.onShow(); await flush(); await flush();
  assert.equal(page.data.items[0].stateLabel, '排队中'); assert.equal(timers.length, 1);
  assert.match(render(read('pages/advisor-tasks/index.wxml'), page.data), /取消/);
  timers[0](); await flush(); await flush(); assert.equal(page.data.items[0].stateLabel, '已完成');
  page.cancel({ currentTarget: { dataset: { id: 7 } } }); await flush(); await flush();
  page.remove({ currentTarget: { dataset: { id: 7 } } }); await flush(); await flush();
  assert.deepEqual(calls, [['cancel', 7], ['delete', 7]]);
  page.onHide();
});
