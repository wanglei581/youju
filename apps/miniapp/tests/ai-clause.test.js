const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const clause = require('../utils/ai-clause.js');
const interpret = require('../utils/ai-interpret.js');
const context = require('../utils/tool-context.js');
const format = require('../utils/format.js');
const advisor = require('../utils/advisor.js');
const { render, parse } = require('./helpers/wxml.js');
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const today = '2026-10-04';
const caps = (enabled = true, features = ['interpret', 'clause']) => ({
  data_tools: { enabled: true, features: [], remaining: {} },
  ai_advisor: { enabled, features, remaining: { interpret: 3, clause: 2 } },
});
function storage(agreed = true) {
  const saved = new Map(agreed ? [[interpret.CONSENT_KEY, true]] : []);
  return { saved, getStorageSync: (key) => saved.get(key), setStorageSync: (key, value) => saved.set(key, value) };
}
function load(file, api, wx = {}, extra = {}) {
  let definition; const module = { exports: {} };
  const dependencies = { '../../utils/api.js': api, '../../utils/ai-clause.js': clause,
    '../../utils/tool-context.js': context, '../../utils/format.js': format, '../../utils/advisor.js': advisor,
    '../../utils/conditions.js': {}, '../../utils/consent.js': {}, ...extra };
  vm.runInNewContext(read(file), { module, Date, Math, Promise, setTimeout, clearTimeout,
    require: (key) => { assert.ok(Object.hasOwn(dependencies, key), key); return dependencies[key]; },
    Page: (value) => { definition = value; }, wx: { setNavigationBarTitle() {}, ...wx },
  }, { filename: file });
  return definition ? { ...definition, data: plain(definition.data), setData(value) { Object.assign(this.data, plain(value)); } } : module.exports;
}
function harness(options = {}) {
  let now = 0; let next = 0; const timers = new Map(); const state = {}; const calls = [];
  const local = options.storage || storage();
  const api = { createAdvisorTask: (body, key) => { calls.push({ body, key }); return Promise.resolve({ id: 7, docId: 42, docTitle: '通知', status: 'queued' }); },
    advisorTask: (id) => Promise.resolve({ id, docId: 42, docTitle: '通知', status: 'running' }), ...options.api };
  const session = clause.createSession({ api, storage: local, docId: 42, company: {}, now: () => now,
    update: (value) => Object.assign(state, value), setTimer(fn, ms) { const id = ++next; timers.set(id, { fn, at: now + ms }); return id; },
    clearTimer: (id) => timers.delete(id), ...options.session });
  async function advance(ms) {
    const end = now + ms;
    while (timers.size) {
      const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
      if (timer.at > end) break;
      now = timer.at; timers.delete(id); timer.fn(); await flush();
    }
    now = end;
  }
  return { session, state, timers, calls, local, advance };
}

test('全空可提交；日期须真实且成立日期不晚于今天，资质有效期可在未来', () => {
  assert.deepEqual(clause.validateForm({}, today), {});
  assert.deepEqual(clause.toCompany(clause.emptyForm(), today), {});
  for (const established of ['2026-10-05', '2026-02-29', '2026-04-31', '0000-01-01', '2026-1-01']) {
    assert.ok(clause.validateForm({ established }, today).established, established);
  }
  for (const established of [today, '2024-02-29', '2000-01-01']) assert.deepEqual(clause.validateForm({ established }, today), {});
  assert.deepEqual(clause.validateForm({ qualification: [{ name: clause.QUALIFICATIONS[0], valid_until: '2030-01-01' }] }, today), {});
});

test('金额至多两位小数且与年份一起填写，年份 2000 到今年；人数整数并遵守后端上限', () => {
  for (const field of ['revenue', 'rd']) {
    for (const amount of ['-1', '1.001', 'Infinity', 'NaN', '1000000001', '1e3', '']) {
      assert.ok(clause.validateForm({ [`${field}Amount`]: amount, [`${field}Year`]: '2026' }, today)[`${field}Amount`]);
    }
    for (const year of ['1999', '2027', '2026.1', '26', '']) {
      assert.ok(clause.validateForm({ [`${field}Amount`]: '1.23', [`${field}Year`]: year }, today)[`${field}Year`]);
    }
    for (const amount of ['0', '0.01', '12.30', '1000000000']) for (const year of ['2000', '2026']) {
      assert.deepEqual(clause.validateForm({ [`${field}Amount`]: amount, [`${field}Year`]: year }, today), {});
    }
  }
  for (const employees of ['-1', '1.5', '1e3', '1000001', 'Infinity']) assert.ok(clause.validateForm({ employees }, today).employees);
  for (const employees of ['0', '1', '1000000']) assert.deepEqual(clause.validateForm({ employees }, today), {});
});

test('注册地复用搜索省份清单，资质限制名称、日期和六项上限', () => {
  assert.match(read('pages/search/index.js'), /require\('\.\.\/\.\.\/utils\/provinces.js'\)/);
  assert.equal(clause.PROVINCES, require('../utils/provinces.js'));
  assert.equal(clause.PROVINCES.length, 31); assert.ok(!clause.PROVINCES.includes('国家'));
  assert.ok(clause.validateForm({ region: '北京市' }, today).region);
  const qualification = Array.from({ length: 6 }, () => ({ name: clause.QUALIFICATIONS[0], valid_until: null }));
  assert.deepEqual(clause.validateForm({ region: '北京', qualification }, today), {});
  assert.ok(clause.validateForm({ qualification: [...qualification, qualification[0]] }, today).qualification);
  assert.ok(clause.validateForm({ qualification: [{ name: '其他资质' }] }, today).qualification);
  assert.ok(clause.validateForm({ qualification: [{ name: clause.QUALIFICATIONS[0], valid_until: '2026-02-30' }] }, today).qualification0);
});

test('company 只包含结构化字段，金额年份人数为数字，有效期空值为 null', () => {
  assert.deepEqual(clause.toCompany({ established: '2020-01-02', region: '辽宁', revenueAmount: '123.45', revenueYear: '2025',
    rdAmount: '0', rdYear: '2000', employees: '0', qualification: [{ name: clause.QUALIFICATIONS[0], valid_until: '' },
      { name: clause.QUALIFICATIONS[1], valid_until: '2027-01-01' }], unrelated: '不发送' }, today), {
    established: '2020-01-02', region: '辽宁', revenue: { amount: 123.45, year: 2025 }, rd: { amount: 0, year: 2000 }, employees: 0,
    qualification: [{ name: clause.QUALIFICATIONS[0], valid_until: null }, { name: clause.QUALIFICATIONS[1], valid_until: '2027-01-01' }],
  });
  assert.throws(() => clause.toCompany({ employees: '1.1' }, today));
});

test('两节结果保留原句、段落、核对值和公示，空 company 提示、过期自述原样显示', () => {
  const result = clause.resultView({ notice: '原文提示', rows: [
    { type: '条件', text: '条件原句', refs: [{ pid: 'P023', quote: '条件原句' }], company: [{ key: 'rd', label: '研发费用', value: '企业自述已过期清除' }] },
    { type: '对象', text: '对象原句', refs: [], company: [] },
  ], verified: [{ value: '人工核对值', time: '17:00', quote: '核对原句' }], warning: '警示', attachment_notice: '附件', disclosure: { modelLabel: '模型', filingNo: '编号' } });
  assert.deepEqual(result.sections.map((section) => section.title), ['申报对象', '申报条件']);
  assert.equal(result.sections[0].items[0].companyHint, '请自行对照原文');
  assert.equal(result.sections[1].items[0].company[0].value, '企业自述已过期清除');
  assert.equal(result.sections[1].items[0].company[0].key, 'rd');
  assert.equal(result.sections[1].items[0].refs[0].pid, 'P023');
  assert.equal(result.verified[0].displayValue, '人工核对值 17:00');
  assert.equal(result.notice, '原文提示'); assert.equal(result.warning, '警示'); assert.equal(result.attachmentNotice, '附件');
  assert.equal(result.disclosure, '模型：模型｜备案号：编号');
  assert.deepEqual(clause.resultView({ refusal: '拒答' }).sections, []);
});

test('共享同一须知文字与本机标记，不勾选不创建；同意后仅提交一次且不存企业自述', async () => {
  const local = storage(false); const h = harness({ storage: local, session: { company: { employees: 15 } } });
  h.session.show(); assert.equal(h.state.terms, interpret.TERMS);
  h.session.agree(); await flush(); assert.equal(h.calls.length, 0);
  h.session.check(true); h.session.agree(); h.session.agree(); await flush();
  assert.equal(h.calls.length, 1); assert.deepEqual(h.calls[0].body, { kind: 'clause', doc_id: 42, company: { employees: 15 } });
  assert.ok(h.calls[0].key); assert.equal(interpret.hasConsent(local), true);
  assert.doesNotMatch(JSON.stringify([...local.saved]), /employees/); h.session.hide();
  const next = harness({ storage: local }); next.session.show(); await flush(); assert.equal(next.state.needsConsent, false); next.session.hide();
});

test('复用轮询：隐藏忽略在途结果、恢复查询已有任务、90 秒超时且晚到结果不覆盖', async () => {
  let resolve;
  const h = harness({ api: { advisorTask: () => new Promise((done) => { resolve = done; }) } });
  h.session.show(); await flush(); await h.advance(1999); assert.equal(resolve, undefined);
  await h.advance(1); h.session.hide(); assert.equal(h.timers.size, 0);
  resolve({ id: 7, status: 'done', result: {} }); await flush(); assert.equal(h.state.phase, 'processing');
  h.session.show(); await flush(); assert.equal(h.calls.length, 1);
  await h.advance(90000); assert.equal(h.state.phase, 'timeout'); assert.equal(h.timers.size, 0);
  resolve({ id: 7, status: 'done', result: {} }); await flush(); assert.equal(h.state.phase, 'timeout');
  assert.equal(h.state.error, '还在处理，可稍后在任务里查看'); h.session.hide();
});

test('失败、422、429、404 与结果失效的处理复用解读规则', async () => {
  for (const status of [422, 429, 404]) {
    const h = harness({ api: { createAdvisorTask: () => Promise.reject(Object.assign(new Error('后端 detail'), { status })) } });
    h.session.show(); await flush(); assert.equal(h.state.phase, 'failed');
    assert.equal(h.state.error, status === 404 ? '暂时没有可用工具' : '后端 detail'); assert.equal(h.state.canRetry, status === 422); h.session.hide();
  }
  for (const status of ['failed', 'cancelled', 'done']) {
    const h = harness({ session: { taskId: 7 }, api: { advisorTask: () => Promise.resolve({ id: 7, docId: 42, status, result: null, error: '结果已失效' }) } });
    h.session.show(); await flush(); assert.equal(h.state.error, '结果已失效'); assert.equal(h.state.canRetry, true); assert.equal(h.calls.length, 0); h.session.hide();
  }
});

test('taskId 回看按任务 docId 和 docTitle 覆盖旧缓存，空编号禁用原文和纠错', async () => {
  for (const docId of [55, null]) {
    const local = storage(); interpret.taskContext(local, 7, { docId: 42, title: '旧标题' }); const urls = [];
    const page = load('pages/advisor-clause/index.js', { advisorCapabilities: () => Promise.resolve(caps()),
      advisorTask: () => Promise.resolve({ id: 7, docId, docTitle: docId ? '服务端标题' : null, status: 'done', result: {} }) },
    { ...local, navigateTo: ({ url }) => urls.push(url) });
    page.onLoad({ taskId: 7 }); page.onShow(); await flush();
    assert.equal(page.data.docId, docId || ''); assert.equal(page.data.title, docId ? '服务端标题' : '');
    page.original(); page.feedback(); assert.equal(urls.length, docId ? 2 : 0);
    if (docId) assert.equal(urls[0], '/pages/detail/index?id=55'); page.onUnload();
  }
});

test('表单先显示且不自动提交；错误就近显示，全空提交，422 可回表单修改', async () => {
  let created = 0;
  const page = load('pages/advisor-clause/index.js', { advisorCapabilities: () => Promise.resolve(caps()), createAdvisorTask: () => {
    created += 1; return Promise.reject(Object.assign(new Error('注册地字段错误'), { status: 422 }));
  } }, storage());
  page.onLoad({ docId: 42 }); page.onShow(); await flush(); assert.equal(created, 0);
  assert.match(render(read('pages/advisor-clause/index.wxml'), page.data), /企业情况.*7 天后自动清除/);
  page.input({ currentTarget: { dataset: { field: 'revenueAmount' } }, detail: { value: '1.001' } }); page.submit();
  assert.match(render(read('pages/advisor-clause/index.wxml'), page.data), /至多两位小数/); assert.equal(created, 0);
  page.setData({ form: clause.emptyForm() }); page.submit(); page.submit(); await flush(); assert.equal(created, 1);
  assert.equal(page.data.error, '注册地字段错误'); page.retry(); assert.equal(page.data.phase, 'form');
  page.qualifications({ detail: { value: [clause.QUALIFICATIONS[0], clause.QUALIFICATIONS[1]] } });
  page.validity({ currentTarget: { dataset: { index: 0 } }, detail: { value: '2027-01-02' } });
  assert.equal(page.data.form.qualification[0].valid_until, '2027-01-02');
  page.clearValidity({ currentTarget: { dataset: { index: 0 } } }); assert.equal(page.data.form.qualification[0].valid_until, null);
  page.onUnload();
});

test('直达页面和工具入口须 enabled 严格为 true 且 features 含 clause，未授权渲染无 AI', async () => {
  for (const [enabled, features] of [[false, ['clause']], ['true', ['clause']], [true, []], [true, ['clause']]]) {
    const allowed = enabled === true && features.includes('clause'); let created = 0; const urls = [];
    const api = { advisorCapabilities: () => Promise.resolve(caps(enabled, features)), advisorTasks: () => Promise.resolve({ items: [] }),
      createAdvisorTask: () => { created += 1; return Promise.reject(Object.assign(new Error('额度已用完'), { status: 429 })); } };
    const page = load('pages/advisor-clause/index.js', api, storage(false));
    page.onLoad({ docId: 42 }); page.onShow(); await flush(); page.submit(); await flush();
    const output = render(read('pages/advisor-clause/index.wxml'), page.data);
    assert.equal(output.includes('使用须知'), allowed); if (!allowed) assert.doesNotMatch(output, /AI|企业情况|条款对照/);
    assert.equal(created, 0); page.onUnload();
    const tools = load('pages/advisor/index.js', api, { navigateTo: ({ url }) => urls.push(url) });
    tools.onLoad({ docId: 42 }); tools.onShow(); await flush(); tools.clause();
    const text = render(read('pages/advisor/index.wxml'), tools.data);
    assert.equal(text.includes('条款对照'), allowed); assert.equal(urls.length, allowed ? 1 : 0);
    if (allowed) { assert.match(text, /今天剩余 2 次/); tools.clearContext(); assert.doesNotMatch(render(read('pages/advisor/index.wxml'), tools.data), /填写企业情况/); }
    if (enabled !== true) assert.doesNotMatch(text, /AI/); tools.onUnload();
  }
});

test('隐藏或卸载后 capabilities 晚到不显示表单，也不启动任务', async () => {
  for (const lifecycle of ['onHide', 'onUnload']) {
    let resolve; let polled = 0;
    const page = load('pages/advisor-clause/index.js', { advisorCapabilities: () => new Promise((done) => { resolve = done; }),
      advisorTask: () => { polled += 1; return Promise.resolve({}); } }, storage());
    page.onLoad({ taskId: 7 }); page.onShow(); page[lifecycle](); resolve(caps()); await flush();
    assert.equal(page.data.aiEnabled, false); assert.equal(polled, 0);
  }
});

test('结果页渲染两节卡片、原句、企业自述、核对和底部信息；拒答不显示两节', async () => {
  const result = { notice: '固定原文提示', rows: [
    { type: '对象', text: '原句可以很长，完整显示。', refs: [{ pid: 'P001' }], company: [] },
    { type: '条件', text: '条件原句', refs: [{ pid: 'P023' }], company: [{ label: '营业收入', value: '企业自述已过期清除' }] },
  ], verified: [{ value: '核对值', quote: '核对原句' }], warning: '警示原文', attachment_notice: '附件原文', disclosure: { modelLabel: '模型名称', filingNo: '备案编号' } };
  const page = load('pages/advisor-clause/index.js', { advisorCapabilities: () => Promise.resolve(caps()), advisorTask: () => Promise.resolve({ id: 7, docId: 42, docTitle: '通知标题', status: 'done', result }) }, storage());
  page.onLoad({ taskId: 7 }); page.onShow(); await flush();
  const output = render(read('pages/advisor-clause/index.wxml'), page.data);
  for (const value of ['固定原文提示', '通知标题', '已核对', '核对原句', '申报对象', '申报条件', '通知要求', '企业自述',
    '段落 P001', '段落 P023', '请自行对照原文', '营业收入：企业自述已过期清除', '警示原文', '附件原文', '模型：模型名称｜备案号：备案编号', '第三方整理 · 非政府平台']) assert.ok(output.includes(value), value);
  page.setData({ result: clause.resultView({ refusal: '拒答原文' }) });
  const refusal = render(read('pages/advisor-clause/index.wxml'), page.data); assert.match(refusal, /拒答原文/); assert.doesNotMatch(refusal, /申报对象|申报条件/); page.onUnload();
});

test('任务、我的数据、最近任务均过滤未授权 clause；授权后类型名正确并导航结果页', async () => {
  for (const [enabled, features] of [[false, ['clause']], [true, []], [true, ['clause']]]) {
    const allowed = enabled === true && features.includes('clause'); const urls = [];
    const tasks = [{ id: 7, kind: 'clause', status: 'done', hasResult: true }, { id: 8, kind: 'compare', status: 'done', rowCount: 2, hasFile: true }];
    const api = { isLive: () => true, advisorCapabilities: () => Promise.resolve(caps(enabled, features)), advisorTasks: () => Promise.resolve({ items: tasks }),
      myData: () => Promise.resolve({ user: {}, follows: [], subscriptions: [], feedback: [], advisorTasks: tasks, retention: {} }) };
    for (const [file, handler] of [['advisor-tasks', 'interpret'], ['my-data', 'advisorResult'], ['advisor', 'tasks']]) {
      const page = load(`pages/${file}/index.js`, api, { navigateTo: ({ url }) => urls.push(url) });
      if (page.onLoad) page.onLoad({}); page.onShow(); await flush();
      const output = render(read(`pages/${file}/index.wxml`), page.data); assert.equal(output.includes('条款对照'), allowed);
      if (enabled !== true) assert.doesNotMatch(output, /AI/);
      const before = urls.length; page[handler]({ currentTarget: { dataset: { id: 7 } } });
      if (allowed) assert.equal(urls.at(-1), '/pages/advisor-clause/index?taskId=7');
      else if (file !== 'advisor') assert.equal(urls.length, before);
      if (page.onHide) page.onHide();
    }
  }
});

test('线上 clause 请求仅提交 company 与 doc_id 并带幂等头；422 显示后端 detail', async () => {
  const requests = [];
  const api = load('utils/api.js', {}, { request: (options) => requests.push(options) }, {
    './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, '../mock/advisor.js': {}, './mockSearch.js': {}, './format.js': {},
  });
  const company = { employees: 2 };
  const promise = api.createAdvisorTask({ kind: 'clause', doc_id: '42', company, extra: '不发送' }, 'clause-key');
  assert.deepEqual(plain(requests[0].data), { kind: 'clause', doc_id: 42, company }); assert.equal(requests[0].header['Idempotency-Key'], 'clause-key');
  requests[0].success({ statusCode: 202, data: { id: 7 } }); assert.equal((await promise).id, 7);
  for (const detail of ['后端原文错误', [{ loc: ['body', 'company', 'employees'], msg: '人数须为整数' }]]) {
    const failed = api.createAdvisorTask({ kind: 'clause', doc_id: 42, company }, 'retry-key');
    requests.at(-1).success({ statusCode: 422, data: { detail } });
    await assert.rejects(failed, (err) => err.status === 422 && err.message === (typeof detail === 'string' ? detail : detail[0].msg));
  }
});

test('页面已注册，所有静态文字无禁止字样；模板闭合，样式沿用颜色且长句不截断', () => {
  assert.ok(JSON.parse(read('app.json')).pages.includes('pages/advisor-clause/index'));
  const banned = /符合|不符合|满足|不满足|可以申报|判定|智能|保证|权威|最/;
  for (const file of ['pages/advisor-clause/index.wxml', 'pages/advisor-clause/index.js', 'pages/advisor-clause/index.json', 'utils/ai-clause.js']) assert.doesNotMatch(read(file), banned, file);
  assert.doesNotMatch(interpret.TERMS.join(''), banned);
  for (const file of ['advisor-clause', 'advisor', 'advisor-tasks', 'my-data']) parse(read(`pages/${file}/index.wxml`));
  assert.doesNotMatch(read('pages/advisor-clause/index.wxss'), /#[0-9a-fA-F]|gradient|text-overflow|line-clamp|iconfont/);
  assert.match(read('pages/advisor-clause/index.wxss'), /word-break: break-word/);
  const template = read('pages/advisor-clause/index.wxml');
  assert.match(template, /bindtap="original" disabled="\{\{!docId\}\}"/); assert.match(template, /bindtap="feedback" disabled="\{\{!docId\}\}"/);
});
