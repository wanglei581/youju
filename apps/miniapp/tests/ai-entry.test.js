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
// 每个用例独立加载 API；失败重试/缓存定时器必须随用例一起结束。
const liveTimers = new Set();
test.afterEach(() => { liveTimers.forEach(clearTimeout); liveTimers.clear(); });
const trackedTimeout = (fn, delay) => { const id = setTimeout(() => { liveTimers.delete(id); fn(); }, delay); liveTimers.add(id); return id; };
const NOTICE = 'AI 生成 · 内部测试：摘录通知原文原句，可能不全；不判断能否申报，以原文和主管部门解释为准。';
// 10/17：入口是"要点"下面的卡片（标题 AI 解读，能用时有"开始解读"），不再是右下角浮动按钮
const ENTRY_OPEN = (text) => text.startsWith('AI 解读') && text.includes('开始解读');
const caps = (enabled, extra = {}) => ({
  data_tools: { enabled: false, features: [], remaining: {} },
  ai_advisor: enabled === false ? { enabled: false, features: [], remaining: {} }
    : { enabled, features: ['interpret', 'clause'], remaining: { interpret: 5, clause: 5 }, disclosure: { modelLabel: '', filingNo: '' }, ...extra },
});

function load(file, dependencies = {}, globals = {}) {
  const module = { exports: {} }; let definition;
  vm.runInNewContext(read(file), {
    module, Date, Math, Promise, Set, setTimeout: trackedTimeout, clearTimeout,
    require(name) { if (name === '../../utils/regions.js') return require('../utils/regions.js'); assert.ok(Object.hasOwn(dependencies, name), `未提供依赖 ${name}`); return dependencies[name]; },
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
  const view = load('components/ai-entry/index.js', { '../../utils/api.js': api, '../../utils/tool-context.js': context, '../../utils/advisor.js': helpers },
    { wx: { navigateTo(options) { navigations.push(options); } } });
  view.properties = Object.fromEntries(Object.entries(view.properties).map(([key, value]) => [key, value.value]));
  Object.assign(view.properties, properties); Object.assign(view, view.methods);
  view.lifetimes.attached.call(view);
  return {
    view, navigations,
    text: () => render(read('components/ai-entry/index.wxml'), { ...view.data, ...view.properties }),
  };
}

test('入口只在 ai_advisor.enabled 严格为 true 时显示“AI 解读”（内测包也叫这个，10/17），其余返回一律不渲染也不能进入', async () => {
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
    assert.equal(ENTRY_OPEN(button.text()), shown, JSON.stringify(response)); if (!shown) assert.equal(button.text(), '');
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
  assert.ok(ENTRY_OPEN(button.text()));
  button.view.open(); button.view.open(); assert.equal(button.navigations.length, 1);
  const url = button.navigations[0].url;
  // 10/17：直接进解读页，不再先进工具页
  assert.equal(url, `/pages/advisor-interpret/index?docId=42&title=${encodeURIComponent('通知：科技 & 申报（50%）')}`);
  const query = Object.fromEntries(new URL(`https://test.invalid${url}`).searchParams);
  query.title = encodeURIComponent(query.title); // 小程序 onLoad 里再解码一次
  assert.deepEqual(context.read(query), { docId: '42', title: '通知：科技 & 申报（50%）', label: '当前通知：通知：科技 & 申报（50%）' });
  button.navigations[0].complete(); assert.equal(button.view.data.navigating, false);
  assert.equal(context.url({}), '/pages/advisor/index'); assert.equal(context.read({}), null);
  // 这份文件只能做条款对照（详情接口 ai.interpret 为假）时直接进条款对照
  const onlyClause = fab(api, { docId: '43', docTitle: '只能对照', ai: { open: false, interpret: false, clause: true } }); await flush();
  assert.match(onlyClause.text(), /^AI 解读.*和我的企业对一下/); assert.doesNotMatch(onlyClause.text(), /开始解读/); onlyClause.view.open();
  assert.equal(onlyClause.navigations[0].url, `/pages/advisor-clause/index?docId=43&title=${encodeURIComponent('只能对照')}`);
});

test('同一次启动多个页面共用一个请求，失败也缓存；AI 页面 refresh 才重新请求；关掉后入口随之消失', async () => {
  const harness = apiHarness();
  const one = fab(harness.api); const two = fab(harness.api);
  assert.equal(harness.calls(), 1);
  harness.pending[0].success({ statusCode: 200, data: caps(true) }); await flush(); await flush();
  assert.ok(ENTRY_OPEN(one.text())); assert.ok(ENTRY_OPEN(two.text()));
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
    const usesFab = !!(JSON.parse(read(`${route}.json`)).usingComponents || {})['ai-entry'];
    if (name === 'detail') {
      assert.match(template, /<ai-entry blocked="\{\{!doc\}\}" doc-id="\{\{doc.id\}\}" doc-title="\{\{doc.title\}\}" ai="\{\{doc.ai\}\}" body-status="\{\{doc.body \? doc.body.status : ''\}\}" body-total="\{\{doc.body && doc.body.total \? doc.body.total : 0\}\}" \/>/);
      // 卡片紧跟在"要点"后面，不再浮在正文上
      assert.ok(template.indexOf('<ai-entry') > template.indexOf('要点') && template.indexOf('<ai-entry') < template.indexOf('原文摘录'));
      assert.equal(usesFab, true);
    } else {
      assert.doesNotMatch(template, /<ai-entry\b|<tool-fab\b/, route); assert.equal(usesFab, false, route);
    }
  }
  // 详情页原有的底部留白不变，非白名单用户看到的版面和原来一样
  assert.match(read('pages/detail/index.wxml'), /<view style="height: 220rpx"><\/view>/);
  assert.equal(read('app.json').includes('components/ai-entry'), false); assert.equal(read('app.json').includes('components/tool-fab'), false);
});

test('非白名单用户在任何页面都看不到 AI 字样：AI 文案全部在 aiEnabled 条件里，原有页面没有新增', () => {
  for (const file of ['components/ai-entry/index.wxml'].concat(AI_PAGES.map((name) => `pages/${name}/index.wxml`))) {
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
        if (name === 'advisor-tasks') assert.equal(titles.at(-1), '我的记录');
        else assert.match(titles.at(-1), /内部测试/, name);
      } else {
        assert.doesNotMatch(output, /AI|内部测试|政策解读|条款对照/, name);
        assert.match(output, /暂时没有可用工具/, name);
        assert.ok(titles.every((title) => !/AI|内部测试/.test(title)), name);
      }
      page.onUnload ? page.onUnload() : page.onHide();
    }
  }
});

test('AI 正式版（config.aiInternal 为 false，D-066）：顶部只写"AI 生成"，标题不带"内部测试"；登录和同意只看协议是否定稿', async () => {
  const release = load('utils/advisor.js', { './config.js': { aiInternal: false } });
  assert.equal(release.AI_LABEL, 'AI 生成：');
  assert.equal(release.aiTitle('政策解读'), '政策解读');
  assert.equal(release.AI_INTERNAL, false);
  const internal = load('utils/advisor.js', { './config.js': { aiInternal: true } });
  assert.equal(internal.AI_LABEL, 'AI 生成 · 内部测试：');
  assert.equal(internal.aiTitle('政策解读'), '政策解读 · 内部测试');
  // 正式版的四个页面：顶部是"AI 生成：" + 说明，标题不带"内部测试"
  for (const name of AI_PAGES) {
    const titles = [];
    const api = { advisorCapabilities: () => Promise.resolve(caps(true)), advisorTasks: () => Promise.resolve({ items: [] }), advisorTask: (id) => Promise.resolve({ id, status: 'running' }) };
    const storage = new Map();
    const wx = { setNavigationBarTitle: ({ title }) => titles.push(title), getStorageSync: (key) => storage.get(key), setStorageSync: (key, value) => storage.set(key, value) };
    const page = load(`pages/${name}/index.js`, { '../../utils/api.js': api, '../../utils/advisor.js': release, '../../utils/tool-context.js': context,
      '../../utils/ai-interpret.js': require('../utils/ai-interpret.js'), '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': require('../utils/format.js') }, { wx });
    if (page.onLoad) page.onLoad({ docId: '42', title: '通知' });
    page.onShow(); await flush(); await flush();
    const output = render(read(`pages/${name}/index.wxml`), page.data);
    assert.ok(output.startsWith('AI 生成：摘录通知原文原句'), name);
    assert.doesNotMatch(output, /内部测试/, name);
    assert.ok(titles.length > 0 && titles.every((title) => !/内部测试/.test(title)), name);
    page.onUnload ? page.onUnload() : page.onHide();
  }
  // 登录和同意：正式版只看 legal.final；内测包协议是草案也开放
  const consentWith = (aiInternal, final) => load('utils/consent.js', {
    '../content/legal.js': { final, version: 'v-test' }, './api.js': { isLive: () => true }, './exports.js': {}, './exporter.js': {}, './config.js': { aiInternal },
  }, { wx: { getStorageSync: () => '', setStorageSync() {}, showToast() {}, showModal() {} } });
  assert.equal(consentWith(false, false).loginOpen(), false);
  assert.equal(consentWith(false, false).consentOpen(), false);
  assert.equal(consentWith(false, true).loginOpen(), true);
  assert.equal(consentWith(false, true).consentOpen(), true);
  assert.equal(consentWith(true, false).loginOpen(), true);
  assert.equal(consentWith(true, false).consentOpen(), true);
});

test('内部测试标注：后端 disclosure 带说明文字时优先显示后端的，否则用固定文字', async () => {
  assert.equal(`AI 生成 · ${helpers.INTERNAL_PREFIX}${helpers.internalNotice(undefined)}`, NOTICE);
  for (const value of [null, {}, { modelLabel: '模型', filingNo: '编号' }, { text: '  ' }, '', 3]) {
    assert.equal(`AI 生成 · ${helpers.INTERNAL_PREFIX}${helpers.internalNotice(value)}`, NOTICE);
  }
  assert.equal(helpers.internalNotice('后端说明文字'), '后端说明文字');
  assert.equal(helpers.internalNotice({ text: '后端说明', modelLabel: '模型' }), '后端说明');
  assert.equal(helpers.internalNotice({ text: '内部测试：后端说明' }), '后端说明');
  const api = { advisorCapabilities: () => Promise.resolve(caps(true, { disclosure: { text: '后端给出的 AI 生成说明', modelLabel: '模型甲', filingNo: '编号乙' } })), advisorTasks: () => Promise.resolve({ items: [] }) };
  const page = load('pages/advisor/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers, '../../utils/tool-context.js': context }, { wx: { setNavigationBarTitle() {} } });
  page.onLoad({}); page.onShow(); await flush(); await flush();
  const output = render(read('pages/advisor/index.wxml'), page.data);
  assert.match(output, /内部测试：后端给出的 AI 生成说明/); assert.doesNotMatch(output, /摘录通知原文原句，可能不全/);
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
  status = 'done'; timers[0](); await flush(); await flush(); assert.equal(page.data.items[0].stateLabel, '已完成');
  page.cancel({ currentTarget: { dataset: { id: 7 } } }); await flush(); await flush();
  page.remove({ currentTarget: { dataset: { id: 7 } } }); await flush(); await flush();
  assert.deepEqual(calls, [['cancel', 7], ['delete', 7]]);
  page.onHide();
});

// ---------- AI 正式版（config.aiInternal 为 false，D-066）：上线即对所有用户开放 ----------
const release = load('utils/advisor.js', { './config.js': { aiInternal: false } });

function releaseFab(api, properties = {}) {
  const navigations = [];
  const view = load('components/ai-entry/index.js', { '../../utils/api.js': api, '../../utils/tool-context.js': context, '../../utils/advisor.js': release },
    { wx: { navigateTo(options) { navigations.push(options); } } });
  view.properties = Object.fromEntries(Object.entries(view.properties).map(([key, value]) => [key, value.value]));
  Object.assign(view.properties, properties); Object.assign(view, view.methods);
  view.lifetimes.attached.call(view);
  return { view, navigations, text: () => render(read('components/ai-entry/index.wxml'), { ...view.data, ...view.properties }) };
}

test('正式版入口：详情说对所有人开放、这份文件能做就显示"AI 解读"，不用先同意协议；名单等做不了的不显示；没开放时只看白名单', async () => {
  const notAgreed = { advisorCapabilities: () => Promise.resolve({ ai_advisor: { enabled: false }, reason: 'consent' }) };
  const open = releaseFab(notAgreed, { docId: '42', docTitle: '通知', ai: { open: true, interpret: true, clause: true } }); await flush();
  assert.ok(ENTRY_OPEN(open.text())); open.view.open(); assert.equal(open.navigations.length, 1);
  // 10/17：做不了的文件也显示卡片，写明原因，不给按钮
  const list = releaseFab(notAgreed, { docId: '43', docTitle: '名单', ai: { open: true, interpret: false, clause: false }, bodyStatus: 'list' }); await flush();
  assert.match(list.text(), /^AI 解读 名单类文件不提供 AI 解读/); assert.doesNotMatch(list.text(), /开始解读/);
  list.view.open(); assert.equal(list.navigations.length, 0);
  const closed = releaseFab(notAgreed, { docId: '42', docTitle: '通知', ai: { open: false, interpret: true, clause: true } }); await flush();
  assert.equal(closed.text(), '');
  const whitelisted = releaseFab({ advisorCapabilities: () => Promise.resolve(caps(true)) }, { docId: '42', docTitle: '通知', ai: { open: false, interpret: true, clause: false } }); await flush();
  assert.ok(ENTRY_OPEN(whitelisted.text()));
  const whitelistedList = releaseFab({ advisorCapabilities: () => Promise.resolve(caps(true)) }, { docId: '43', docTitle: '名单', ai: { open: false, interpret: false, clause: false }, bodyStatus: 'list' }); await flush();
  assert.match(whitelistedList.text(), /名单类文件不提供 AI 解读/); assert.doesNotMatch(whitelistedList.text(), /开始解读/);
  // 旧后台没有 ai 字段：只看白名单
  const legacy = releaseFab({ advisorCapabilities: () => Promise.resolve(caps(true)) }, { docId: '42', docTitle: '通知' }); await flush();
  assert.ok(ENTRY_OPEN(legacy.text()));
  // 详情晚到：ai 字段变化后重新判断
  const late = releaseFab(notAgreed, { docId: '42', docTitle: '通知', ai: null }); await flush();
  assert.equal(late.text(), '');
  late.view.properties.ai = { open: true, interpret: true, clause: false }; late.view.observers.ai.call(late.view); await flush();
  assert.ok(ENTRY_OPEN(late.text())); assert.doesNotMatch(late.text(), /和我的企业对一下/);
});

test('卡片在做不了的文件上写明原因、不给按钮（10/17 创始人：按钮时有时无看不懂）', async () => {
  const api = { advisorCapabilities: () => Promise.resolve(caps(true)) };
  const none = { open: false, interpret: false, clause: false };
  const cases = [
    [{ bodyStatus: 'list' }, '名单类文件不提供 AI 解读。要查企业在不在名单上，可以用“查名单”。'],
    [{ bodyStatus: 'original' }, '这类文件不提供 AI 解读，请复制原文链接查看原文。'],
    [{ bodyStatus: 'ok', bodyTotal: 24001 }, '正文超过 2.4 万字，暂不能解读，请查看原文。'],
    [{ bodyStatus: '' }, '这份文件没有可公开的完整正文，暂不能解读，请查看原文。'],
    [{ bodyStatus: 'ok', bodyTotal: 800 }, '这份文件暂不能解读，请查看原文。'],
  ];
  for (const [body, reason] of cases) {
    const card = fab(api, { docId: '42', docTitle: '通知', ai: none, ...body }); await flush();
    assert.equal(card.text(), `AI 解读 ${reason}`, JSON.stringify(body));
    card.view.open(); card.view.openClause(); assert.equal(card.navigations.length, 0);
  }
  // 能做的文件：正文长短不影响（以后端判断为准）
  const ok = fab(api, { docId: '42', docTitle: '通知', ai: { open: false, interpret: true, clause: true }, bodyStatus: 'ok', bodyTotal: 30000 }); await flush();
  assert.ok(ENTRY_OPEN(ok.text())); ok.view.openClause();
  assert.equal(ok.navigations[0].url, `/pages/advisor-clause/index?docId=42&title=${encodeURIComponent('通知')}`);
  // 非白名单用户：做不了的文件也什么都不渲染
  const outsider = fab({ advisorCapabilities: () => Promise.resolve(caps(false)) }, { docId: '42', ai: none, bodyStatus: 'list' }); await flush();
  assert.equal(outsider.text(), '');
});

test('capabilities 用不了的原因：403 没同意协议、503 和网络失败是连不上（重试完仍失败）、404 和关着是 off', async () => {
  const cases = [
    [{ statusCode: 403, data: { detail: '请先阅读并同意用户协议和隐私保护指引' } }, 'consent'],
    [{ statusCode: 503 }, 'offline'],
    ['fail', 'offline'],
    [{ statusCode: 404, data: { detail: '没有找到接口' } }, 'off'],
    [{ statusCode: 200, data: caps(false) }, 'off'],
    [{ statusCode: 200, data: { ai_advisor: { enabled: false, reason: 'consent' } } }, 'consent'],
    [{ statusCode: 200, data: caps(true) }, ''],
  ];
  for (const [response, reason] of cases) {
    // 假定时器：自动重试的等待立即执行，重试完才给结果
    const timers = []; const pending = []; const cancelled = new Set(); let clock = 0; let timerId = 0;
    const api = load('utils/api.js', { './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {} },
      { wx: { request(options) { pending.push(options); }, removeStorageSync() {}, showToast() {}, hideToast() {}, showLoading() {}, hideLoading() {} },
        Date: { now: () => clock },
        setTimeout: (fn, delay = 0) => { const id = ++timerId; timers.push(() => { if (!cancelled.has(id)) { clock += delay; fn(); } }); return id; },
        clearTimeout(id) { cancelled.add(id); } });
    let result; api.advisorCapabilities({ refresh: true }).then((value) => { result = value; });
    for (let round = 0; round < 60 && result === undefined; round += 1) {
      while (pending.length) { const request = pending.shift(); if (response === 'fail') request.fail({ errMsg: 'request:fail' }); else request.success(response); }
      await flush(); await flush();
      // 每轮只推进已有定时器，再处理新发出的请求；模拟时间而非等真实的90秒。
      const scheduled = timers.splice(0);
      for (const runTimer of scheduled) { if (result !== undefined) break; runTimer(); await flush(); }
    }
    assert.notEqual(result, undefined, JSON.stringify(response));
    assert.equal(result.reason, reason, JSON.stringify(response));
  }
});

test('正式版四个 AI 页面：没同意协议时引导同意（点按钮走同意流程，回来自动刷新），连不上时给重试，关着时写"AI 功能暂时不可用"', async () => {
  for (const [reason, text, button] of [['consent', '请先阅读并同意《用户协议》和《隐私保护指引》', '阅读并同意'], ['offline', '暂时连不上服务器', '重试'], ['off', 'AI 功能暂时不可用，请稍后再试', '']]) {
    for (const name of AI_PAGES) {
      let consentCalls = 0; let capabilityCalls = 0;
      const api = { advisorCapabilities: () => { capabilityCalls += 1; return Promise.resolve({ ai_advisor: { enabled: false, features: [], remaining: {} }, reason }); }, advisorTasks: () => Promise.resolve({ items: [] }) };
      const storage = new Map();
      const wx = { setNavigationBarTitle() {}, getStorageSync: (key) => storage.get(key), setStorageSync: (key, value) => storage.set(key, value) };
      const page = load(`pages/${name}/index.js`, { '../../utils/api.js': api, '../../utils/advisor.js': release, '../../utils/tool-context.js': context,
        '../../utils/ai-interpret.js': require('../utils/ai-interpret.js'), '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': require('../utils/format.js'),
        '../../utils/consent.js': { ensureConsent: () => { consentCalls += 1; return Promise.resolve(true); } } }, { wx });
      if (page.onLoad) page.onLoad({ docId: '42', title: '通知' });
      page.onShow(); await flush(); await flush();
      const output = render(read(`pages/${name}/index.wxml`), page.data);
      assert.match(output, new RegExp(text), `${name} ${reason}`);
      if (button) assert.match(output, new RegExp(button), `${name} ${reason}`);
      else assert.doesNotMatch(output, /阅读并同意|重试/, `${name} ${reason}`);
      const before = capabilityCalls;
      if (reason === 'consent') { page.consent(); await flush(); await flush(); assert.equal(consentCalls, 1, name); assert.ok(capabilityCalls > before, name); }
      if (reason === 'offline') { page.reload(); await flush(); assert.ok(capabilityCalls > before, name); }
      page.onUnload ? page.onUnload() : page.onHide();
    }
  }
});

test('投诉举报页：写明可以投诉什么、怎么提交、处理流程和 15 个工作日的反馈时限；AI 页面底部有入口', () => {
  const output = read('pages/complaint/index.wxml');
  for (const text of ['可以投诉举报什么', '怎么提交', '处理流程和时限', '15 个工作日内答复处理结果', '联系邮箱']) assert.ok(output.includes(text), text);
  assert.ok(JSON.parse(read('app.json')).pages.includes('pages/complaint/index'));
  for (const name of ['advisor', 'advisor-interpret', 'advisor-clause']) {
    assert.match(read(`pages/${name}/index.wxml`), /bindtap="report">投诉举报</, name);
    assert.match(read(`pages/${name}/index.js`), /\/pages\/complaint\/index/, name);
  }
});
