const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const zlib = require('node:zlib');

function zipEntries(base64) {
  const data = Buffer.from(base64, 'base64'); const out = {};
  let offset = 0;
  while (data.readUInt32LE(offset) === 0x04034b50) {
    const method = data.readUInt16LE(offset + 8); const size = data.readUInt32LE(offset + 18);
    const nameSize = data.readUInt16LE(offset + 26); const extraSize = data.readUInt16LE(offset + 28);
    const name = data.subarray(offset + 30, offset + 30 + nameSize).toString();
    const begin = offset + 30 + nameSize + extraSize; const packed = data.subarray(begin, begin + size);
    out[name] = (method === 8 ? zlib.inflateRawSync(packed) : packed).toString();
    offset = begin + size;
  }
  return out;
}

function page(name, api, helpers, timers, wxOptions = {}) {
  api = { advisorCapabilities: () => Promise.resolve({}), payProducts: () => Promise.resolve({ items: [] }), ...api };
  let definition;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'pages', name, 'index.js'), 'utf8'), {
    require: (module) => module.endsWith('consent.js') ? Object.assign({ loginOpen: () => true }, helpers) : module.endsWith('api.js') ? api : module.endsWith('tool-context.js') ? require('../utils/tool-context.js') : module.endsWith('/pay.js') ? require('../utils/pay.js') : helpers,
    Page: (value) => { definition = value; },
    wx: Object.assign({ setNavigationBarTitle() {}, navigateTo() {}, redirectTo() {} }, wxOptions),
    setTimeout: timers ? timers.set : setTimeout,
    clearTimeout: timers ? timers.clear : clearTimeout,
    Date, Math, Promise,
  });
  definition.setData = (next) => Object.assign(definition.data, next);
  return definition;
}
const flush = async () => { for (let n = 0; n < 8; n += 1) await Promise.resolve(); };

test('演示流程：单独同意、CSV/XLSX 行数、幂等、进度、打开和删除', async () => {
  const original = Date.now; let now = original(); Date.now = () => now;
  const storage = {}; let unpacked = {}; let saved = ''; let opened = false;
  global.wx = {
    getStorageSync: (key) => storage[key], setStorageSync: (key, value) => { storage[key] = value; },
    env: { USER_DATA_PATH: '/demo' },
    base64ToArrayBuffer: (value) => Uint8Array.from(Buffer.from(value, 'base64')).buffer,
    openDocument: (options) => { assert.equal(options.fileType, 'xlsx'); opened = true; options.success(); },
    getFileSystemManager: () => ({
      writeFileSync() {}, unlinkSync() {}, rmdirSync() {},
      unzip: (options) => { options.success(); },
      readFileSync: (file) => { const name = file.slice(file.indexOf('/xl/') + 1); if (!(name in unpacked)) throw new Error(); return unpacked[name]; },
      writeFile: (options) => { saved = options.data; options.success(); },
    }),
  };
  const api = require('../utils/api.js');
  const helpers = require('../utils/advisor.js');
  const config = require('../utils/config.js');
  const originalConfig = { cloudEnv: config.cloudEnv, devBaseUrl: config.devBaseUrl };
  config.cloudEnv = ''; config.devBaseUrl = '';
  try {
    const csv = { kind: 'compare', file: { name: 'clients.xlsx', content_base64: Buffer.from('企业名称\n甲公司\n乙公司').toString('base64') } };
    await assert.rejects(api.advisorPreview(csv), /同意/);
    await api.agree(helpers.VERSION, 'advisor');
    assert.equal(await helpers.consent(), true);
    assert.equal((await api.advisorPreview(csv)).rowCount, 2);
    assert.equal((await api.advisorStatus()).quota.compare.used, 0);
    const first = await api.createAdvisorTask(csv, 'one');
    const retry = await api.createAdvisorTask({ kind: 'compare', rows: ['不同内容'] }, 'one');
    assert.equal(retry.id, first.id);
    assert.equal((await api.advisorStatus()).quota.compare.used, 1);
    await api.cancelAdvisorTask(first.id);
    await api.deleteAdvisorTask(first.id);
    assert.equal((await api.advisorStatus()).quota.compare.used, 0);
    const fixture = require('../mock/advisor-result.js'); unpacked = zipEntries(fixture);
    assert.equal((await api.advisorPreview({ kind: 'compare', file: { name: 'actual.csv', content_base64: fixture } })).rowCount, 5);
    const next = await api.createAdvisorTask({ kind: 'compare', rows: ['甲公司'] }, 'two');
    now += 3100;
    assert.equal((await api.advisorTask(next.id)).status, 'running');
    await assert.rejects(api.cancelAdvisorTask(next.id), /不能取消/);
    now += 3000;
    assert.equal((await api.advisorTask(next.id)).status, 'done');
    await helpers.openResult(next.id);
    assert.ok(opened); assert.equal(Buffer.from(saved).readUInt32LE(0), 0x04034b50);
    assert.equal((await api.myData()).advisorTasks[0].rowCount, 1);
    await api.deleteAdvisorTask(next.id);
    assert.equal((await api.advisorTasks()).items.length, 0);
    assert.equal((await api.advisorStatus()).quota.compare.used, 1);
    const track = { kind: 'track', file: { name: 'profiles.csv', content_base64: Buffer.from('客户名称,省份,关键词,支持方式\n甲公司,辽宁,高新;科技,资金支持').toString('base64') } };
    assert.equal((await api.advisorPreview(track)).rowCount, 1);
    const queued = await api.createAdvisorTask(track, 'track');
    await api.deleteAdvisorTask(queued.id);
    assert.equal((await api.advisorStatus()).quota.track.used, 0);
    assert.equal(helpers.pasteRows('track', '"甲,公司",辽宁,科技,资金支持')[0].name, '甲,公司');
    await api.deleteMe(); assert.equal(await helpers.consent(), false);
  } finally { Date.now = original; Object.assign(config, originalConfig); }
});

test('正式请求支持 202 和 Idempotency-Key，下载重新取地址', async () => {
  let code = 202; let request; const config = require('../utils/config.js');
  const original = { cloudEnv: config.cloudEnv, devBaseUrl: config.devBaseUrl };
  config.cloudEnv = ''; config.devBaseUrl = 'http://test';
  const requests = []; global.wx.request = (options) => { request = options; requests.push(options.url); const part = /\/file\?part=(\d)$/.exec(options.url); options.success({ statusCode: code, data: code === 202 ? { id: 7 } : part ? new Uint8Array(part[1] === '0' ? [1, 2, 3] : [4]).buffer : { size: 4, parts: 2, partSize: 3 } }); };
  const api = require('../utils/api.js');
  try {
    assert.equal((await api.createAdvisorTask({ kind: 'compare', rows: ['甲公司'] }, 'stable-key')).id, 7);
    assert.equal(request.header['Idempotency-Key'], 'stable-key');
    code = 200; let saved = null; let opened = false;
    global.wx.downloadFile = () => assert.fail('结果文件不该让手机直接下载');
    global.wx.openDocument = (options) => { opened = true; assert.equal(options.fileType, 'xlsx'); options.success(); };
    global.wx.getFileSystemManager = () => ({ readFile: (o) => o.success({ data: new ArrayBuffer(4) }), writeFile: (o) => { saved = o.data; o.success(); }, readdirSync: () => [] });
    await require('../utils/advisor.js').openResult(7);
    // 先问大小和段数，再逐段取，拼起来是完整文件；不出现对象存储的地址
    assert.ok(opened); assert.deepEqual(requests.slice(-3).map((u) => u.replace('http://test', '')), ['/api/v1/advisor/tasks/7/download', '/api/v1/advisor/tasks/7/file?part=0', '/api/v1/advisor/tasks/7/file?part=1']);
    assert.deepEqual(Array.from(new Uint8Array(saved)), [1, 2, 3, 4]);
  } finally { Object.assign(config, original); }
});

test('轮询从三秒增至十五秒，隐藏页后忽略在途结果并停止', async () => {
  const delays = []; const callbacks = []; let cleared = 0; let resolveTask;
  const api = { isLive: () => true, advisorTasks: () => Promise.resolve({ items: [{ id: 1, kind: 'compare', status: 'queued' }] }), advisorTask: () => new Promise((resolve) => { resolveTask = resolve; }) };
  const helpers = { taskView: (task) => Object.assign({}, task, { terminal: task.status === 'done' }) };
  const view = page('advisor-tasks', api, helpers, { set: (fn, delay) => { delays.push(delay); callbacks.push(fn); return callbacks.length; }, clear: () => { cleared += 1; } });
  view.onShow(); await flush();
  for (let n = 0; n < 5; n += 1) {
    callbacks[n](); resolveTask({ id: 1, kind: 'compare', status: 'running' }); await flush();
  }
  assert.deepEqual(delays, [3000, 6000, 9000, 12000, 15000, 15000]);
  callbacks[5](); view.onHide(); resolveTask({ id: 1, kind: 'compare', status: 'done' }); await flush();
  assert.ok(cleared); assert.equal(view.data.items[0].status, 'running'); assert.equal(delays.length, 6);
  view.onShow(); await flush(); assert.equal(delays[6], 3000);
  callbacks[6](); resolveTask({ id: 1, kind: 'compare', status: 'done' }); await flush(); assert.equal(delays.length, 7);
});

test('表单重试沿用幂等键，上传预览失败不能提交', async () => {
  const keys = []; const api = { createAdvisorTask: (body, key) => { keys.push(key); return Promise.reject(new Error('超时')); }, advisorPreview: () => Promise.reject(new Error('读取失败')) };
  const helpers = { KINDS: { compare: '批量名单比对' }, pasteRows: (kind, text) => text.split('\n') };
  const view = page('advisor-form', api, helpers);
  view.onLoad({}); view.setData({ agreed: true, text: '甲公司', rowCount: 1 });
  view.submit(); await flush(); view.submit(); await flush(); assert.equal(keys[0], keys[1]);
  view.file = { name: 'a.xlsx' }; view.setData({ mode: 'file', rowCount: 1 });
  view.preview(); await flush(); assert.equal(view.data.rowCount, 0); view.submit(); assert.equal(keys.length, 2);
});


function helpersWith(api, general, terms = { version: 'advisor-test', final: true, text: '工具使用说明正文' }) {
  const context = { module: { exports: {} }, require: (name) => {
    if (name === './api.js') return api;
    if (name === './consent.js') return general;
    if (name === '../content/advisor-terms.js') return terms;
    throw new Error(`不应读取 ${name}`);
  }, Date, Promise, wx: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../utils/advisor.js'), 'utf8'), context);
  return context.module.exports;
}

test('修改文本、文件或工具后换键，同一内容网络失败重试保留键', async () => {
  const calls = []; let selected = { name: 'a.csv', data: 'YQ==' };
  const api = { createAdvisorTask: (body, key) => { calls.push({ body, key }); return Promise.reject(new Error('超时')); }, advisorPreview: () => Promise.resolve({ rowCount: 1 }) };
  const helpers = { KINDS: { compare: '批量名单比对', track: '客户相关通知表' }, pasteRows: (kind, text) => text.split('\n') };
  const view = page('advisor-form', api, helpers, null, {
    chooseMessageFile: (o) => o.success({ tempFiles: [{ name: selected.name, size: 1, path: '/selected' }] }),
    getFileSystemManager: () => ({ readFile: (o) => o.success({ data: selected.data }) }),
  });
  view.onLoad({}); view.setData({ agreed: true });
  view.input({ detail: { value: '甲公司' } }); view.submit(); await flush(); view.submit(); await flush();
  assert.equal(calls[0].key, calls[1].key);
  view.input({ detail: { value: '乙公司' } }); view.submit(); await flush();
  assert.notEqual(calls[1].key, calls[2].key);
  view.input({ detail: { value: '甲公司' } }); view.submit(); await flush();
  assert.notEqual(calls[0].key, calls[3].key);
  view.setData({ kind: 'track' }); view.submit(); await flush(); assert.notEqual(calls[3].key, calls[4].key);
  view.mode({ currentTarget: { dataset: { mode: 'file' } } }); view.choose(); await flush(); view.submit(); await flush();
  view.choose(); await flush(); view.submit(); await flush(); assert.equal(calls[5].key, calls[6].key);
  selected = { name: 'a.csv', data: 'Yg==' }; view.choose(); await flush(); view.submit(); await flush();
  assert.notEqual(calls[6].key, calls[7].key);
  selected = { name: 'b.csv', data: 'Yg==' }; view.choose(); await flush(); view.submit(); await flush();
  assert.notEqual(calls[7].key, calls[8].key);
});

test('通用协议过期时先更新本机状态并引导重新同意', async () => {
  let guided = 0; const records = [];
  const general = { consentEpoch: () => 7, applyServer: (agreed, epoch) => { records.push({ agreed, epoch }); return agreed && agreed.version === 'current'; }, ensureConsent: () => { guided += 1; return Promise.resolve(false); } };
  let profile = { agreed: { version: 'old' }, advisorAgreed: { version: 'advisor-test' } };
  const helpers = helpersWith({ isLive: () => true, me: () => Promise.resolve(profile) }, general);
  assert.equal(await helpers.consent(), false); assert.equal(guided, 1); assert.equal(records[0].epoch, 7);
  profile = { agreed: { version: 'current' }, advisorAgreed: { version: 'advisor-test' } };
  assert.equal(await helpers.consent(), true); assert.equal(guided, 1);
  profile.advisorAgreed.version = 'old-tool'; assert.equal(await helpers.consent(), false);
  profile.agreed = null; assert.equal(await helpers.consent(), false); assert.equal(guided, 2);
});

test('工具关闭、说明占位和加载失败时不允许同意，显示版本', async () => {
  const template = fs.readFileSync(path.join(__dirname, '../pages/advisor-terms/index.wxml'), 'utf8');
  assert.match(template, /工具暂未开放/); assert.match(template, /这个功能暂未开放/); assert.match(template, /当前版本：\{\{version\}\}/);
  assert.match(template, /wx:elif="\{\{!final\}\}"/);
  let posted = 0;
  for (const setting of [
    { enabled: false, final: true }, { enabled: true, final: false },
    { enabled: true, final: true, status: 404 }, { enabled: true, final: true, status: 503 },
  ]) {
    const api = { isLive: () => true, advisorStatus: () => setting.status ? Promise.reject(Object.assign(new Error('不可用'), { status: setting.status })) : Promise.resolve({ enabled: setting.enabled }), agree: () => { posted += 1; return Promise.resolve(); } };
    const helpers = { VERSION: 'advisor-current', TERMS_TEXT: '说明正文', TERMS_FINAL: setting.final, consent: () => Promise.resolve(false) };
    const view = page('advisor-terms', api, helpers);
    view.onShow(); await flush(); view.setData({ checked: true }); view.agree(); await flush();
    assert.equal(view.data.version, 'advisor-current'); assert.equal(posted, 0);
  }
  const helpers = helpersWith({}, {}, { version: 'v1', final: true, text: '【工具使用说明：待律师定稿】' });
  assert.equal(helpers.TERMS_FINAL, false);
  const draft = require('../content/advisor-terms.js'); assert.equal(draft.final, false);
});

test('工具卡片、提交告知、保存期限和两个填写页标题符合审阅口径', async () => {
  const api = { advisorStatus: () => Promise.resolve({ enabled: true, quota: { compare: { used: 0, limit: 3, maxRows: 50 }, track: { used: 0, limit: 1, maxRows: 50 } } }) };
  api.advisorTasks = () => Promise.resolve({ items: [] });
  const view = page('advisor', api, require('../utils/advisor.js')); view.onShow(); await flush();
  assert.equal(view.data.skills[0].description, '在已收录的公示/公布名单里查找客户');
  assert.equal(view.data.skills[1].description, '按客户省份列出近 90 天发布的申报通知；截止日期只写人工核对过的');
  const text = fs.readFileSync(path.join(__dirname, '../pages/advisor-form/index.wxml'), 'utf8');
  assert.ok(text.includes('提交前请确认你有权提交这些客户信息，不要填写个人姓名、身份证号、手机号'));
  assert.ok(text.includes('原文件不保存；客户名单 7 天后清空，结果文件 30 天后删除；可在“我的任务”或“我的数据”中删除（处理中的任务结束后才能删除）'));
  assert.match(text, /advisor-title">\{\{title\}\}/); assert.match(text, /提交任务/);
  const helpers = require('../utils/advisor.js');
  for (const kind of ['compare', 'track']) {
    let title; const form = page('advisor-form', {}, helpers, null, { setNavigationBarTitle: (o) => { title = o.title; } });
    form.onLoad({ kind }); assert.equal(title, helpers.KINDS[kind]); assert.equal(form.data.title, helpers.KINDS[kind]);
  }
});

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) { crc ^= byte; for (let n = 0; n < 8; n += 1) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); }
  return (crc ^ 0xffffffff) >>> 0;
}
function makeZip(files, method = 8, deflateOptions) {
  const locals = []; const centrals = []; let offset = 0;
  for (const [name, text] of Object.entries(files)) {
    const bytes = Buffer.isBuffer(text) ? text : Buffer.from(text); const encoded = Buffer.from(name);
    const packed = method === 8 ? zlib.deflateRawSync(bytes, deflateOptions) : bytes; const crc = crc32(bytes);
    const local = Buffer.alloc(30); local.writeUInt32LE(0x04034b50); local.writeUInt16LE(20, 4); local.writeUInt16LE(method, 8);
    local.writeUInt32LE(crc, 14); local.writeUInt32LE(packed.length, 18); local.writeUInt32LE(bytes.length, 22); local.writeUInt16LE(encoded.length, 26);
    const central = Buffer.alloc(46); central.writeUInt32LE(0x02014b50); central.writeUInt16LE(20, 4); central.writeUInt16LE(20, 6); central.writeUInt16LE(method, 10);
    central.writeUInt32LE(crc, 16); central.writeUInt32LE(packed.length, 20); central.writeUInt32LE(bytes.length, 24); central.writeUInt16LE(encoded.length, 28); central.writeUInt32LE(offset, 42);
    locals.push(local, encoded, packed); centrals.push(central, encoded); offset += local.length + encoded.length + packed.length;
  }
  const directory = Buffer.concat(centrals); const end = Buffer.alloc(22); end.writeUInt32LE(0x06054b50); end.writeUInt16LE(centrals.length / 2, 8); end.writeUInt16LE(centrals.length / 2, 10); end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, directory, end]);
}
function customWorkbook(sheet) {
  const files = zipEntries(require('../mock/advisor-result.js'));
  const old = 'xl/worksheets/sheet1.xml'; const name = 'custom/clients.data';
  files[name] = sheet || files[old]; delete files[old];
  for (const meta of ['xl/_rels/workbook.xml.rels', '[Content_Types].xml']) files[meta] = files[meta].replaceAll(`/${old}`, `/${name}`);
  return files;
}
const sheetXml = (content) => `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${content}</sheetData></worksheet>`;

test('演示解析非标准首表、PK 开头文本、UTF-8/GBK、跨行 CSV 和坏引号', async () => {
  const mock = require('../mock/advisor.js'); await mock.clear(); await mock.agree(require('../utils/advisor.js').VERSION);
  const preview = (bytes) => mock.preview({ kind: 'compare', file: { name: 'a.csv', content_base64: bytes.toString('base64') } });
  assert.equal((await preview(Buffer.from('PK客户\n甲公司'))).rowCount, 2);
  // 企业名称\n甲公司（GBK），不依赖 Node 的 TextDecoder。
  assert.equal((await preview(Buffer.from('c6f3d2b5c3fbb3c60abcd7b9abcbbe', 'hex'))).rowCount, 1);
  assert.equal((await preview(Buffer.from('企业名称\n"甲\n乙公司"'))).rowCount, 1);
  assert.equal(require('../utils/advisor.js').csvRows('企业名称\r\n"甲\r\n乙公司"')[1][0], '甲\r\n乙公司');
  assert.equal((await preview(Buffer.from('甲公司\n\n乙公司'))).rowCount, 3);
  await assert.rejects(preview(Buffer.from('企业名称\n' + '甲公司\n'.repeat(501))), /最多 500 行/);
  await assert.rejects(preview(Buffer.from('企业名称\n"未闭合公司')), /CSV 格式有误/);
  await assert.rejects(preview(Buffer.from('企业名称\n"甲公司"尾巴')), /CSV 格式有误/);
  for (const [method, options] of [[0], [8], [8, { strategy: zlib.constants.Z_FIXED }], [8, { level: 0 }]]) {
    assert.equal((await preview(makeZip(customWorkbook(), method, options))).rowCount, 5);
  }
});

test('演示与线上一致限制工作表行、列、单元格、XML 迭代和解压总量', async () => {
  const mock = require('../mock/advisor.js');
  const preview = (bytes) => mock.preview({ kind: 'compare', file: { name: 'a.xlsx', content_base64: bytes.toString('base64') } });
  const cases = [
    ['<row r="1000000"><c r="A1000000"/></row>', /行号过大/],
    ['<row r="1"><c r="A1000000"/></row>', /行号过大/],
    ['<row r="1"><c r="BM1"/></row>', /列数过多/],
    ['<row r="1"><c r="A1"/></row>'.repeat(503), /行号过大/],
    [`<row r="1">${'<c r="A1"/>'.repeat(32129)}</row>`, /单元格过多/],
    ['<x/>'.repeat(100001), /内容过多/],
  ];
  for (const [payload, reason] of cases) await assert.rejects(preview(makeZip(customWorkbook(sheetXml(payload)))), reason);
  const files = customWorkbook(); files.a = 'x'.repeat(1024 * 1024 + 1); files.b = files.a;
  await assert.rejects(preview(makeZip(files)), /解压后过大/);
  // 伪造中央目录的解压大小：输出过程中就停止，不依赖系统 unzip。
  const forged = makeZip(customWorkbook()); const start = forged.readUInt32LE(forged.length - 6);
  forged.writeUInt32LE(1, start + 24); await assert.rejects(preview(forged), /解压后过大/);
});

test('工具说明定稿并开放后可单独同意当前版本', async () => {
  let submitted;
  const api = { advisorStatus: () => Promise.resolve({ enabled: true }), isLive: () => true, agree: (version, scope) => { submitted = { version, scope }; return Promise.resolve(); } };
  const helpers = { VERSION: 'advisor-current', TERMS_TEXT: '正式正文', TERMS_FINAL: true, consent: () => Promise.resolve(false), ensureConsent: () => Promise.resolve(true) };
  const view = page('advisor-terms', api, helpers, null, { navigateBack() {} });
  view.onShow(); await flush(); view.check({ detail: { value: ['agree'] } }); view.agree(); await flush();
  assert.deepEqual(submitted, { version: 'advisor-current', scope: 'advisor' }); assert.equal(view.data.agreed, true);
});

test('演示单元格上限累计覆盖所有工作表', async () => {
  const files = customWorkbook(sheetXml(`<row r="1">${'<c r="A1"/>'.repeat(16100)}</row>`));
  files['custom/second.data'] = files['custom/clients.data'];
  files['xl/workbook.xml'] = files['xl/workbook.xml'].replace('</sheets>', '<sheet name="第二表" sheetId="2" r:id="rIdSecond"/></sheets>');
  files['xl/_rels/workbook.xml.rels'] = files['xl/_rels/workbook.xml.rels'].replace('</Relationships>', '<Relationship Id="rIdSecond" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="/custom/second.data"/></Relationships>');
  await assert.rejects(require('../mock/advisor.js').preview({ kind: 'compare', file: { name: 'a.xlsx', content_base64: makeZip(files).toString('base64') } }), /单元格过多/);
});

test('修改的 WXML 标签均闭合', () => {
  for (const name of ['advisor-form', 'advisor-terms']) {
    const xml = fs.readFileSync(path.join(__dirname, '..', 'pages', name, 'index.wxml'), 'utf8');
    const tags = xml.replace(/\{\{[\s\S]*?\}\}/g, '').matchAll(/<(\/)?([\w-]+)\b[^>]*?(\/?)>/g); const stack = [];
    for (const tag of tags) {
      if (tag[1]) assert.equal(stack.pop(), tag[2], `${name} 关闭标签 ${tag[2]}`);
      else if (!tag[3]) stack.push(tag[2]);
    }
    assert.equal(stack.length, 0, name);
  }
});

test('有界 ZIP 解压保留二进制成员，并拒绝损坏的校验值', () => {
  const { zipFiles } = require('../utils/advisor-file.js');
  const bytes = Buffer.alloc(6000); let state = 17;
  for (let n = 0; n < bytes.length; n += 1) { state = (Math.imul(state, 1664525) + 1013904223) >>> 0; bytes[n] = state >>> 24; }
  const files = customWorkbook(); files['media/data.bin'] = bytes;
  for (const options of [{ level: 0 }, { level: 1 }, { level: 9 }, { strategy: zlib.constants.Z_FIXED }]) {
    const unpacked = zipFiles(makeZip(files, 8, options)); assert.deepEqual(Buffer.from(unpacked['media/data.bin']), bytes);
  }
  const corrupt = makeZip(files, 0); const end = corrupt.length - 22; const central = corrupt.readUInt32LE(end + 16);
  corrupt.writeUInt32LE((corrupt.readUInt32LE(central + 16) ^ 1) >>> 0, central + 16);
  assert.throws(() => zipFiles(corrupt), /无法读取 XLSX/);
});
