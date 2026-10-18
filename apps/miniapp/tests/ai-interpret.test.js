const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
require('./helpers/internal-build.js'); // 本文件按 AI 内测包测，见该文件说明
const logic = require('../utils/ai-interpret.js');
const advisor = require('../utils/tool-context.js');
const { render, assertAiGuarded, parse } = require('./helpers/wxml.js');
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const plain = (value) => JSON.parse(JSON.stringify(value));
const helpers = require('../utils/advisor.js');
// capabilities 的两类能力独立授权。
const caps = (dataEnabled, aiEnabled) => ({
  data_tools: { enabled: dataEnabled, features: dataEnabled ? ['compare', 'track'] : [], remaining: { compare: 1, track: 1 } },
  ai_advisor: { enabled: aiEnabled, features: aiEnabled ? ['interpret'] : [], remaining: { interpret: 3 }, disclosure: { modelLabel: '模型名称', filingNo: '登记编号' } },
});

function load(file, deps, globals = {}) {
  let definition; const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, Date, Math, Promise, setTimeout, clearTimeout,
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
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

test('结果按六类顺序排，没摘到的类别合成一行；保留原句、引用编号和人工核对值（10/17 精简）', () => {
  const result = logic.resultView({
    items: [{ type: '材料', text: '材料原句', refs: [{ pid: 'P021', quote: '材料原句' }] }, { type: '对象', text: '对象原句', refs: [{ pid: 'P001', quote: '对象原句' }] }],
    missing: ['支持', '条件', '时间', '方式'], verified: [{ type: '时间', value: '2026-11-01', time: '17:00', quote: '截止原句' }],
    notice: '原样提示', warning: '原样警示', attachment_notice: '附件提示', disclosure: { modelLabel: '模型', filingNo: '编号' },
  });
  assert.deepEqual(result.sections.map((section) => section.title), ['申报对象', '材料清单']);
  assert.equal(result.missingText, '支持方式、申报条件、申报时间、申报方式');
  assert.equal(result.sections[0].items[0].text, '对象原句');
  assert.equal(result.sections[1].items[0].refs[0].pid, 'P021');
  assert.equal(result.verified[0].displayValue, '2026-11-01 17:00');
  assert.equal(result.verified[0].quote, '截止原句');
  assert.equal(result.notice, '原样提示'); assert.equal(result.warning, '原样警示');
  assert.equal(result.attachmentNotice, '附件提示'); assert.equal(result.disclosure, '模型：模型｜备案号：编号');
  // 后端没标 missing、但这一类一条都没有：也算没摘到
  assert.equal(logic.resultView({ items: [{ type: '对象', text: '对象原句' }] }).missingText, '支持方式、申报条件、申报时间、申报方式、材料清单');
});

test('同一句原文只出现一次：首次出现处标"也属于"，只剩重复句的类别写和哪一类相同（10/17）', () => {
  const same = '请各市组织企业通过平台申报';
  const result = logic.resultView({ items: [
    { type: '时间', text: same, refs: [{ pid: 'P005', quote: same }] },
    { type: '方式', text: same, refs: [{ pid: 'P005', quote: same }] },
    { type: '材料', text: `${same}\n`, refs: [] }, { type: '材料', text: '提交申请书', refs: [] },
    { type: '材料', text: '提交申请书', refs: [] },
  ] });
  assert.deepEqual(result.sections.map((section) => section.title), ['申报时间', '申报方式', '材料清单']);
  const [time, way, material] = result.sections;
  assert.deepEqual(time.items.map((item) => [item.text, item.alsoInText]), [[same, '申报方式、材料清单']]);
  assert.deepEqual(way.items, []); assert.equal(way.sameAs, '申报时间');
  assert.deepEqual(material.items.map((item) => item.text), ['提交申请书']); assert.equal(material.sameAs, '申报时间');
  assert.equal(result.missingText, '申报对象、支持方式、申报条件');
  // 重复句带的不同引用并到第一次出现的那条里，同样的引用只留一条
  const merged = logic.resultView({ items: [
    { type: '对象', text: same, refs: [{ pid: 'P001', quote: '甲' }] },
    { type: '对象', text: same, refs: [{ pid: 'P001', quote: '甲' }, { pid: 'P002', quote: '乙' }] },
    { type: '条件', text: same, refs: [{ pid: 'P003', quote: '丙' }] },
  ], missing: ['条件'] });
  assert.deepEqual(merged.sections[0].items[0].refs.map((ref) => [ref.key, ref.pid, ref.quote]), [[0, 'P001', '甲'], [1, 'P002', '乙'], [2, 'P003', '丙']]);
  // 后端把"条件"列为没摘到、但实际有和"对象"同一句：以条目为准，不再出现在"没找到"里
  assert.deepEqual(merged.sections.map((section) => [section.title, section.sameAs]), [['申报对象', ''], ['申报条件', '申报对象']]);
  assert.doesNotMatch(merged.missingText, /申报条件/);
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

test('使用须知一个按钮：start 等于确认并开始，本机记住，重复点击只建一次（10/17）', async () => {
  const local = storage(); const harness = sessionHarness({ storage: local });
  harness.session.show(); await flush(); assert.equal(harness.state.phase, 'consent'); assert.equal(harness.created(), 0);
  harness.session.start(); harness.session.start(); await flush();
  assert.equal(harness.created(), 1); assert.equal(logic.hasConsent(local), true); assert.equal(harness.state.needsConsent, false);
  harness.session.hide();
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

test('90 秒还没好：改成每 15 秒查一次，完成后自动显示；挂起的旧查询晚到不写页面；15 分钟后停，回到页面接着查', async () => {
  let status = 'running'; let polls = 0;
  const h = sessionHarness({ api: { advisorTask: (id) => {
    polls += 1; return Promise.resolve(status === 'done' ? { id, status, result: { notice: '晚到的结果' } } : { id, status: 'running' });
  } } });
  h.session.show(); await flush(); await h.advance(90000);
  assert.equal(h.state.phase, 'timeout'); assert.match(h.state.error, /可以先离开/);
  const count = polls; assert.ok(count > 40); await h.advance(14999); assert.equal(polls, count);
  await h.advance(1); assert.equal(polls, count + 1); assert.equal(h.state.phase, 'timeout');
  status = 'done'; await h.advance(15000);
  assert.equal(h.state.phase, 'done'); assert.equal(h.state.result.notice, '晚到的结果'); assert.equal(h.timers.size, 0);
  h.session.hide();

  let resolve; let calls = 0;
  const hung = sessionHarness({ api: { advisorTask: (id) => {
    calls += 1; return calls === 1 ? new Promise((done) => { resolve = done; }) : Promise.resolve({ id, status: 'running' });
  } } });
  hung.session.show(); await flush(); await hung.advance(90000); assert.equal(hung.state.phase, 'timeout');
  resolve({ id: 1, status: 'done', result: { notice: '旧查询' } }); await flush(); assert.equal(hung.state.phase, 'timeout');
  hung.session.hide();

  const idle = sessionHarness(); idle.session.show(); await flush(); await idle.advance(15 * 60000 + 15000);
  assert.equal(idle.timers.size, 0); assert.match(idle.state.error, /请稍后在“我的 → 我的记录”里查看结果/);
  const before = idle.polled(); idle.session.hide(); idle.session.show(); await flush(); await idle.advance(2000);
  assert.ok(idle.polled() > before); assert.equal(idle.created(), 1); idle.session.hide();
});

test('排队时显示前面还有几个、预计多久；没给就只写排队中', () => {
  assert.equal(logic.progressText({ status: 'queued', queuePosition: 3, etaSeconds: 150 }), '排队中：前面还有 3 个，预计约 3 分钟');
  assert.equal(logic.progressText({ status: 'queued', queuePosition: 2 }), '排队中：前面还有 2 个');
  assert.equal(logic.progressText({ status: 'queued', queuePosition: 0, etaSeconds: 20 }), '排队中，马上开始');
  assert.equal(logic.progressText({ status: 'queued' }), '排队中，马上开始');
  assert.equal(logic.progressText({ status: 'running' }), '正在对照原文摘录…');
});

test('没连上和任务失败分开：创建没连上重试用同一个请求编号，查询没连上重试接着查同一个任务', async () => {
  let fails = 1;
  const h = sessionHarness({ api: { createAdvisorTask(body, key) {
    h.keys.push(key);
    if (fails > 0) { fails -= 1; return Promise.reject(Object.assign(new Error('网络不好'), { retryable: true })); }
    return Promise.resolve({ id: 5, status: 'queued' });
  } } });
  h.session.show(); await flush(); assert.equal(h.state.phase, 'failed'); assert.equal(h.state.canRetry, true);
  assert.equal(h.session.resumable(), true);
  h.session.retry(); await flush(); assert.equal(h.keys.length, 2); assert.equal(h.keys[0], h.keys[1]);
  assert.equal(h.state.taskId, 5); h.session.hide();

  let pollFails = 1;
  const q = sessionHarness({ api: { advisorTask: (id) => {
    if (pollFails > 0) { pollFails -= 1; return Promise.reject(Object.assign(new Error('服务器忙'), { status: 503 })); }
    return Promise.resolve({ id, status: 'running' });
  } } });
  q.session.show(); await flush(); await q.advance(2000); assert.equal(q.state.phase, 'failed');
  q.session.retry(); await flush(); assert.equal(q.created(), 1); assert.equal(q.state.phase, 'processing'); q.session.hide();
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
    const api = { advisorStatus: () => Promise.resolve({ enabled: true, quota: { compare: { limit: 3, used: 2, maxRows: 50 }, track: { limit: 3, used: 2, maxRows: 50 } } }), advisorCapabilities: () => Promise.resolve(capabilities), advisorTasks: () => Promise.resolve({ items: [{ id: 1, kind: 'interpret', status: 'done', docTitle: '通知' }, { id: 2, kind: 'compare', status: 'done' }] }) };
    const fab = load('components/ai-entry/index.js', { '../../utils/api.js': api, '../../utils/tool-context.js': advisor, '../../utils/advisor.js': helpers });
    fab.properties = Object.fromEntries(Object.entries(fab.properties).map(([key, value]) => [key, value.value]));
    Object.assign(fab, fab.methods); fab.alive = true; fab.load(); await flush();
    const text = render(read('components/ai-entry/index.wxml'), { ...fab.data, blocked: false });
    assert.equal(text.startsWith('AI 解读') && text.includes('开始解读'), aiEnabled); if (!aiEnabled) assert.equal(text, '');
    const page = load('pages/advisor/index.js', { '../../utils/api.js': api, '../../utils/tool-context.js': advisor, '../../utils/advisor.js': helpers, '../../utils/boost.js': require('../utils/boost.js') },
      { wx: { setNavigationBarTitle: ({ title }) => { navigationTitle = title; } } });
    page.onLoad({ docId: '42', title: '通知' }); page.onShow(); await flush();
    const rendered = render(read('pages/advisor/index.wxml'), page.data);
    if (aiEnabled) {
      assert.match(rendered, /内部测试：摘录通知原文原句/); assert.match(rendered, /今天还能用 3 次（失败、拒答不扣次数）/); assert.match(rendered, /开始解读/);
      assert.match(rendered, /模型：模型名称｜备案号：登记编号/);
      assert.deepEqual(page.data.recent.map((task) => task.kind).sort(), dataEnabled ? ['compare', 'interpret'] : ['interpret']);
    } else {
      assert.doesNotMatch(rendered, /AI|政策解读|模型：|内部测试/); assert.equal(page.data.recent.length, dataEnabled ? 1 : 0);
    }
    assert.equal(rendered.includes('批量名单比对'), dataEnabled);
    assert.equal(navigationTitle, aiEnabled && !dataEnabled ? 'AI 工具 · 内部测试' : '工具中心');
    if (aiEnabled) { page.clearContext(); const noContext = render(read('pages/advisor/index.wxml'), page.data); assert.match(noContext, /打开一份申报通知，在“要点”下面的“AI 解读”卡片里点“开始解读”/); assert.match(noContext, /去找通知/); }
    page.onHide();
  }
  for (const file of ['components/ai-entry/index.wxml', 'pages/advisor/index.wxml']) assertAiGuarded(read(file));
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
    assert.equal(output.includes('内部测试：摘录通知原文原句，可能不全；不判断能否申报，以原文和主管部门解释为准。'), enabled === true);
    assert.equal(navigationTitle, enabled === true ? 'AI 解读 · 内部测试' : '工具');
    page.check({ detail: { value: ['agree'] } }); page.agree(); await flush(); assert.equal(created, enabled === true ? 1 : 0); page.onUnload();
  }
});

test('换了手机打开已有解读：须知按钮写"查看结果"；从通知进来写"开始解读"', async () => {
  for (const taskId of ['', '7']) {
    const local = storage();
    const page = load('pages/advisor-interpret/index.js', {
      '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(caps(false, true)), advisorTask: (id) => Promise.resolve({ id, status: 'done', result: {} }) },
      '../../utils/ai-interpret.js': logic, '../../utils/tool-context.js': advisor, '../../utils/advisor.js': helpers,
    }, { wx: { ...local, setNavigationBarTitle() {} } });
    page.onLoad(taskId ? { taskId } : { docId: '42', title: '通知' }); page.onShow(); await flush();
    const output = render(read('pages/advisor-interpret/index.wxml'), page.data);
    assert.match(output, /使用须知/); assert.match(output, taskId ? /我知道了，查看结果/ : /我知道了，开始解读/);
    assert.doesNotMatch(output, taskId ? /开始解读/ : /查看结果/); page.onUnload();
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
  for (const expected of ['固定原文提示', '通知 & 标题', '人工核对值', '核对原句', '很长的原句，完整显示。', '正文里没找到：支持方式、申报条件、申报时间、申报方式、材料清单。可能写在附件里，也可能原文没写，请查看原文。', '这句摘错了', '看原文', '警示原文', '附件原文', '第三方整理 · 非政府平台']) assert.ok(output.includes(expected), expected);
  assert.doesNotMatch(output, /模型：/);
  // 10/17：免责只说一次（出结果后只留吸顶的"AI 生成"提示）；不显示段落编号；附件提醒放在分类前面
  assert.doesNotMatch(output, /内部测试：摘录通知原文原句/); assert.doesNotMatch(output, /段落 P0/);
  assert.ok(output.indexOf('附件原文') < output.indexOf('申报对象'));
  // 解读结果下面可选"和我的企业对一下"：没开条款对照的不显示、点了也不跳；开了才带通知编号进条款对照
  assert.doesNotMatch(output, /和我的企业对一下/); const before = urls.length; page.clause(); assert.equal(urls.length, before);
  page.setData({ clauseEnabled: true });
  assert.match(render(read('pages/advisor-interpret/index.wxml'), page.data), /和我的企业对一下（可选）/); page.clause();
  assert.equal(urls.at(-1), `/pages/advisor-clause/index?docId=42&title=${encodeURIComponent('通知 & 标题')}`);
  page.original(); assert.equal(urls.at(-1), '/pages/detail/index?id=42');
  page.feedback(); assert.equal(urls.at(-1), '/pages/feedback/index?id=42&title=' + encodeURIComponent('通知 & 标题'));
  page.setData({ result: logic.resultView({ refusal: '拒答原文' }) });
  const refusal = render(read('pages/advisor-interpret/index.wxml'), page.data);
  assert.match(refusal, /拒答原文.*看原文/); assert.doesNotMatch(refusal, /申报对象|材料清单/); page.onUnload();
});

test('capabilities 缓存与 refresh 并发合并，失败静默且 interpret 请求只有 doc_id', async () => {
  const requests = [];
  const api = load('utils/api.js', { './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {} },
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
  // 数据工具走同一接口，但不发送 AI 字段；未知类型仍拒绝。
  const compare = api.createAdvisorTask({ kind: 'compare', rows: ['甲'], doc_id: 42 }, 'k');
  assert.deepEqual(plain(requests[3].data), { kind: 'compare', rows: ['甲'] });
  requests[3].success({ statusCode: 202, data: { id: 8 } }); assert.equal((await compare).id, 8);
  await assert.rejects(api.createAdvisorTask({ kind: 'unknown' }, 'k'), (err) => err.status === 404);
  assert.equal(requests.length, 4);
});

test('共同任务页按各类权限显示，数据任务不能扩大 AI 访问权', async () => {
  for (const enabled of [false, true]) {
    const urls = []; const tasks = [{ id: 7, kind: 'interpret', status: 'done', hasResult: true, rowCount: 0, docTitle: '通知标题' }, { id: 8, kind: 'compare', status: 'done', rowCount: 2, hasFile: true }];
    let listed = 0;
    const api = { advisorCapabilities: () => Promise.resolve(caps(true, enabled)), advisorTasks: () => { listed += 1; return Promise.resolve({ items: tasks }); } };
    const page = load('pages/advisor-tasks/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers }, { wx: { navigateTo: ({ url }) => urls.push(url), setNavigationBarTitle() {} } });
    page.onShow(); await flush();
    const output = render(read('pages/advisor-tasks/index.wxml'), page.data);
    assert.equal(output.includes('AI 解读'), enabled); assert.equal(output.includes('查看解读'), enabled);
    assert.equal(output.includes('通知标题'), enabled); assert.match(output, /批量名单比对.*下载 Excel/);
    assert.equal(listed, 1);
    if (!enabled) assert.doesNotMatch(output, /AI|内部测试/);
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
  page.setData({ aiEnabled: true, kinds: ['interpret'], items: [{ id: 7, kind: 'interpret', kindLabel: 'AI 解读' }] });
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

test('原文片段跨了 PDF 版面换行时，显示前去掉换行；英文单词之间留一个空格（10/13）', () => {
  assert.equal(logic.oneLine('近三年未\n发生重大安全事故。'), '近三年未发生重大安全事故。');
  assert.equal(logic.oneLine('求索对话 DeepSeek\nChat 备案'), '求索对话 DeepSeek Chat 备案');
  assert.equal(logic.oneLine(null), '');
  const quote = '具有独立法人资格的企事业单位，近三年未\n发生重大安全、环保、质量事故。';
  const view = logic.resultView({ items: [{ type: '条件', text: quote, refs: [{ pid: 'P002', quote }] }] });
  const row = view.sections.find((s) => s.type === '条件').items[0];
  assert.equal(row.text, '具有独立法人资格的企事业单位，近三年未发生重大安全、环保、质量事故。');
  assert.equal(row.refs[0].quote, row.text);
});
