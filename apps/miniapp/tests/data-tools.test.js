const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const helpers = require('../utils/advisor.js');
const idempotency = require('../utils/data-idempotency.js');
const OWNER = 'a'.repeat(64);
const terms = require('../content/advisor-terms.js');
const context = require('../utils/tool-context.js');
const { render, textOf, parse, findAll } = require('./wxml-render.js');
const root = path.join(__dirname, '..');
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
const event = (data) => ({ currentTarget: { dataset: data } });
function load(file, deps, globals = {}) {
  let page; const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(root, file), 'utf8'), {
    module, Date, Math, Promise, Uint8Array, setTimeout, clearTimeout,
    // 纯函数模块（支付文案和格式化）直接用真的，其他依赖必须由测试给出
    require(key) { if (!Object.hasOwn(deps, key) && key === '../../utils/pay.js') return require('../utils/pay.js'); if (!Object.hasOwn(deps, key) && key === '../../utils/boost.js') return require('../utils/boost.js'); assert.ok(Object.hasOwn(deps, key), `${file}: ${key}`); return deps[key]; },
    Page(def) { page = def; }, ...globals,
  }, { filename: file });
  return page ? { ...page, data: plain(page.data), setData(value) { Object.assign(this.data, plain(value)); } } : module.exports;
}
const caps = (data = true, ai = false) => ({ data_tools: { enabled: data, features: data ? ['compare', 'track'] : [], remaining: { compare: 3, track: 3 } }, ai_advisor: { enabled: ai, features: ai ? ['interpret', 'clause'] : [] } });
const status = (pass = false, used = 0) => ({ enabled: true, pass: { active: pass }, quota: { compare: { used, limit: 3, maxRows: pass ? 500 : 50 }, track: { used, limit: 3, maxRows: pass ? 500 : 50 } } });
function dataLogic(api = {}, deps = {}, wx = {}) {
  return load('utils/data-tools.js', { './regions.js': require('../utils/regions.js'), './api.js': api, './advisor.js': helpers, '../content/advisor-terms.js': terms, './boost.js': require('../utils/boost.js'), ...deps }, { wx });
}
function form(options = {}) {
  const calls = []; const urls = []; let picked;
  const storage = options.storage || new Map();
  const api = { advisorCapabilities: () => Promise.resolve(caps()), advisorStatus: () => Promise.resolve(status()), me: () => Promise.resolve({ advisorAgreed: { version: terms.version }, taskOwner: OWNER }),
    advisorPreview: (body) => { calls.push(['preview', plain(body)]); return Promise.resolve({ rowCount: body.rows ? body.rows.length : 2 }); },
    createAdvisorTask: (body, key) => { calls.push(['create', plain(body), key]); return Promise.resolve({ id: 8 }); }, ...options.api };
  const wx = { getStorageSync: key => storage.get(key), setStorageSync: (key,value) => storage.set(key,plain(value)), removeStorageSync: key => storage.delete(key), setNavigationBarTitle() {}, navigateTo: ({ url }) => urls.push(url), redirectTo: ({ url }) => urls.push(url), showToast() {},
    chooseMessageFile: (o) => { picked = o; }, getFileSystemManager: () => ({ readFile: (o) => o.success({ data: 'YQ==' }) }), ...options.wx };
  const page = load('pages/advisor-form/index.js', { '../../utils/api.js': api, '../../utils/data-idempotency.js': idempotency, '../../utils/advisor.js': helpers, '../../utils/data-tools.js': dataLogic(api), '../../utils/consent.js': { ensureConsent: () => Promise.resolve(true) } }, { wx });
  page.onLoad({ kind: options.kind || 'compare' }); page.onShow();
  return { page, calls, urls, storage, picked: () => picked };
}

test('像手机号、身份证号的行能认出来（只给行号）；统一社会信用代码和普通企业名不误报（10/18 验收）', () => {
  const logic = dataLogic();
  const text = ['合成样例甲科技有限公司', '联系人 13812345678', '110105199003078211', '91370200MA3C9X7Y2K', '110105199003078214', '合成样例2026年度十佳企业'].join('\n');
  // 第 3 行身份证号校验位正确；第 5 行校验位不对，不当成身份证号
  assert.deepEqual(plain(logic.personalInfoLines(text)), [2, 3]);
  assert.deepEqual(plain(logic.personalInfoLines('合成样例乙生物有限公司\n913702001234567890')), []);
  // Codex 审阅：分段写的、全角的手机号也认；校验位不对的号码后面还有真的身份证号也认；座机（单位总机）不提醒
  const more = ['电话 138 1234 5678', '电话 138-1234-5678', '电话 １３８１２３４５６７８', '110105199003078214、110105199003078211', '总机 0532-88886666', '合成样例丙公司 2026 年 3 月'].join('\n');
  assert.deepEqual(plain(logic.personalInfoLines(more)), [1, 2, 3, 4]);
  // Codex 第 2 轮：编号、代码里的一段和金额不算手机号；句末的句号不影响
  const notPhones = ['序号A13812345678Z', '金额13812345678.00', '9137AA13812345678X', '2026-11-18', '联系 13812345678。'].join('\n');
  assert.deepEqual(plain(logic.personalInfoLines(notPhones)), [5]);
});

test('粘贴内容像个人信息时先提醒、不发预览；选"仍要预览"才发，同一段内容不重复提醒', async () => {
  const modals = [];
  let answer = { cancel: false, confirm: true };
  const h = form({ wx: { showModal: (o) => { modals.push(o); o.success(answer); } } });
  await flush();
  h.page.input({ detail: { value: '合成样例甲科技有限公司\n联系人 13812345678' } });
  h.page.preview(); await flush();
  assert.equal(modals.length, 1); assert.match(modals[0].content, /第 2 行/); assert.doesNotMatch(modals[0].content, /13812345678/);
  assert.equal(h.calls.filter((c) => c[0] === 'preview').length, 0);
  answer = { cancel: true, confirm: false };
  h.page.preview(); await flush();
  assert.equal(modals.length, 2); assert.equal(h.calls.filter((c) => c[0] === 'preview').length, 1);
  h.page.preview(); await flush();
  assert.equal(modals.length, 2);
});

test('表单写明怎样把表格放进微信、每提交一次算 1 次；通知表的长说明默认收起', () => {
  const wxml = fs.readFileSync(path.join(root, 'pages/advisor-form/index.wxml'), 'utf8');
  assert.match(wxml, /文件传输助手/); assert.match(wxml, /每提交一次算 1 次/); assert.match(wxml, /临时解析行数，不保存/);
  assert.match(wxml, /其他列（如联系人、电话）不使用、不保存，建议上传前删掉/);
  assert.match(wxml, /kind === 'track' && showFillHelp/);
});

test('我的记录：失败、取消的任务写明"这次不算次数"，完成的不写（10/18 验收）', () => {
  assert.equal(helpers.taskView({ id: 1, kind: 'compare', status: 'failed' }).refundNote, '这次不算次数');
  assert.equal(helpers.taskView({ id: 2, kind: 'track', status: 'cancelled' }).refundNote, '这次不算次数');
  assert.equal(helpers.taskView({ id: 3, kind: 'compare', status: 'done' }).refundNote, '');
  assert.equal(helpers.taskView({ id: 4, kind: 'compare', status: 'queued' }).refundNote, '');
  assert.match(fs.readFileSync(path.join(root, 'pages/advisor-tasks/index.wxml'), 'utf8'), /item\.refundNote/);
});

test('CSV 引号与多条件保留，格式错误拒绝，不把名称里的逗号拆成新客户', () => {
  const logic = dataLogic();
  assert.deepEqual(plain(logic.pasteRows('compare', '甲公司\n\n  91370000ABC  \r\n')), ['甲公司', '91370000ABC']);
  assert.deepEqual(plain(logic.pasteRows('track', '"甲,乙公司",山东,科技;创新,资金支持')), [{ name: '甲,乙公司', province: '山东', keywords: '科技;创新', supports: '资金支持', city: '' }]);
  assert.throws(() => logic.pasteRows('track', '"未闭合,山东'), /引号/);
  assert.throws(() => logic.pasteRows('track', '甲公司,,科技'), /省份/);
  // 第 6 列是区县（2026-10-15 起），第 7 列才算多出来
  assert.throws(() => logic.pasteRows('track', '甲,山东,a,b,c,d,e'), /最多六列/);
});

test('支付关闭不影响免费额度，工作台已有权益以服务端为准，缺失额度不猜测', () => {
  const logic = dataLogic();
  assert.equal(logic.quotaView(status(), 'compare').remaining, 3);
  assert.equal(logic.quotaView(status(false, 3), 'track').remaining, 0);
  assert.equal(logic.quotaView(status(true, 3), 'compare').maxRows, 500);
  assert.equal(logic.quotaView(status(true, 3), 'compare').passActive, true);
  assert.throws(() => logic.quotaView({ enabled: true }, 'track'), /额度/);
});

test('未单独同意时，粘贴预览与文件选择都只去说明页，不传客户信息', async () => {
  const h = form({ api: { me: () => Promise.resolve({ advisorAgreed: null }) } }); await flush();
  h.page.input({ detail: { value: '合成企业甲' } }); h.page.preview(); h.page.choose(); h.page.submit();
  assert.equal(h.calls.length, 0); assert.equal(h.picked(), undefined);
  assert.ok(h.urls.every((url) => url === '/pages/advisor-terms/index'));
  h.page.onUnload();
});

test('免费粘贴任务：预览成功才提交；超时重试保留幂等键，成功清理输入且重复点击不重建', async () => {
  let sent = 0; const keys = [];
  const h = form({ api: { createAdvisorTask: (body, key) => { sent += 1; keys.push(key); return sent === 1 ? Promise.reject(new Error('超时')) : Promise.resolve({ id: 9 }); } } }); await flush();
  h.page.input({ detail: { value: '合成企业甲\n合成企业乙' } }); h.page.submit();
  assert.equal(sent, 0); assert.match(h.page.data.inputError, /预览/);
  h.page.preview(); await flush(); assert.equal(h.page.data.previewed, true);
  h.page.submit(); h.page.submit(); await flush(); assert.equal(sent, 1);
  h.page.submit(); await flush(); assert.equal(sent, 2); assert.equal(keys[0], keys[1]);
  assert.equal(h.page.data.text, ''); assert.equal(h.page.data.accepted, true);
  assert.equal(h.urls.at(-1), '/pages/advisor-tasks/index?taskId=9');
  h.page.submit(); assert.equal(sent, 2); h.page.onUnload();
});

test('修改输入取消旧预览并换幂等键；超限和额度耗尽有反馈且不提交', async () => {
  const h = form(); await flush();
  h.page.input({ detail: { value: '甲公司' } }); h.page.preview(); await flush(); const key = h.page.key;
  h.page.input({ detail: { value: '乙公司' } }); assert.equal(h.page.data.previewed, false); assert.notEqual(h.page.key, key);
  h.page.input({ detail: { value: Array(51).fill('合成企业').join('\n') } }); h.page.preview();
  assert.match(h.page.data.inputError, /最多 50/); assert.equal(h.calls.length, 1);
  h.page.input({ detail: { value: '合成企业' } });
  h.page.setData({ remaining: 0, previewed: true }); h.page.submit();
  assert.match(h.page.data.inputError, /本月免费次数已用完/); assert.equal(h.calls.filter(c => c[0] === 'create').length, 0);
  h.page.onUnload();
});

test('文件上传验证大小与扩展名，服务器预览行数超过权益上限时不能提交', async () => {
  const h = form({ api: { advisorPreview: () => Promise.resolve({ rowCount: 51 }) } }); await flush();
  h.page.mode(event({ mode: 'file' })); h.page.choose();
  h.picked().success({ tempFiles: [{ name: 'bad.exe', size: 10, path: '/fake' }] }); assert.match(h.page.data.inputError, /XLSX/);
  h.page.choose(); h.picked().success({ tempFiles: [{ name: 'large.xlsx', size: 61441, path: '/fake' }] }); assert.match(h.page.data.inputError, /60 KB/);
  h.page.choose(); h.picked().success({ tempFiles: [{ name: 'synthetic.csv', size: 1, path: '/fake' }] }); await flush();
  assert.equal(h.page.data.rowCount, 51); assert.equal(h.page.data.previewed, false); assert.match(h.page.data.inputError, /最多 50/);
  h.page.submit(); assert.equal(h.calls.length, 0); h.page.onUnload(); assert.equal(h.page.file, null);
});

test('客户通知表文件预览和提交使用当前后端合同，不发送本机客户卡片或 AI 字段', async () => {
  const h = form({ kind: 'track' }); await flush(); h.page.mode(event({ mode: 'file' })); h.page.choose();
  h.picked().success({ tempFiles: [{ name: 'synthetic.csv', size: 1, path: '/fake' }] }); await flush(); h.page.submit(); await flush();
  assert.deepEqual(h.calls.map(c => c[0]), ['preview', 'create']);
  assert.deepEqual(h.calls[0][1], { kind: 'track', file: { name: 'synthetic.csv', content_base64: 'YQ==' } });
  assert.deepEqual(h.calls[1][1], h.calls[0][1]); h.page.onUnload();
});

test('服务器撤回同意或额度变化时清理可提交状态；未开放与网络错误不显示死表单', async () => {
  const h = form({ api: { advisorPreview: () => Promise.reject(Object.assign(new Error('请先同意工具说明'), { status: 403 })) } }); await flush();
  h.page.input({ detail: { value: '合成企业' } }); h.page.preview(); await flush();
  assert.equal(h.page.data.agreed, false); assert.equal(h.page.data.previewed, false); h.page.onUnload();
  for (const reason of ['off', 'offline', 'consent']) {
    const x = form({ api: { advisorCapabilities: () => Promise.resolve({ ...caps(false), reason }) } }); await flush();
    assert.equal(x.page.data.enabled, false); assert.equal(x.page.data.loading, false);
    assert.doesNotMatch(textOf(render('pages/advisor-form/index.wxml', x.page.data)), /提交任务/);
    x.page.onUnload();
  }
});

test('卸载时忽略迟到的能力结果；未同意说明的表单不因其他页面返回而丢掉输入', async () => {
  let finish; const h = form({ api: { advisorCapabilities: () => new Promise((resolve) => { finish = resolve; }) } });
  h.page.onUnload(); finish(caps()); await flush(); assert.equal(h.page.data.enabled, false);
  const x = form(); await flush(); x.page.input({ detail: { value: '合成企业' } }); const key = x.page.key;
  x.page.onHide(); x.page.onShow(); await flush(); assert.equal(x.page.data.text, '合成企业'); assert.equal(x.page.key, key); x.page.onUnload();
});

function downloadHarness(options = {}) {
  const calls = []; const partSize = 900 * 1024; const size = partSize + 3;
  const api = { isLive: () => true, downloadAdvisorTask: () => Promise.resolve({ size, partSize, parts: 2 }),
    advisorTaskPart: (id, part) => { calls.push(['part', id, part]); return Promise.resolve(new Uint8Array(part ? [4, 5, 6] : partSize).buffer); }, ...options.api };
  const logic = dataLogic(api, {
    './exporter.js': { save: (buffer, format) => { calls.push(['save', buffer.byteLength, format, [...new Uint8Array(buffer).slice(-3)]]); return Promise.resolve('/fake/result.xlsx'); }, clearOld: () => calls.push(['clearOld']), open: () => { calls.push(['open']); return Promise.resolve(); } },
    './exports.js': { add: (record) => { calls.push(['record', record]); return true; } },
  }, { getFileSystemManager() {} });
  return { calls, logic };
}

test('真实分片下载按顺序拼接校验，写入导出记录后打开；不会访问临时公网 URL', async () => {
  const h = downloadHarness(); await h.logic.openResult(9, { task: { kind: 'track', rowCount: 2 } });
  assert.deepEqual(h.calls.slice(0, 2), [['part', 9, 0], ['part', 9, 1]]);
  assert.deepEqual(h.calls[2], ['save', 900 * 1024 + 3, 'xlsx', [4, 5, 6]]);
  assert.equal(h.calls[3][1].source, 'advisor'); assert.equal(h.calls[3][1].taskId, 9); assert.equal(h.calls[3][1].count, 2);
  assert.equal(h.calls.at(-1)[0], 'open');
});

test('拒绝异常元数据、短片、过期、越权与后台切换；不保存不完整结果', async () => {
  for (const meta of [{ size: 0, parts: 1, partSize: 921600 }, { size: 21 * 1024 * 1024, parts: 24, partSize: 921600 }, { size: 3, parts: 2, partSize: 921600 }, { size: 3, parts: 1, partSize: 1000000 }]) {
    const h = downloadHarness({ api: { downloadAdvisorTask: () => Promise.resolve(meta) } });
    await assert.rejects(h.logic.openResult(9), /结果文件信息无效/); assert.equal(h.calls.length, 0);
  }
  const short = downloadHarness({ api: { advisorTaskPart: () => Promise.resolve(new ArrayBuffer(2)) } });
  await assert.rejects(short.logic.openResult(9), /没有取完整/); assert.equal(short.calls.length, 0);
  for (const statusCode of [403, 404, 410]) {
    const h = downloadHarness({ api: { advisorTaskPart: () => Promise.reject(Object.assign(new Error('拒绝下载'), { status: statusCode })) } });
    await assert.rejects(h.logic.openResult(9), (e) => e.status === statusCode); assert.equal(h.calls.length, 0);
  }
  const hidden = downloadHarness(); await assert.rejects(hidden.logic.openResult(9, { current: () => false }), (e) => e.cancelled); assert.equal(hidden.calls.length, 0);
});

test('共同任务中心四种能力分别过滤；过期结果没有下载按钮，AI 关闭不阻断数据任务轮询', async () => {
  const timers = new Map(); let serial = 0; let listed = 0; let visibleCaps = caps(true, false);
  let items = [{ id: 1, kind: 'compare', status: 'queued', rowCount: 2 }, { id: 2, kind: 'interpret', status: 'done' }, { id: 3, kind: 'track', status: 'done', hasFile: true, expiresAt: '2020-01-01', rowCount: 1 }];
  const api = { advisorCapabilities: () => Promise.resolve(visibleCaps), advisorTasks: () => { listed += 1; return Promise.resolve({ items }); } };
  const page = load('pages/advisor-tasks/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers }, {
    wx: { setNavigationBarTitle() {} }, setTimeout(fn) { const id = ++serial; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); },
  });
  page.onShow(); await flush(); assert.deepEqual(page.data.items.map(i => i.kind), ['compare', 'track']);
  let visible = textOf(render('pages/advisor-tasks/index.wxml', page.data)); assert.doesNotMatch(visible, /AI|下载 Excel/); assert.match(visible, /已过期/);
  const poll = [...timers.values()][0]; page.onHide(); assert.equal(timers.size, 0); poll(); await flush(); assert.equal(listed, 1);
  items = [{ id: 1, kind: 'compare', status: 'done', hasFile: true, rowCount: 2 }]; page.onShow(); await flush();
  visible = textOf(render('pages/advisor-tasks/index.wxml', page.data)); assert.match(visible, /下载 Excel/); assert.equal(timers.size, 0);
  visibleCaps = caps(false, true); page.onShow(); await flush(); assert.equal(page.data.items.length, 0); page.onUnload();
});

test('任务中心取消、删除按任务身份调用；下载遇到下架响应停止展示旧下载入口', async () => {
  const calls = [];
  const task = { id: 5, kind: 'compare', status: 'done', hasFile: true, rowCount: 2 };
  const api = { advisorCapabilities: () => Promise.resolve(caps()), advisorTasks: () => Promise.resolve({ items: [task] }),
    cancelAdvisorTask: (id) => { calls.push(['cancel', id]); return Promise.resolve(); }, deleteAdvisorTask: (id) => { calls.push(['delete', id]); return Promise.resolve(); } };
  const page = load('pages/advisor-tasks/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers, '../../utils/data-tools.js': { openResult: () => Promise.reject(Object.assign(new Error('结果已下架，请重新生成'), { status: 410 })) } }, { wx: { setNavigationBarTitle() {}, showModal: (o) => o.success({ confirm: true }) } });
  page.onShow(); await flush(); page.download(event({ id: 5 })); await flush();
  assert.equal(page.data.items[0].downloadable, false); assert.match(page.data.actionError, /已下架/); assert.equal(page.data.busyId, null);
  page.cancel(event({ id: 5 })); await flush(); page.remove(event({ id: 5 })); await flush(); assert.deepEqual(calls, [['cancel', 5], ['delete', 5]]); page.onUnload();
});

test('独立说明须主动勾选，并先确认通用协议，再记录 advisor 版本', async () => {
  const calls = [];
  const api = { advisorStatus: () => Promise.resolve(status()), agree: (...args) => { calls.push(args); return Promise.resolve(); } };
  const page = load('pages/advisor-terms/index.js', { '../../utils/api.js': api, '../../content/advisor-terms.js': terms,
    '../../utils/data-tools.js': { toolConsent: () => Promise.resolve(false) }, '../../utils/consent.js': { ensureConsent: () => { calls.push(['general']); return Promise.resolve(true); } } }, { wx: { navigateBack() {} } });
  page.onShow(); await flush(); page.agree(); assert.equal(calls.length, 0);
  page.check({ detail: { value: ['agree'] } }); page.agree(); page.agree(); await flush();
  assert.deepEqual(calls, [['general'], [terms.version, 'advisor']]); assert.equal(page.data.agreed, true); page.onUnload();
});

test('新增表单和独立说明均注册，模板完整；工具入口覆盖首页、我的、客户页', () => {
  const app = JSON.parse(fs.readFileSync(path.join(root, 'app.json'), 'utf8'));
  // 21 页完整候选 + 收费第一期的购买页、订单页（D-058，入口只在服务端在售或有订单时出现）
  // + 加量体验的确认页（D-070 ③，服务端开关开着才有入口）
  assert.equal(app.pages.length, 24);
  for (const page of ['advisor-form', 'advisor-terms', 'advisor-trial', 'pass', 'orders']) {
    assert.ok(app.pages.includes(`pages/${page}/index`)); parse(fs.readFileSync(path.join(root, `pages/${page}/index.wxml`), 'utf8'));
  }
  for (const page of ['home', 'me', 'clients']) assert.match(fs.readFileSync(path.join(root, `pages/${page}/index.wxml`), 'utf8'), /工具中心/);
  assert.match(fs.readFileSync(path.join(root, 'pages/clients/index.wxml'), 'utf8'), /不会自动发送/);
});

test('真实 API 层保留两类能力、独立同意 scope 与免费工具合同，分片是鉴权二进制 GET', async () => {
  const calls = [];
  const api = load('utils/api.js', { './config.js': { devBaseUrl: 'https://synthetic.invalid', cloudService: 'api' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {} }, {
    wx: { request(o) { calls.push(o); const pathname = new URL(o.url).pathname;
      const data = pathname.endsWith('/capabilities') ? caps(true, false) : o.responseType === 'arraybuffer' ? new Uint8Array([1, 2, 3]).buffer : {};
      o.success({ statusCode: 200, data });
    } }, setTimeout() { return 1; }, clearTimeout() {},
  });
  assert.equal((await api.advisorCapabilities()).data_tools.enabled, true);
  assert.equal((await api.advisorCapabilities()).ai_advisor.enabled, false);
  await api.agree(terms.version, 'advisor'); assert.deepEqual(plain(calls.at(-1).data), { version: terms.version, scope: 'advisor' });
  await api.agree('2026-11'); assert.deepEqual(plain(calls.at(-1).data), { version: '2026-11' });
  await api.advisorPreview({ kind: 'track', rows: [{ name: '合成企业', province: '山东' }], doc_id: 1 });
  assert.deepEqual(plain(calls.at(-1).data), { kind: 'track', rows: [{ name: '合成企业', province: '山东' }] });
  await api.advisorStatus(); assert.equal(new URL(calls.at(-1).url).pathname, '/api/v1/advisor/status');
  await api.downloadAdvisorTask(8); assert.equal(new URL(calls.at(-1).url).pathname, '/api/v1/advisor/tasks/8/download');
  const bytes = await api.advisorTaskPart(8, 1); assert.equal(bytes.byteLength, 3);
  assert.equal(calls.at(-1).method, 'GET'); assert.equal(calls.at(-1).dataType, 'other'); assert.equal(calls.at(-1).responseType, 'arraybuffer');
  assert.equal(new URL(calls.at(-1).url).search, '?part=1'); assert.ok(calls.every(o => !o.url.includes('/pay/')));
});

test('真实 API 层明确呈现免费额度错误与下载失效，403 不当作正常二进制', async () => {
  let response = { statusCode: 429, data: { detail: { code: 'FREE_MONTHLY_LIMIT', message: '本月免费次数已用完' } } };
  const api = load('utils/api.js', { './config.js': { devBaseUrl: 'https://synthetic.invalid', cloudService: 'api' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {} }, {
    wx: { request: o => o.success(response), removeStorageSync() {} }, setTimeout() { return 1; }, clearTimeout() {},
  });
  await assert.rejects(api.createAdvisorTask({ kind: 'compare', rows: ['合成企业'] }, 'same-key'), e => e.code === 'FREE_MONTHLY_LIMIT' && e.message === '本月免费次数已用完');
  for (const statusCode of [403, 404, 410]) {
    response = { statusCode, data: new ArrayBuffer(0) };
    await assert.rejects(api.advisorTaskPart(8, 0), e => e.status === statusCode && /协议|没有找到|已过期/.test(e.message));
  }
});

test('读取任务期间权限被撤回时清空旧任务；文件记录重下只走工具鉴权链路', async () => {
  const page = load('pages/advisor-tasks/index.js', { '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(caps()), advisorTasks: () => Promise.reject(Object.assign(new Error('权限已变更'), { status: 403 })) }, '../../utils/advisor.js': helpers }, { wx: { setNavigationBarTitle() {} } });
  page.setData({ items: [{ id: 1, kind: 'compare', status: 'done' }] }); page.onShow(); await flush();
  assert.equal(page.data.items.length, 0); assert.equal(page.data.toolsEnabled, false); page.onUnload();
  const calls = []; let guard;
  const filePage = load('pages/files/index.js', {
    '../../utils/api.js': { isLive: () => true }, '../../utils/exports.js': { list: () => [{ id: 'advisor-8', source: 'advisor', taskId: 8, kind: 'track', count: 2, format: 'xlsx', at: '', path: '/fake' }] },
    '../../utils/exporter.js': { exists: () => false, exportDocs: () => { throw new Error('不能把任务编号当文档导出'); } }, '../../utils/format.js': { shortTime: () => '' },
    '../../utils/data-tools.js': { openResult: (id, options) => { calls.push(id); guard = options.current; return Promise.resolve(); } },
  }, { wx: { showLoading() {}, hideLoading() {} } });
  filePage.onShow(); filePage.reexport(event({ index: 0 })); assert.equal(guard(), true); filePage.onHide(); assert.equal(guard(), false); await flush();
  assert.deepEqual(calls, [8]); filePage.onUnload();
});

test('服务端免费次数刚耗尽时丢弃购买引导，两类行数上限均作废预览并采用服务端上限', async () => {
  for (const [code, message, maxRows] of [
    ['FREE_MONTHLY_LIMIT', '本月免费次数已用完，买一次客户工作台可用 30 天，到期不扣钱', null],
    ['FREE_ROW_LIMIT', '每次最多提交 50 行', 50], ['PASS_ROW_LIMIT', '每次最多提交 500 行', 500],
  ]) {
    const h = form({ api: { createAdvisorTask: () => Promise.reject(Object.assign(new Error(message), { code, status: 429, detail: { maxRows } })) } }); await flush();
    h.page.input({ detail: { value: '合成企业' } }); h.page.preview(); await flush(); h.page.submit(); await flush();
    if (code === 'FREE_MONTHLY_LIMIT') { assert.match(h.page.data.inputError, /下月可继续/); assert.doesNotMatch(h.page.data.inputError, /买一次|30 天/); }
    else { assert.equal(h.page.data.previewed, false); assert.equal(h.page.data.maxRows, maxRows); }
    h.page.onUnload();
  }
});

test('文件使用最新额度再次校验，旧预览在返回刷新权限后不能恢复可提交状态', async () => {
  let paid = true; let resolvePreview;
  const h = form({ api: { advisorStatus: () => Promise.resolve(status(paid)), advisorPreview: () => new Promise(resolve => { resolvePreview = resolve; }) } }); await flush();
  h.page.mode(event({ mode: 'file' })); h.page.file = { name: 'synthetic.csv', content_base64: 'YQ==' }; h.page.preview();
  paid = false; h.page.onHide(); h.page.onShow(); await flush();
  resolvePreview({ rowCount: 100 }); await flush(); assert.equal(h.page.data.previewed, false); assert.equal(h.page.data.maxRows, 50);
  h.page.setData({ rowCount: 100, previewed: true }); h.page.submit();
  assert.match(h.page.data.inputError, /最多 50/); assert.equal(h.calls.length, 0); h.page.onUnload();
});

test('410按文件身份保留：轮询、刷新、销毁重进不复活，文件更新或任务消失才解除', async () => {
  let item = { id: 98761, kind: 'compare', status: 'done', hasFile: true, expiresAt: '2099-01-01' };
  const api = { advisorCapabilities: () => Promise.resolve(caps()), advisorTasks: () => Promise.resolve({ items: item ? [item] : [] }) };
  const make = () => load('pages/advisor-tasks/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers, '../../utils/data-tools.js': { openResult: () => Promise.reject(Object.assign(new Error('结果已下架'), { status: 410 })) } }, { wx: { setNavigationBarTitle() {} } });
  let page = make(); page.onShow(); await flush(); page.download(event({ id: item.id })); await flush(); page.poll(page.epoch); await flush();
  assert.equal(page.data.items[0].downloadable, false); page.onUnload(); page = make(); page.onShow(); await flush(); assert.equal(page.data.items[0].downloadable, false);
  item = { ...item, expiresAt: '2099-02-01' }; page.load(); await flush(); assert.equal(page.data.items[0].downloadable, true);
  page.download(event({ id: item.id })); await flush(); const previous = item; item = null; page.load(); await flush(); item = previous; page.load(); await flush(); assert.equal(page.data.items[0].downloadable, true); page.onUnload();
});

test('真实 exporter 模块接线可下载；页面离开后异步写入只删除本次文件，不误删新导出', async () => {
  const files = new Map(); const writes = []; const added = []; const opened = [];
  const fs = { writeFile(o) { writes.push(o); }, readdirSync() { return [...files.keys()].map(p => p.split('/').at(-1)); }, unlinkSync(p) { files.delete(p); }, accessSync(p) { if (!files.has(p)) throw new Error('ENOENT'); } };
  const wx = { env: { USER_DATA_PATH: '/local' }, getFileSystemManager: () => fs, openDocument(o) { opened.push(o.filePath); o.success(); } };
  const log = { add: r => { added.push(r); return true; } };
  const exporter = load('utils/exporter.js', { './api.js': {}, './format.js': { todayISO: () => '2026-10-15' }, './exports.js': log }, { wx });
  assert.equal(typeof exporter.save, 'function'); assert.equal(typeof exporter.clearOld, 'function'); assert.equal(typeof exporter.remove, 'function');
  const api = { isLive: () => true, downloadAdvisorTask: () => Promise.resolve({ size: 3, partSize: 900 * 1024, parts: 1 }), advisorTaskPart: () => Promise.resolve(new Uint8Array([1, 2, 3]).buffer) };
  const logic = dataLogic(api, { './exporter.js': exporter, './exports.js': log }, wx);
  let current = true; const pending = logic.openResult(100, { current: () => current }); await flush(); assert.equal(writes.length, 1);
  current = false;
  const newer = exporter.save(new Uint8Array([4]).buffer, 'xlsx'); assert.notEqual(writes[0].filePath, writes[1].filePath);
  files.set(writes[1].filePath, writes[1].data); writes[1].success(); await newer;
  files.set(writes[0].filePath, writes[0].data); writes[0].success(); await assert.rejects(pending, e => e.cancelled);
  assert.equal(files.has(writes[0].filePath), false); assert.equal(files.has(writes[1].filePath), true); assert.equal(added.length, 0); assert.equal(opened.length, 0);
  current = true; const done = logic.openResult(100); await flush(); files.set(writes[2].filePath, writes[2].data); writes[2].success(); await done;
  assert.equal(added.length, 1); assert.equal(opened.length, 1); assert.deepEqual([...new Uint8Array(files.get(opened[0]))], [1, 2, 3]);
  assert.equal(exporter.remove('/local/private.txt'), false);
});

test('文件页清空确认使在途任务下载失效，清空后不能重新写入记录', async () => {
  let guard; let resolve; let cleared = 0;
  const page = load('pages/files/index.js', {
    '../../utils/api.js': { isLive: () => true }, '../../utils/format.js': { shortTime: () => '' },
    '../../utils/exports.js': { list: () => [{ id: 'advisor-9', source: 'advisor', taskId: 9, count: 1, format: 'xlsx' }], clear: () => { cleared += 1; return true; } },
    '../../utils/exporter.js': { exists: () => false, removeFiles: () => 0 },
    '../../utils/data-tools.js': { openResult: (_id, options) => { guard = options.current; return new Promise(r => { resolve = r; }); } },
  }, { wx: { showLoading() {}, hideLoading() {}, showToast() {}, showModal: o => o.success({ confirm: true }) } });
  page.onShow(); page.reexport(event({ index: 0 })); assert.equal(guard(), true); page.clearAll(); assert.equal(guard(), false); assert.equal(cleared, 1); resolve(); await flush(); page.onUnload();
});

test('从微信文件选择器返回时先刷新能力，读取完成会等待能力后自动预览，不静默丢失', async () => {
  let refreshing = false; let resolveStatus;
  const h = form({ api: { advisorStatus: () => refreshing ? new Promise(resolve => { resolveStatus = resolve; }) : Promise.resolve(status()) } }); await flush();
  h.page.mode(event({ mode: 'file' })); h.page.choose(); h.page.onHide(); refreshing = true; h.page.onShow(); await flush();
  h.picked().success({ tempFiles: [{ name: 'synthetic.csv', path: '/fake', size: 1 }] }); await flush();
  assert.equal(h.page.data.enabled, false); assert.equal(h.calls.length, 0);
  resolveStatus(status()); await flush(); assert.equal(h.calls[0][0], 'preview'); assert.equal(h.page.data.previewed, true); h.page.onUnload();
});

test('502、503、504提交结果不确定都明确提示，保持同一内容幂等键可重试', async () => {
  for (const statusCode of [502, 503, 504]) {
    const h = form({ api: { createAdvisorTask: () => Promise.reject(Object.assign(new Error('网关暂不可用'), { status: statusCode })) } }); await flush();
    h.page.input({ detail: { value: '合成企业' } }); h.page.preview(); await flush();
    h.page.submit(); await flush(); const key = h.page.key; h.page.submit(); await flush(); assert.match(h.page.data.inputError, /状态尚未确认/); assert.equal(h.page.key, key); h.page.onUnload();
  }
});

test('数据工具已开时保留其他工具同意入口，不因整体enabled清掉AI gate', async () => {
  let agreed = 0;
  const page = load('pages/advisor/index.js', {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve({ ...caps(true, false), reason: 'consent' }), advisorStatus: () => Promise.resolve(status()), advisorTasks: () => Promise.resolve({ items: [] }) },
    '../../utils/advisor.js': helpers, '../../utils/tool-context.js': context,
    '../../utils/consent.js': { ensureConsent: () => { agreed += 1; return Promise.resolve(false); } },
  }, { wx: { setNavigationBarTitle() {} } });
  page.onLoad({}); page.onShow(); await flush(); assert.equal(page.data.toolsEnabled, true); assert.equal(page.data.aiGate, 'consent');
  assert.match(textOf(render('pages/advisor/index.wxml', page.data)), /阅读并同意后查看其他工具/); page.consent(); await flush(); assert.equal(agreed, 1); page.onUnload();
});

test('文件重下错误先关闭loading再显示提示，不被hideLoading吞掉', async () => {
  const order = [];
  const page = load('pages/files/index.js', {
    '../../utils/api.js': { isLive: () => true }, '../../utils/format.js': { shortTime: () => '' },
    '../../utils/exports.js': { list: () => [{ source: 'advisor', taskId: 2, count: 1, format: 'xlsx' }] },
    '../../utils/exporter.js': { exists: () => false }, '../../utils/data-tools.js': { openResult: () => Promise.reject(new Error('结果已下架')) },
  }, { wx: { showLoading() {}, hideLoading() { order.push('hide'); }, showToast(o) { order.push(o.title); } } });
  page.onShow(); page.reexport(event({ index: 0 })); await flush(); assert.deepEqual(order, ['hide', '结果已下架']); page.onUnload();
});

test('客户通知表旧四列保留关键词支持方式，第五列才解释为城市', () => {
  const logic = dataLogic();
  assert.deepEqual(plain(logic.pasteRows('track', '合成公司,山东,科技;创新,资金支持;资格认定')), [{ name: '合成公司', province: '山东', keywords: '科技;创新', supports: '资金支持;资格认定', city: '' }]);
  assert.equal(logic.pasteRows('track', '合成公司,山东,,,青岛')[0].city, '青岛');
});

test('同一请求504后销毁页面，重建表单即使免费额度已耗尽也复用键，只创建一次任务', async () => {
  const storage = new Map(); const server = new Map(); const keys = []; let charge = 0;
  const createAdvisorTask = (body, key) => {
    keys.push(key);
    if (!server.has(key)) { charge += 1; server.set(key, { id: 88 }); return Promise.reject(Object.assign(new Error('响应丢失'), { status: 504 })); }
    return Promise.resolve(server.get(key));
  };
  const first = form({ storage, api: { createAdvisorTask } }); await flush();
  first.page.input({ detail: { value: '合成跨页企业' } }); first.page.preview(); await flush(); first.page.submit(); await flush();
  assert.match(first.page.data.inputError, /状态尚未确认/); first.page.onUnload();
  const retry = form({ storage, api: { createAdvisorTask, advisorStatus: () => Promise.resolve(status(false, 3)) } }); await flush();
  retry.page.input({ detail: { value: '合成跨页企业' } }); retry.page.preview(); await flush();
  const button = findAll(render('pages/advisor-form/index.wxml', retry.page.data), n => n.attrs.bindtap === 'submit')[0];
  assert.equal(button.attrs.disabled, false);
  retry.page.submit(); await flush();
  assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]); assert.equal(charge, 1);
  assert.equal(retry.page.data.accepted, true); assert.equal(storage.get(idempotency.STORAGE_KEY).entries.length, 0);
  assert.doesNotMatch(JSON.stringify([...storage]), /合成跨页企业/); retry.page.onUnload();
});
test('跨页面文件重选保留幂等键与未知结果；损坏存储禁止发送', async () => {
  const storage = new Map(); const keys = [];
  const api = { createAdvisorTask: (_, key) => { keys.push(key); return Promise.reject(new Error('断网')); } };
  for (let i = 0; i < 2; i++) {
    const h = form({ storage, api }); await flush(); h.page.mode(event({ mode: 'file' })); h.page.choose();
    h.picked().success({ tempFiles: [{ name: 'synthetic.csv', size: 1, path: '/fake' }] }); await flush(); h.page.submit(); await flush(); h.page.onUnload();
  }
  assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]); assert.doesNotMatch(JSON.stringify([...storage]), /YQ==|synthetic.csv/);
  storage.set(idempotency.STORAGE_KEY, { owner: OWNER, entries: [null] });
  const h = form({ storage, api }); await flush(); assert.equal(h.page.data.enabled, false); assert.match(h.page.data.error, /无法读取/); assert.equal(keys.length, 2); h.page.onUnload();
});
test('新请求明确422拒绝后清理键；已未知请求后来403不能当作之前未创建', async () => {
  let rejected = 422;
  const h = form({ api: { createAdvisorTask: () => Promise.reject(Object.assign(new Error('拒绝'), { status: rejected })) } }); await flush();
  h.page.input({ detail: { value: '合成企业' } }); h.page.preview(); await flush(); h.page.submit(); await flush();
  assert.equal(h.storage.get(idempotency.STORAGE_KEY).entries.length, 0);
  rejected = 504; h.page.submit(); await flush(); const key = h.page.key;
  rejected = 403; h.page.submit(); await flush();
  assert.equal(h.storage.get(idempotency.STORAGE_KEY).entries[0].key, key); h.page.onUnload();
});
test('账号切换清空旧输入与摘要；旧账号迟到结果不占据新账号表单', async () => {
  let owner = OWNER; let finish;
  const h = form({ api: { me: () => Promise.resolve({ advisorAgreed: { version: terms.version }, taskOwner: owner }), createAdvisorTask: () => new Promise(resolve => { finish = resolve; }) } }); await flush();
  h.page.input({ detail: { value: '旧账号合成客户' } }); h.page.preview(); await flush(); h.page.submit();
  owner = 'b'.repeat(64); h.page.onHide(); h.page.onShow(); await flush();
  assert.equal(h.page.data.text, ''); assert.equal(h.page.data.submitting, false);
  finish({ id: 9 }); await flush(); assert.equal(h.page.data.accepted, false); assert.equal(h.urls.length, 0);
  assert.equal(h.storage.get(idempotency.STORAGE_KEY).owner, owner); assert.equal(h.storage.get(idempotency.STORAGE_KEY).entries.length, 0); h.page.onUnload();
});
test('旧服务未返回账号摘要时明确关闭提交；全称省市粘贴兼容旧四列', async () => {
  const h = form({ api: { me: () => Promise.resolve({ advisorAgreed: { version: terms.version } }) } }); await flush();
  assert.equal(h.page.data.enabled, false); assert.match(h.page.data.error, /服务版本/); h.page.onUnload();
  const logic = dataLogic();
  assert.deepEqual(plain(logic.pasteRows('track', '合成企业,山东省,创新,资金支持,青岛市')), [{ name: '合成企业', province: '山东', keywords: '创新', supports: '资金支持', city: '青岛' }]);
  assert.equal(logic.pasteRows('track', '合成企业,吉林省,,,延边朝鲜族自治州')[0].city, '延边朝鲜族自治州');
});
test('客户通知表粘贴按服务端城市规则逐行报错：只填一个、32字内、无分隔符，国家部委不带城市，直辖市归省级，空行计入行号', async () => {
  const logic = dataLogic();
  for (const [text, message] of [
    ['合成甲,山东,,,青岛;烟台', /第 1 行城市只填一个/], ['合成甲,山东,,,青岛；烟台', /第 1 行城市/], ['合成甲,山东,,,青岛，烟台', /第 1 行城市/],
    ['合成甲,山东,,,青岛/烟台', /第 1 行城市/], ['合成甲,山东,,,"青岛,烟台"', /第 1 行城市/], [`合成甲,山东,,,${'城'.repeat(33)}`, /第 1 行城市/],
    ['合成甲,山东\n\n合成乙,国家部委,科技,,北京', /第 3 行省份为国家部委时城市请留空/], ['合成甲,国家,,,青岛', /第 1 行省份为国家部委/],
    ['合成甲,山东,科技,资金支持,青岛,市南区,烟台', /第 1 行最多六列/], ['合成甲,山东\n\n\n合成乙', /第 4 行请填写客户名称和省份/],
    ['合成甲,山东,,,青岛、烟台', /第 1 行城市只填一个/], ['合成甲,山东,,,青岛|烟台', /第 1 行城市只填一个/], ['合成甲,山东,,,青岛\\烟台', /第 1 行城市只填一个/],
  ]) assert.throws(() => logic.pasteRows('track', text), message, text);
  assert.equal(logic.pasteRows('track', `合成甲,山东,,,${'城'.repeat(32)}市`)[0].city, '城'.repeat(32));
  assert.deepEqual(plain(logic.pasteRows('track', '合成甲,北京市,,,北京市\n合成乙,重庆,,,重庆\n合成丙,国家部委,科技;创新,资金支持;资格认定')).map(r => [r.province, r.city, r.keywords, r.supports]),
    [['北京', '', '', ''], ['重庆', '', '', ''], ['国家部委', '', '科技;创新', '资金支持;资格认定']]);
  const h = form({ kind: 'track' }); await flush();
  h.page.input({ detail: { value: '合成甲,山东,,,青岛;烟台' } }); assert.match(h.page.data.inputError, /第 1 行城市只填一个/); assert.equal(h.page.data.rowCount, 0);
  h.page.preview(); assert.equal(h.calls.length, 0);
  // 10/18：省、市、区县的长说明默认收起，点开后才显示
  assert.doesNotMatch(textOf(render('pages/advisor-form/index.wxml', h.page.data)), /区县写完整名称/);
  h.page.toggleFillHelp();
  const shown = textOf(render('pages/advisor-form/index.wxml', h.page.data));
  assert.match(shown, /关键词、支持方式多项用分号分隔，城市只填一个/); assert.match(shown, /国家部委不填城市/); assert.doesNotMatch(shown, /后三项可留空，多项用分号分隔/);
  h.page.onUnload();
});
test('真实表单A未知提交→切到B→回A：同一内容沿用A的键只扣一次，B另起键，A的键切换时不丢', async () => {
  const B = 'b'.repeat(64); let owner = OWNER;
  const storage = new Map(); const server = new Map(); const keys = [];
  const createAdvisorTask = (body, key) => {
    keys.push(key);
    if (!server.has(key)) { server.set(key, { id: server.size + 1 }); return Promise.reject(Object.assign(new Error('响应丢失'), { status: 504 })); }
    return Promise.resolve(server.get(key));
  };
  const h = form({ storage, api: { me: () => Promise.resolve({ advisorAgreed: { version: terms.version }, taskOwner: owner }), createAdvisorTask } }); await flush();
  const send = async () => { h.page.input({ detail: { value: '合成跨账号企业' } }); h.page.preview(); await flush(); h.page.submit(); await flush(); };
  await send(); assert.match(h.page.data.inputError, /状态尚未确认/);
  owner = B; h.page.onHide(); h.page.onShow(); await flush();
  assert.equal(h.page.data.text, ''); assert.deepEqual(storage.get(idempotency.STORAGE_KEY).others[OWNER].map(e => e.key), [keys[0]]);
  await send(); assert.notEqual(keys[1], keys[0]);
  owner = OWNER; h.page.onHide(); h.page.onShow(); await flush();
  await send();
  assert.equal(keys[2], keys[0]); assert.equal(server.size, 2); assert.equal(h.page.data.accepted, true);
  const saved = storage.get(idempotency.STORAGE_KEY);
  assert.equal(saved.owner, OWNER); assert.equal(saved.entries.length, 0); assert.deepEqual(Object.keys(saved.others), [B]); assert.deepEqual(saved.others[B].map(e => e.key), [keys[1]]);
  assert.doesNotMatch(JSON.stringify([...storage]), /合成跨账号企业/); h.page.onUnload();
});
test('未同意通用协议：工具中心与任务中心写通用工具文案、不提AI，按钮仍走通用同意', async () => {
  const gated = () => Promise.resolve({ data_tools: { enabled: false, features: [], remaining: {} }, ai_advisor: { enabled: false, features: [], remaining: {} }, reason: 'consent' });
  for (const name of ['advisor', 'advisor-tasks']) {
    let asked = 0;
    const page = load(`pages/${name}/index.js`, { '../../utils/api.js': { advisorCapabilities: gated, advisorTasks: () => Promise.resolve({ items: [] }) }, '../../utils/advisor.js': helpers, '../../utils/tool-context.js': context,
      '../../utils/consent.js': { ensureConsent: () => { asked += 1; return Promise.resolve(false); } } }, { wx: { setNavigationBarTitle() {} } });
    if (page.onLoad) page.onLoad({});
    page.onShow(); await flush();
    const shown = textOf(render(`pages/${name}/index.wxml`, page.data));
    assert.match(shown, /使用工具前，请先阅读并同意《用户协议》和《隐私保护指引》/, name); assert.doesNotMatch(shown, /AI/, name);
    // 403 时不知道服务端开没开批量工具：介绍卡只说"有什么"，不承诺同意后一定能用（Codex 审阅）
    if (name === 'advisor') { assert.match(shown, /工具中心有什么/); assert.doesNotMatch(shown, /同意后可以用/); }
    page.consent(); await flush(); assert.equal(asked, 1, name); page.onUnload();
  }
});
test('老用户协议更新后：工具中心和我的记录直接写"协议已更新"，按钮照旧走同意', async () => {
  const gated = () => Promise.resolve({ data_tools: { enabled: false, features: [], remaining: {} }, ai_advisor: { enabled: false, features: [], remaining: {} }, reason: 'consent', consentUpdated: true });
  for (const name of ['advisor', 'advisor-tasks']) {
    let asked = 0;
    const page = load(`pages/${name}/index.js`, { '../../utils/api.js': { advisorCapabilities: gated, advisorTasks: () => Promise.resolve({ items: [] }) }, '../../utils/advisor.js': helpers, '../../utils/tool-context.js': context,
      '../../utils/consent.js': { ensureConsent: () => { asked += 1; return Promise.resolve(false); } } }, { wx: { setNavigationBarTitle() {} } });
    if (page.onLoad) page.onLoad({});
    page.onShow(); await flush();
    const shown = textOf(render(`pages/${name}/index.wxml`, page.data));
    assert.match(shown, /《用户协议》和《隐私保护指引》已更新，请阅读并同意新版后继续使用工具/, name); assert.doesNotMatch(shown, /使用工具前/, name);
    page.consent(); await flush(); assert.equal(asked, 1, name); page.onUnload();
  }
});
