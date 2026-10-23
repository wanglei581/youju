// AI 内测入口的显示和隐藏（只上传体验版、不合并）：只有 capabilities.ai_advisor.enabled 严格为 true 才出现，
// 其余情况（关闭、不在白名单、未同意协议 403、接口 404/503、网络失败、演示数据）什么都不渲染、不能进入。
const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
require('./helpers/internal-build.js'); // 本文件按 AI 内测包测，见该文件说明
const context = require('../utils/tool-context.js');
const helpers = require('../utils/advisor.js');
const { render, assertAiGuarded } = require('./helpers/wxml.js');

const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));
// 有 AI 内容的工具页。批量工具页（pages/advisor，原工具中心）10/22 起不放 AI（AI 只留一个家，在底栏「AI」），
// 按普通页面查"没有 AI 字样"；没同意协议、连不上、关着时的提示照样和这几页一起测（TOOL_PAGES）
const AI_PAGES = ['advisor-interpret', 'advisor-clause', 'advisor-tasks'];
const TOOL_PAGES = ['advisor'].concat(AI_PAGES);
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
    require(name) { if (name === '../../utils/regions.js') return require('../utils/regions.js'); if (name === '../../utils/boost.js') return require('../utils/boost.js'); if (!Object.hasOwn(dependencies, name) && /ai-access\.js$/.test(name)) return AI_OFF; if (!Object.hasOwn(dependencies, name) && /daywatch\.js$/.test(name)) return require('./helpers/day-off.js'); assert.ok(Object.hasOwn(dependencies, name), `未提供依赖 ${name}`); return dependencies[name]; },
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

// 卡片显不显示只听 AI 板块状态（D-075）：用真的 utils/ai-access.js，同一个 api 共用一份（同一次启动里各页面共用）。
// 没给 isLive、advisorMode 的替身补上：连着真实服务、旧后台没有开放状态接口（404），往下看白名单
const accessFor = new Map();
function accessOf(api) {
  if (!accessFor.has(api)) {
    const full = Object.assign({ isLive: () => true, advisorMode: () => Promise.reject(Object.assign(new Error('Not Found'), { status: 404 })), forgetCapabilities() {} }, api);
    const saved = new Map();
    accessFor.set(api, load('utils/ai-access.js', { './api.js': full },
      { wx: { getStorageSync: (key) => saved.get(key), setStorageSync: (key, value) => saved.set(key, value), removeStorageSync: (key) => saved.delete(key) } }));
  }
  return accessFor.get(api);
}
const reasons = require('../utils/ai-reasons.js');

// 详情接口带的 ai：没写就当这份能做（详情接口总带 ai；要测"没有 ai"时显式传 ai: null）
const ELIGIBLE = { interpret: true, clause: true, reason: null };
function fab(api, properties = {}) {
  properties = Object.assign({ ai: ELIGIBLE }, properties);
  const navigations = []; const copied = []; const events = []; const back = [];
  const view = load('components/ai-entry/index.js', {
    '../../utils/ai-access.js': accessOf(api), '../../utils/tool-context.js': context, '../../utils/ai-reasons.js': reasons,
    '../../utils/copy.js': { copyLink: (url) => copied.push(url) },
  }, { wx: { navigateTo(options) { navigations.push(options); }, navigateBack(options) { back.push(options); } } });
  view.properties = Object.fromEntries(Object.entries(view.properties).map(([key, value]) => [key, value.value]));
  Object.assign(view.properties, properties); Object.assign(view, view.methods);
  view.triggerEvent = (name, detail) => events.push([name, detail]);
  view.lifetimes.attached.call(view);
  return {
    view, navigations, copied, events, back,
    text: () => render(read('components/ai-entry/index.wxml'), { ...view.data, ...view.properties }),
  };
}

test('白名单阶段：入口只在能力接口 ai_advisor.enabled 严格为 true 时显示“AI 解读”（先问开放状态，没对所有人开放才看白名单），其余返回一律不渲染也不能进入', async () => {
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
    // D-075：先问 AI 对不对所有人开放（只问接口服务）；白名单阶段说没有，再看能力接口
    const mode = harness.pending[0];
    assert.equal(mode.url, 'https://test.invalid/api/v1/advisor/mode');
    mode.success({ statusCode: 200, data: { open: false, available: false } }); await flush(); await flush();
    assert.equal(button.text(), '', '能力接口回来前不显示');
    const request = harness.pending[1];
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
  assert.equal(context.url, undefined, '不再带着通知打开工具中心（10/22 改成批量工具，只放批量工具）'); assert.equal(context.read({}), null);
  // 这份文件只能做条款对照（详情接口 ai.interpret 为假）时直接进条款对照
  const onlyClause = fab(api, { docId: '43', docTitle: '只能对照', ai: { interpret: false, clause: true, reason: null } }); await flush();
  // 同一件事只用一个名字（D-075）：条款对照，按钮写"开始对照"
  assert.match(onlyClause.text(), /^AI 解读 条款对照：.*开始对照$/); assert.doesNotMatch(onlyClause.text(), /开始解读|和我的企业对一下/); onlyClause.view.open();
  assert.equal(onlyClause.navigations[0].url, `/pages/advisor-clause/index?docId=43&title=${encodeURIComponent('只能对照')}`);
});

test('同一次启动各页面共用一次查询；10 分钟内页面显示不再问；重新问后关掉的入口随之消失；连不上时 15 秒内不重复问', async () => {
  const harness = apiHarness();
  const one = fab(harness.api); const two = fab(harness.api);
  assert.equal(harness.calls(), 1);
  harness.pending[0].success({ statusCode: 200, data: { open: false, available: false } }); await flush(); await flush();
  assert.equal(harness.calls(), 2);
  harness.pending[1].success({ statusCode: 200, data: caps(true) }); await flush(); await flush();
  assert.ok(ENTRY_OPEN(one.text())); assert.ok(ENTRY_OPEN(two.text()));
  one.view.pageLifetimes.show.call(one.view); await flush(); assert.equal(harness.calls(), 2);
  // 重新问一次（AI 首页、提交被拒时会这样）：同时只有一个在途；后台说不在白名单了，两张卡片都收起
  const access = accessOf(harness.api);
  const refreshed = access.refresh(); assert.equal(access.refresh(), refreshed);
  harness.pending[2].success({ statusCode: 200, data: { open: false, available: false } }); await flush(); await flush();
  harness.pending[3].success({ statusCode: 200, data: caps(false) }); await refreshed; await flush();
  assert.equal(one.text(), ''); assert.equal(two.text(), '');
  // 连不上：不下结论（不显示），15 秒内别的页面再显示不重复问
  let asked = 0;
  const offline = { advisorCapabilities: () => { asked += 1; return Promise.resolve({ ai_advisor: { enabled: false }, reason: 'offline' }); } };
  const first = fab(offline); await flush(); await flush(); await flush();
  assert.equal(asked, 1); assert.equal(first.text(), '');
  const second = fab(offline); await flush(); await flush();
  assert.equal(asked, 1); assert.equal(second.text(), '');
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
  await flush(); await flush(); // 开放状态问完（旧后台 404），能力接口还在途
  late.view.lifetimes.detached.call(late.view); resolve(caps(true)); await flush(); await flush();
  assert.equal(late.view.data.aiEnabled, false);
});

test('入口只放在通知详情页，绑定当前通知；其他页面不引用入口组件', () => {
  for (const route of JSON.parse(read('app.json')).pages) {
    const name = route.split('/')[1]; const template = read(`${route}.wxml`);
    const usesFab = !!(JSON.parse(read(`${route}.json`)).usingComponents || {})['ai-entry'];
    if (name === 'detail') {
      assert.match(template, /<view id="ai-entry"><ai-entry blocked="\{\{!doc\}\}" doc-id="\{\{doc.id\}\}" doc-title="\{\{doc.title\}\}" doc-url="\{\{doc.url\}\}" has-attachments="\{\{atts.length > 0\}\}" ai="\{\{doc.ai\}\}" body-status="\{\{doc.body \? doc.body.status : ''\}\}" body-total="\{\{doc.body && doc.body.total \? doc.body.total : 0\}\}" bind:attachments="toAttachments" \/><\/view>/);
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
  // AI 板块首页（D-075）本身就是 AI 页面：AI 关着时底栏不画、直接打开也回首页（ai-section.test.js）
  const aiRoutes = AI_PAGES.concat('ai').map((name) => `pages/${name}/index`);
  for (const route of JSON.parse(read('app.json')).pages.filter((item) => !aiRoutes.includes(item))) {
    const visible = read(`${route}.wxml`).replace(/<!--[^]*?-->/g, '');
    assert.doesNotMatch(visible, /\bAI\b|内部测试/, route);
  }
});

test('AI 页面（解读、对照、我的记录）：没开放时只显示“暂时没有可用工具”，开放后顶部醒目标注内部测试和 AI 生成；批量工具页开不开都没有 AI 字样', async () => {
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
    for (const name of TOOL_PAGES) {
      const page = load(`pages/${name}/index.js`, dependencies, { wx });
      if (page.onLoad) page.onLoad({ docId: '42', title: '通知' });
      page.onShow(); await flush(); await flush();
      const output = render(read(`pages/${name}/index.wxml`), page.data);
      if (name === 'advisor') {
        // 批量工具页：标题和首页入口同名；这里批量工具关着，AI 开着也只写"暂时没有可用工具"
        assert.equal(titles.at(-1), '批量工具');
        assert.doesNotMatch(output, /AI|内部测试|政策解读|条款对照/);
        assert.match(output, /暂时没有可用工具/);
      } else if (enabled) {
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
  const release = load('utils/advisor.js', { './config.js': { aiInternal: false }, './ai-reasons.js': reasons });
  assert.equal(release.AI_LABEL, 'AI 生成：');
  assert.equal(release.aiTitle('政策解读'), '政策解读');
  assert.equal(release.AI_INTERNAL, false);
  const internal = load('utils/advisor.js', { './config.js': { aiInternal: true }, './ai-reasons.js': reasons });
  assert.equal(internal.AI_LABEL, 'AI 生成 · 内部测试：');
  assert.equal(internal.aiTitle('政策解读'), '政策解读 · 内部测试');
  // 正式版的 AI 页面：顶部是"AI 生成：" + 说明，标题不带"内部测试"
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

test('内部测试标注：后端 disclosure 带说明文字时优先显示后端的，否则用固定文字（我的记录页顶部）', async () => {
  assert.equal(`AI 生成 · ${helpers.INTERNAL_PREFIX}${helpers.internalNotice(undefined)}`, NOTICE);
  for (const value of [null, {}, { modelLabel: '模型', filingNo: '编号' }, { text: '  ' }, '', 3]) {
    assert.equal(`AI 生成 · ${helpers.INTERNAL_PREFIX}${helpers.internalNotice(value)}`, NOTICE);
  }
  assert.equal(helpers.internalNotice('后端说明文字'), '后端说明文字');
  assert.equal(helpers.internalNotice({ text: '后端说明', modelLabel: '模型' }), '后端说明');
  assert.equal(helpers.internalNotice({ text: '内部测试：后端说明' }), '后端说明');
  const api = { advisorCapabilities: () => Promise.resolve(caps(true, { disclosure: { text: '后端给出的 AI 生成说明', modelLabel: '模型甲', filingNo: '编号乙' } })), advisorTasks: () => Promise.resolve({ items: [] }) };
  const page = load('pages/advisor-tasks/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers }, { wx: { setNavigationBarTitle() {} } });
  page.onLoad({}); page.onShow(); await flush(); await flush();
  const output = render(read('pages/advisor-tasks/index.wxml'), page.data);
  assert.match(output, /内部测试：后端给出的 AI 生成说明/); assert.doesNotMatch(output, /摘录通知原文原句，可能不全/);
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
const release = load('utils/advisor.js', { './config.js': { aiInternal: false }, './ai-reasons.js': reasons });


test('对所有人开放（开放状态 open）：没同意协议的人也看得到卡片，点了进解读页（解读页先走同意）；没对所有人开放时只看白名单；后台处理不了时写暂时不可用', async () => {
  const forAll = (available = true) => ({
    advisorMode: () => Promise.resolve({ open: true, available }),
    advisorCapabilities: () => Promise.resolve({ ai_advisor: { enabled: false }, reason: 'consent' }),
  });
  const api = forAll();
  const open = fab(api, { docId: '42', docTitle: '通知', ai: { interpret: true, clause: true, reason: null } }); await flush(); await flush();
  assert.ok(ENTRY_OPEN(open.text())); open.view.open(); assert.equal(open.navigations.length, 1);
  assert.equal(open.navigations[0].url, `/pages/advisor-interpret/index?docId=42&title=${encodeURIComponent('通知')}`);
  // 做不了的文件也显示卡片，写明原因和下一步，不给"开始解读"
  const list = fab(api, { docId: '43', docTitle: '名单', ai: { interpret: false, clause: false, reason: 'list' } }); await flush();
  assert.equal(list.text(), 'AI 解读 名单不需要 AI 解读。要查企业在不在名单上，可以用“查名单”。 去查名单');
  list.view.open(); assert.equal(list.navigations.length, 0);
  list.view.nextStep(); assert.equal(list.navigations[0].url, '/pages/lists/index');
  // 没对所有人开放、这个人也不在白名单：什么都不渲染
  const closed = fab({ advisorMode: () => Promise.resolve({ open: false, available: false }), advisorCapabilities: () => Promise.resolve({ ai_advisor: { enabled: false }, reason: 'off' }) },
    { docId: '42', docTitle: '通知', ai: { interpret: true, clause: true, reason: null } }); await flush(); await flush();
  assert.equal(closed.text(), '');
  const whitelisted = fab({ advisorMode: () => Promise.resolve({ open: false, available: false }), advisorCapabilities: () => Promise.resolve(caps(true)) },
    { docId: '42', docTitle: '通知', ai: { interpret: true, clause: false, reason: null } }); await flush(); await flush();
  assert.ok(ENTRY_OPEN(whitelisted.text())); assert.doesNotMatch(whitelisted.text(), /条款对照/);
  // 详情没有 ai 字段（还不知道能不能做）：不渲染，不把不知道当成能做
  const unknown = fab(api, { docId: '42', docTitle: '通知', ai: null }); await flush();
  assert.equal(unknown.text(), '');
  // 详情还没加载完（blocked）时不显示；加载完重新判断
  const late = fab(api, { blocked: true, docId: '42', docTitle: '通知', ai: null }); await flush();
  assert.equal(late.text(), '');
  Object.assign(late.view.properties, { blocked: false, ai: { interpret: true, clause: false, reason: null } }); late.view.observers.ai.call(late.view); await flush();
  assert.ok(ENTRY_OPEN(late.text()));
  // 后台暂时处理不了（admin 心跳没来、两边设置对不上）：卡片写暂时不可用，不给按钮
  const paused = fab(forAll(false), { docId: '42', docTitle: '通知', ai: { interpret: true, clause: true, reason: null } }); await flush(); await flush();
  assert.equal(paused.text(), 'AI 解读 AI 暂时不可用，请稍后再试。');
  paused.view.open(); assert.equal(paused.navigations.length, 0);
});

test('卡片在做不了的文件上按后台给的原因代码写原因和下一步、不给按钮（D-075 第六节第 3 条）', async () => {
  const api = { advisorCapabilities: () => Promise.resolve(caps(true)) };
  const none = (reason) => ({ interpret: false, clause: false, reason });
  const cases = [
    [{ ai: none('list') }, '名单不需要 AI 解读。要查企业在不在名单上，可以用“查名单”。', '去查名单'],
    [{ ai: none('kind') }, '这类文件不做 AI 解读。', '复制原文链接'],
    [{ ai: none('source') }, '这个来源暂不用于 AI 解读。', '复制原文链接'],
    // 没有完整正文：有附件给"看附件"，没有附件给"复制原文链接"
    [{ ai: none('no_body') }, '还没有拿到完整正文，正文可能在附件里。', '复制原文链接'],
    [{ ai: none('no_body'), hasAttachments: true }, '还没有拿到完整正文，正文可能在附件里。', '看附件'],
    [{ ai: none('too_long') }, '正文超过 2.4 万字，暂不能解读，请先看要点和原文。', ''],
    [{ ai: none('unavailable') }, '这份文件已下架。', '返回'],
    // 旧后台：有 ai、没有原因代码时按页面已有的正文信息判断
    [{ ai: none(null), bodyStatus: 'list' }, '名单不需要 AI 解读。要查企业在不在名单上，可以用“查名单”。', '去查名单'],
    [{ ai: none(null), bodyStatus: 'original' }, '这类文件不做 AI 解读。', '复制原文链接'],
    [{ ai: none(null), bodyStatus: 'ok', bodyTotal: 24001 }, '正文超过 2.4 万字，暂不能解读，请先看要点和原文。', ''],
    [{ ai: none(null), bodyStatus: '' }, '还没有拿到完整正文，正文可能在附件里。', '复制原文链接'],
    [{ ai: none(null), bodyStatus: 'ok', bodyTotal: 800 }, '这份文件暂不能解读，请查看原文。', '复制原文链接'],
  ];
  for (const [props, reason, next] of cases) {
    const card = fab(api, { docId: '42', docTitle: '通知', docUrl: 'https://gov.example/1.html', ...props }); await flush(); await flush();
    assert.equal(card.text(), `AI 解读 ${reason}${next ? ` ${next}` : ''}`, JSON.stringify(props));
    card.view.open(); card.view.openClause(); assert.equal(card.navigations.length, 0);
    card.view.nextStep();
    if (next === '去查名单') assert.equal(card.navigations[0].url, '/pages/lists/index');
    if (next === '复制原文链接') assert.deepEqual(card.copied, ['https://gov.example/1.html']);
    if (next === '看附件') assert.deepEqual(card.events.map(([name]) => name), ['attachments']);
    if (next === '返回') assert.equal(card.back.length, 1);
    if (!next) assert.equal(card.navigations.length + card.copied.length + card.events.length + card.back.length, 0);
  }
  // 能做的文件：正文长短不影响（以后端判断为准）
  const ok = fab(api, { docId: '42', docTitle: '通知', ai: { interpret: true, clause: true, reason: null }, bodyStatus: 'ok', bodyTotal: 30000 }); await flush(); await flush();
  assert.ok(ENTRY_OPEN(ok.text())); assert.match(ok.text(), /也可以做条款对照$/); ok.view.openClause();
  assert.equal(ok.navigations[0].url, `/pages/advisor-clause/index?docId=42&title=${encodeURIComponent('通知')}`);
  // 不在白名单的人：做不了的文件也什么都不渲染
  const outsider = fab({ advisorCapabilities: () => Promise.resolve(caps(false)) }, { docId: '42', ai: none('list') }); await flush(); await flush();
  assert.equal(outsider.text(), '');
});

test('capabilities 用不了的原因：403 没同意协议、503 和网络失败是连不上（重试完仍失败）、404 和关着是 off', async () => {
  const cases = [
    [{ statusCode: 403, data: { detail: '请先阅读并同意用户协议和隐私保护指引' } }, 'consent'],
    [{ statusCode: 403, data: { detail: '用户协议或隐私保护指引已更新，请重新阅读并同意' } }, 'consent', true],
    [{ statusCode: 503 }, 'offline'],
    ['fail', 'offline'],
    [{ statusCode: 404, data: { detail: '没有找到接口' } }, 'off'],
    [{ statusCode: 200, data: caps(false) }, 'off'],
    [{ statusCode: 200, data: { ai_advisor: { enabled: false, reason: 'consent' } } }, 'consent'],
    [{ statusCode: 200, data: caps(true) }, ''],
  ];
  for (const [response, reason, updated = false] of cases) {
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
    assert.equal(result.consentUpdated === true, updated, JSON.stringify(response));
  }
});

test('正式版批量工具页和 AI 页面：没同意协议时引导同意（点按钮走同意流程，回来自动刷新），连不上时给重试；关着时批量工具页、我的记录只写"暂时没有可用工具"，解读页、对照页回首页（D-075：不写 AI、不留"不可用"页）', async () => {
  for (const [reason, text, button] of [['consent', '请先阅读并同意《用户协议》和《隐私保护指引》', '阅读并同意'], ['offline', '暂时连不上服务器', '重试'], ['off', '暂时没有可用工具', '']]) {
    for (const name of TOOL_PAGES) {
      const switched = [];
      let consentCalls = 0; let capabilityCalls = 0;
      const api = { advisorCapabilities: () => { capabilityCalls += 1; return Promise.resolve({ ai_advisor: { enabled: false, features: [], remaining: {} }, reason }); }, advisorTasks: () => Promise.resolve({ items: [] }) };
      const storage = new Map();
      const wx = { setNavigationBarTitle() {}, getStorageSync: (key) => storage.get(key), setStorageSync: (key, value) => storage.set(key, value), switchTab: (o) => switched.push(o.url) };
      const page = load(`pages/${name}/index.js`, { '../../utils/api.js': api, '../../utils/advisor.js': release, '../../utils/tool-context.js': context,
        '../../utils/ai-interpret.js': require('../utils/ai-interpret.js'), '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': require('../utils/format.js'),
        '../../utils/consent.js': { ensureConsent: () => { consentCalls += 1; return Promise.resolve(true); } } }, { wx });
      if (page.onLoad) page.onLoad({ docId: '42', title: '通知' });
      page.onShow(); await flush(); await flush();
      const output = render(read(`pages/${name}/index.wxml`), page.data);
      if (reason === 'off' && ['advisor-interpret', 'advisor-clause'].includes(name)) {
        assert.deepEqual(switched, ['/pages/home/index'], name);
        assert.doesNotMatch(output, /AI|不可用|暂未开放/, name);
        page.onUnload ? page.onUnload() : page.onHide();
        continue;
      }
      assert.deepEqual(switched, [], name);
      if (reason === 'off') assert.doesNotMatch(output, /AI/, name);
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
  for (const name of ['advisor-interpret', 'advisor-clause']) {
    assert.match(read(`pages/${name}/index.wxml`), /bindtap="report">投诉举报</, name);
    assert.match(read(`pages/${name}/index.js`), /\/pages\/complaint\/index/, name);
  }
  // AI 首页底部也有；批量工具页（原工具中心）10/22 起不放 AI，投诉举报跟着 AI 走
  assert.match(read('pages/ai/index.wxml'), /bindtap="report"[^>]*>投诉举报/);
  assert.doesNotMatch(read('pages/advisor/index.wxml'), /投诉举报/);
});
