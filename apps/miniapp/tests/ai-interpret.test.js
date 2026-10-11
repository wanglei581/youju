const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const logic = require('../utils/ai-interpret.js');
const advisor = require('../utils/tool-context.js');
const { render, assertAiGuarded, parse } = require('./helpers/wxml.js');
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const plain = (value) => JSON.parse(JSON.stringify(value));
const helpers = require('../utils/advisor.js');
// 线上 capabilities 的结构（backend/src/youju/api/advisor.py）：数据工具没上线，保持关闭。
const caps = (dataEnabled, aiEnabled) => ({
  data_tools: { enabled: dataEnabled, features: dataEnabled ? ['compare', 'track'] : [], remaining: { compare: 1, track: 1 } },
  ai_advisor: { enabled: aiEnabled, features: aiEnabled ? ['interpret'] : [], remaining: { interpret: 3 }, disclosure: { modelLabel: '模型名称', filingNo: '登记编号' } },
});

function load(file, deps, globals = {}) {
  let definition; const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, Date, Math, Promise, setTimeout, clearTimeout,
    require(name) {
      if (name === '../../utils/consent.js' && !Object.hasOwn(deps, name)) return { loginOpen: () => true };
      if (name === '../../utils/pay.js' && !Object.hasOwn(deps, name)) return require('../utils/pay.js');
      if (name === '../../utils/tool-overlays.js') return { subscribe: () => () => {} };
      if (name === '../../utils/api.js') return { advisorStatus: () => Promise.resolve({ enabled: false }), payProducts: () => Promise.resolve({ items: [] }), isLive: () => true, ...deps[name] };
      assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name];
    },
    Page(value) { definition = value; }, Component(value) { definition = value; }, ...globals,
  }, { filename: file });
  return definition ? { ...definition, data: plain(definition.data), setData(value) { Object.assign(this.data, value); } } : module.exports;
}
function storage(agreed = false) {
  const values = new Map(agreed ? [[logic.CONSENT_KEY, true]] : []);
  return { getStorageSync: (key) => values.get(key), setStorageSync: (key, value) => values.set(key, value), values };
}
function sessionHarness(options = {}) {
  let now = 0; let next = 0; let created = 0; let polled = 0;
  const timers = new Map(); const state = {}; const keys = []; const local = options.storage || storage(true);
  const api = {
    createAdvisorTask(body, key) { created += 1; keys.push(key); assert.deepEqual(body, { kind: 'interpret', doc_id: 42 }); return Promise.resolve({ id: created, status: 'queued' }); },
    advisorTask(id) { polled += 1; return Promise.resolve({ id, status: 'running' }); },
    ...options.api,
  };
  const session = logic.createSession({ api, storage: local, docId: '42', title: '通知标题',
    now: () => now, makeKey: () => `new-key-${keys.length}`,
    setTimer(fn, ms) { const id = ++next; timers.set(id, { fn, at: now + ms }); return id; },
    clearTimer(id) { timers.delete(id); }, update: (value) => Object.assign(state, value), ...options.session });
  async function advance(ms) {
    const end = now + ms;
    while (timers.size) {
      const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
      if (timer.at > end) break;
      now = timer.at; timers.delete(id); timer.fn(); await flush();
    }
    now = end;
  }
  return { session, state, timers, storage: local, advance, keys, created: () => created, polled: () => polled };
}

test('结果分成固定六节，保留原句、段落、missing 和人工核对值', () => {
  const result = logic.resultView({
    items: [{ type: '材料', text: '材料原句', refs: [{ pid: 'P021', quote: '材料原句' }] }, { type: '对象', text: '对象原句', refs: [{ pid: 'P001', quote: '对象原句' }] }],
    missing: ['支持', '条件', '时间', '方式'], verified: [{ type: '时间', value: '2026-11-01', time: '17:00', quote: '截止原句' }],
    notice: '原样提示', warning: '原样警示', attachment_notice: '附件提示', disclosure: { modelLabel: '模型', filingNo: '编号' },
  });
  assert.deepEqual(result.sections.map((section) => section.title), ['申报对象', '支持方式', '申报条件', '申报时间', '申报方式', '材料清单']);
  assert.equal(result.sections[0].items[0].text, '对象原句');
  assert.equal(result.sections[5].items[0].refs[0].pid, 'P021');
  assert.deepEqual(result.sections.map((section) => section.missing), [false, true, true, true, true, false]);
  assert.equal(result.verified[0].displayValue, '2026-11-01 17:00');
  assert.equal(result.verified[0].quote, '截止原句');
  assert.equal(result.notice, '原样提示'); assert.equal(result.warning, '原样警示');
  assert.equal(result.attachmentNotice, '附件提示'); assert.equal(result.disclosure, '模型：模型｜备案号：编号');
});

test('拒答不显示六节；disclosure 两项都非空才显示', () => {
  const result = logic.resultView({ refusal: '请查看原文', items: [{ type: '对象', text: '不显示' }] });
  assert.equal(result.refusal, '请查看原文'); assert.deepEqual(result.sections, []);
  assert.deepEqual(result.verified, []); assert.equal(result.warning, ''); assert.equal(result.attachmentNotice, '');
  for (const disclosure of [undefined, null, {}, { modelLabel: '模型' }, { filingNo: '编号' }, { modelLabel: '', filingNo: '编号' }]) {
    assert.equal(logic.resultView({ disclosure }).disclosure, '');
  }
});

test('须知未勾选不保存、不创建；同意后本机记住且重复点击只建一次', async () => {
  const local = storage(); const harness = sessionHarness({ storage: local });
  harness.session.show(); harness.session.agree(); await flush();
  assert.equal(harness.created(), 0); assert.equal(harness.state.phase, 'consent');
  assert.equal(logic.hasConsent(local), false);
  harness.session.check(true); harness.session.check(false); harness.session.agree(); await flush(); assert.equal(harness.created(), 0);
  harness.session.check(true); harness.session.agree(); harness.session.agree(); await flush();
  assert.equal(harness.created(), 1); assert.equal(logic.hasConsent(local), true); assert.equal(harness.state.needsConsent, false);
  harness.session.hide();
  const second = sessionHarness({ storage: local }); second.session.show(); await flush();
  assert.equal(second.state.needsConsent, false); assert.equal(second.created(), 1); second.session.hide();
});

test('每两秒轮询；隐藏页清除定时器，忽略在途结果，返回只查询已有任务', async () => {
  let resolve;
  const h = sessionHarness({ api: { advisorTask: () => new Promise((done) => { resolve = done; }) } });
  h.session.show(); await flush(); assert.equal(h.state.phase, 'processing');
  await h.advance(1999); assert.equal(resolve, undefined);
  await h.advance(1); assert.equal(typeof resolve, 'function');
  h.session.hide(); assert.equal(h.timers.size, 0);
  resolve({ id: 1, status: 'done', result: { items: [] } }); await flush(); assert.equal(h.state.phase, 'processing');
  h.session.show(); await flush(); assert.equal(h.created(), 1);
  resolve({ id: 1, status: 'done', result: { notice: '提示' } }); await flush();
  assert.equal(h.state.phase, 'done'); assert.equal(h.timers.size, 0); h.session.hide();
});

test('创建过程中隐藏后返回，复用请求且回调不会重复开启轮询', async () => {
  let resolve; let count = 0;
  const h = sessionHarness({ api: { createAdvisorTask: () => { count += 1; return new Promise((done) => { resolve = done; }); } } });
  h.session.show(); await flush(); h.session.hide(); h.session.show(); await flush();
  assert.equal(count, 1); resolve({ id: 9, status: 'queued' }); await flush();
  assert.equal(h.state.taskId, 9); assert.equal(h.timers.size, 2);
  assert.deepEqual(logic.taskContext(h.storage, 9), { docId: '42', title: '通知标题' }); h.session.hide();
});

test('90 秒超时停止请求；查询挂起也能超时，晚到的完成结果不覆盖提示', async () => {
  const h = sessionHarness(); h.session.show(); await flush(); await h.advance(90000);
  assert.equal(h.state.phase, 'timeout'); assert.equal(h.state.error, '还在处理，可稍后在任务里查看');
  assert.equal(h.timers.size, 0); const count = h.polled(); await h.advance(20000); assert.equal(h.polled(), count); h.session.hide();
  let resolve;
  const hung = sessionHarness({ api: { advisorTask: () => new Promise((done) => { resolve = done; }) } });
  hung.session.show(); await flush(); await hung.advance(90000); assert.equal(hung.state.phase, 'timeout');
  resolve({ id: 1, status: 'done', result: {} }); await flush(); assert.equal(hung.state.phase, 'timeout'); hung.session.hide();
});

test('任务失败与结果失效显示后端 error；重试使用新幂等键', async () => {
  for (const status of ['failed', 'done']) {
    const h = sessionHarness({ api: { advisorTask: (id) => Promise.resolve({ id, status, result: null, error: '正文已变化' }) } });
    h.session.show(); await flush(); await h.advance(2000);
    assert.equal(h.state.phase, 'failed'); assert.equal(h.state.error, '正文已变化'); assert.equal(h.state.canRetry, true);
    h.session.retry(); await flush(); assert.equal(h.created(), 2); assert.notEqual(h.keys[0], h.keys[1]); h.session.hide();
  }
});

test('创建接口只返回已完成元数据时，另取任务详情，不误报结果失效', async () => {
  const h = sessionHarness({ api: {
    createAdvisorTask: () => Promise.resolve({ id: 7, status: 'done', hasResult: true }),
    advisorTask: (id) => Promise.resolve({ id, status: 'done', result: { notice: '已有结果' } }),
  } });
  h.session.show(); await flush(); assert.equal(h.state.phase, 'done'); assert.equal(h.state.result.notice, '已有结果');
  assert.equal(h.timers.size, 0); h.session.hide();
});

test('429 保留后端 detail，404 使用固定提示；两者不重试消耗额度', async () => {
  for (const status of [429, 404]) {
    const h = sessionHarness({ api: { createAdvisorTask: () => Promise.reject(Object.assign(new Error('今天额度已用完'), { status })) } });
    h.session.show(); await flush(); assert.equal(h.state.phase, 'failed'); assert.equal(h.state.canRetry, false);
    assert.equal(h.state.error, status === 404 ? '暂时没有可用工具' : '今天额度已用完'); h.session.hide();
  }
});

test('仅 taskId 回看读取本机上下文，不创建任务；缺少上下文也能回看结果', async () => {
  const local = storage(true); logic.taskContext(local, 7, { docId: '42', title: '缓存标题' });
  for (const id of [7, 8]) {
    const h = sessionHarness({ storage: local, session: { docId: '', title: '', taskId: id }, api: { advisorTask: (taskId) => Promise.resolve({ id: taskId, status: 'done', result: {} }) } });
    h.session.show(); await flush(); assert.equal(h.created(), 0); assert.equal(h.state.phase, 'done');
    assert.equal(h.state.docId, id === 7 ? '42' : ''); assert.equal(h.state.title, id === 7 ? '缓存标题' : ''); h.session.hide();
  }
});

test('入口与入口页实际渲染：关闭无 AI、开放才显示，数据工具开关不影响', async () => {
  for (const [dataEnabled, aiEnabled] of [[false, false], [true, false], [false, true], [true, true]]) {
    let navigationTitle; const capabilities = caps(dataEnabled, aiEnabled);
    const api = { advisorCapabilities: () => Promise.resolve(capabilities), advisorTasks: () => Promise.resolve({ items: [{ id: 1, kind: 'interpret', status: 'done', docTitle: '通知' }, { id: 2, kind: 'compare', status: 'done' }] }) };
    const fab = load('components/tool-fab/index.js', { '../../utils/api.js': api, '../../utils/tool-context.js': advisor });
    Object.assign(fab, fab.methods); fab.alive = true; fab.setData({ visible: true }); fab.load(); await flush();
    const text = render(read('components/tool-fab/index.wxml'), { ...fab.data, bottomRpx: 0, blocked: false });
    assert.equal(text, aiEnabled ? 'AI 内测' : '');
    const page = load('pages/advisor/index.js', { '../../utils/api.js': api, '../../utils/tool-context.js': advisor, '../../utils/advisor.js': helpers },
      { wx: { setNavigationBarTitle: ({ title }) => { navigationTitle = title; } } });
    page.onLoad({ docId: '42', title: '通知' }); page.onShow(); await flush();
    const rendered = render(read('pages/advisor/index.wxml'), page.data);
    if (aiEnabled) {
      assert.match(rendered, /内部测试：结果由 AI 模型根据原文生成/); assert.match(rendered, /今天剩余 3 次/); assert.match(rendered, /解读这篇通知/);
      assert.match(rendered, /模型：模型名称｜备案号：登记编号/);
      assert.deepEqual(page.data.recent.map((task) => task.kind), ['interpret']);
    } else {
      assert.doesNotMatch(rendered, /AI|政策解读|模型：|内部测试/); assert.equal(page.data.recent.length, 0);
    }
    assert.match(rendered, /数据工具/); assert.doesNotMatch(rendered, /名单比对|填写客户/);
    assert.equal(navigationTitle, undefined);
    if (aiEnabled) { page.clearContext(); const noContext = render(read('pages/advisor/index.wxml'), page.data); assert.match(noContext, /在通知详情页点右下角“AI 内测”进入/); }
    page.onHide();
  }
  for (const file of ['components/tool-fab/index.wxml', 'pages/advisor/index.wxml']) assertAiGuarded(read(file));
});

test('解读页直达仍须 capabilities 开放，拒绝权限时不显示须知也不创建任务', async () => {
  for (const enabled of [false, true, 'true']) {
    const local = storage(); let created = 0; let navigationTitle;
    const page = load('pages/advisor-interpret/index.js', {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(caps(false, enabled)), createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 1, status: 'failed', error: '错误' }); } },
      '../../utils/ai-interpret.js': logic, '../../utils/tool-context.js': advisor, '../../utils/advisor.js': helpers,
    }, { wx: { ...local, setNavigationBarTitle: ({ title }) => { navigationTitle = title; } } });
    page.onLoad({ docId: '42', title: '通知' }); page.onShow(); await flush(); page.agree(); await flush(); assert.equal(created, 0);
    const output = render(read('pages/advisor-interpret/index.wxml'), page.data);
    assert.equal(output.includes('使用须知'), enabled === true);
    assert.equal(output.includes('AI'), enabled === true);
    assert.equal(output.includes('内部测试：结果由 AI 模型根据原文生成，每条都附原文引用，请以原文为准，不作为能否申报的判断。'), enabled === true);
    assert.equal(navigationTitle, enabled === true ? '政策解读 · 内部测试' : '工具');
    page.check({ detail: { value: ['agree'] } }); page.agree(); await flush(); assert.equal(created, enabled === true ? 1 : 0); page.onUnload();
  }
});

test('解读页隐藏或卸载时忽略在途 capabilities，不开启会话', async () => {
  for (const lifecycle of ['onHide', 'onUnload']) {
    let resolve; let shown = 0; let stopped = 0;
    const page = load('pages/advisor-interpret/index.js', {
      '../../utils/api.js': { advisorCapabilities: () => new Promise((done) => { resolve = done; }) },
      '../../utils/ai-interpret.js': { createSession: () => ({ show() { shown += 1; }, hide() { stopped += 1; } }) },
      '../../utils/tool-context.js': advisor, '../../utils/advisor.js': helpers,
    }, { wx: { setNavigationBarTitle() {} } });
    page.onLoad({ docId: '42', title: '通知' }); page.onShow(); page[lifecycle]();
    resolve(caps(false, true)); await flush(); assert.equal(shown, 0); assert.equal(stopped, 1); assert.equal(page.data.aiEnabled, false);
  }
});

test('解读页渲染结果原句、核对值、missing、警示、附件提示和拒答；跳转带通知编号', async () => {
  const urls = []; const local = storage(true);
  const page = load('pages/advisor-interpret/index.js', {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(caps(false, true)), advisorTask: () => Promise.resolve({ id: 7, status: 'done', result: {} }) },
    '../../utils/ai-interpret.js': logic, '../../utils/tool-context.js': advisor, '../../utils/advisor.js': helpers,
  }, { wx: { ...local, setNavigationBarTitle() {}, navigateTo: ({ url }) => urls.push(url) } });
  page.onLoad({ docId: '42', title: encodeURIComponent('通知 & 标题'), taskId: 7 }); page.onShow(); await flush();
  page.setData({ result: logic.resultView({ notice: '固定原文提示', items: [{ type: '对象', text: '很长的原句，完整显示。', refs: [{ pid: 'P002', quote: '很长的原句，完整显示。' }] }], missing: ['材料'], verified: [{ value: '人工核对值', quote: '核对原句' }], warning: '警示原文', attachment_notice: '附件原文' }) });
  const output = render(read('pages/advisor-interpret/index.wxml'), page.data);
  for (const expected of ['内部测试：结果由 AI 模型根据原文生成', '固定原文提示', '通知 & 标题', '人工核对值', '核对原句', '很长的原句，完整显示。', '段落 P002', '未摘录到相关原句，请查看原文', '警示原文', '附件原文', '第三方整理 · 非政府平台']) assert.ok(output.includes(expected), expected);
  assert.doesNotMatch(output, /模型：/);
  page.original(); assert.equal(urls.at(-1), '/pages/detail/index?id=42');
  page.feedback(); assert.equal(urls.at(-1), '/pages/feedback/index?id=42&title=' + encodeURIComponent('通知 & 标题'));
  page.setData({ result: logic.resultView({ refusal: '拒答原文' }) });
  const refusal = render(read('pages/advisor-interpret/index.wxml'), page.data);
  assert.match(refusal, /拒答原文.*看原文/); assert.doesNotMatch(refusal, /申报对象|材料清单/); page.onUnload();
});

test('capabilities 缓存与 refresh 并发合并，失败静默且 interpret 请求只有 doc_id', async () => {
  const requests = [];
  const api = load('utils/api.js', { './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, '../mock/advisor.js': {}, './mockSearch.js': {}, './format.js': {} },
    { wx: { request: (options) => requests.push(options) } });
  const first = api.advisorCapabilities(); assert.equal(first, api.advisorCapabilities({ refresh: true }));
  assert.equal(requests[0].url, 'https://test.invalid/api/v1/advisor/capabilities');
  requests[0].success({ statusCode: 200, data: caps(true, true) }); assert.equal((await first).ai_advisor.enabled, true);
  assert.equal(first, api.advisorCapabilities());
  const refreshed = api.advisorCapabilities({ refresh: true }); assert.notEqual(first, refreshed);
  requests[1].fail(); const failed = await refreshed;
  assert.equal(failed.ai_advisor.enabled, false); assert.equal(refreshed, api.advisorCapabilities());
  const task = api.createAdvisorTask({ kind: 'interpret', doc_id: '42', rows: ['不发送'] }, 'unique-key');
  assert.deepEqual(plain(requests[2].data), { kind: 'interpret', doc_id: 42 }); assert.equal(requests[2].header['Idempotency-Key'], 'unique-key');
  assert.equal(requests[2].method, 'POST'); assert.equal(requests[2].url, 'https://test.invalid/api/v1/advisor/tasks');
  requests[2].success({ statusCode: 202, data: { id: 7 } }); assert.equal((await task).id, 7);
  // 数据工具仍保留原来的请求结构与幂等头。
  const compare = api.createAdvisorTask({ kind: 'compare', rows: ['甲'] }, 'k');
  assert.deepEqual(plain(requests[3].data), { kind: 'compare', rows: ['甲'] });
  requests[3].success({ statusCode: 201, data: { id: 8 } }); assert.equal((await compare).id, 8);
  assert.equal(requests.length, 4);
});

test('任务页保留数据任务并只列有权限的解读任务；类型名与入口正确', async () => {
  for (const enabled of [false, true]) {
    const urls = []; const tasks = [{ id: 7, kind: 'interpret', status: 'done', hasResult: true, rowCount: 0, docTitle: '通知标题' }, { id: 8, kind: 'compare', status: 'done', rowCount: 2, hasFile: true }];
    let listed = 0;
    const api = { advisorCapabilities: () => Promise.resolve(caps(true, enabled)), advisorTasks: () => { listed += 1; return Promise.resolve({ items: tasks }); } };
    const page = load('pages/advisor-tasks/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers }, { wx: { navigateTo: ({ url }) => urls.push(url), setNavigationBarTitle() {} } });
    page.onShow(); await flush();
    const output = render(read('pages/advisor-tasks/index.wxml'), page.data);
    assert.equal(output.includes('政策解读'), enabled); assert.equal(output.includes('查看解读'), enabled);
    assert.equal(output.includes('通知标题'), enabled); assert.match(output, /批量名单比对/); assert.match(output, /打开 Excel/);
    assert.equal(listed, 1);
    if (!enabled) { assert.doesNotMatch(output, /AI|内部测试/); assert.match(output, /批量名单比对/); }
    page.openTask({ currentTarget: { dataset: { id: 7 } } }); assert.equal(urls.length, enabled ? 1 : 0);
    page.openTask({ currentTarget: { dataset: { id: 8 } } }); assert.equal(urls.length, enabled ? 1 : 0);
    if (enabled) assert.equal(urls[0], '/pages/advisor-interpret/index?taskId=7'); page.onHide();
  }
});

test('关闭权限后任务列表请求失败，也不显示此前的解读任务', async () => {
  const page = load('pages/advisor-tasks/index.js', {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(caps(true, false)), advisorTasks: () => Promise.reject(new Error('网络错误')) },
    '../../utils/advisor.js': helpers,
  }, { wx: { setNavigationBarTitle() {} } });
  page.setData({ aiEnabled: true, kinds: ['interpret'], items: [{ id: 7, kind: 'interpret', kindLabel: '政策解读' }] });
  page.onShow(); await flush();
  assert.deepEqual(plain(page.data.items), []); assert.doesNotMatch(render(read('pages/advisor-tasks/index.wxml'), page.data), /政策解读|AI/);
  page.onHide();
});

test('新增页已注册、模板闭合，原句样式没有截断或新颜色', () => {
  for (const name of ['advisor', 'advisor-interpret', 'advisor-clause', 'advisor-tasks']) {
    assert.ok(JSON.parse(read('app.json')).pages.includes(`pages/${name}/index`), name);
    parse(read(`pages/${name}/index.wxml`));
  }
  assert.doesNotMatch(read('pages/advisor-interpret/index.wxss'), /#[0-9a-fA-F]|gradient|text-overflow|line-clamp/);
  assert.doesNotMatch(read('pages/advisor-interpret/index.wxml'), /智能|保证|权威|符合|不符合/);
});
