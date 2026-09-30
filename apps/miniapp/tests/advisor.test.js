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

function page(name, api, helpers, timers) {
  let definition;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'pages', name, 'index.js'), 'utf8'), {
    require: (module) => module.endsWith('api.js') ? api : helpers,
    Page: (value) => { definition = value; },
    wx: { setNavigationBarTitle() {}, navigateTo() {}, redirectTo() {} },
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
    assert.ok(opened); assert.equal(Buffer.from(saved, 'base64').readUInt32LE(0), 0x04034b50);
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
  global.wx.request = (options) => { request = options; options.success({ statusCode: code, data: code === 202 ? { id: 7 } : { url: 'https://test/result' } }); };
  const api = require('../utils/api.js');
  try {
    assert.equal((await api.createAdvisorTask({ kind: 'compare', rows: ['甲公司'] }, 'stable-key')).id, 7);
    assert.equal(request.header['Idempotency-Key'], 'stable-key');
    code = 200; let downloaded = false; let opened = false;
    global.wx.downloadFile = (options) => { downloaded = true; assert.equal(options.url, 'https://test/result'); options.success({ statusCode: 200, tempFilePath: '/tmp/result' }); };
    global.wx.openDocument = (options) => { opened = true; assert.equal(options.fileType, 'xlsx'); options.success(); };
    await require('../utils/advisor.js').openResult(7);
    assert.ok(downloaded && opened); assert.match(request.url, /\/7\/download$/);
  } finally { Object.assign(config, original); }
});

test('轮询从三秒增至十五秒，隐藏页后忽略在途结果并停止', async () => {
  const delays = []; const callbacks = []; let cleared = 0; let resolveTask;
  const api = { advisorTasks: () => Promise.resolve({ items: [{ id: 1, status: 'queued' }] }), advisorTask: () => new Promise((resolve) => { resolveTask = resolve; }) };
  const helpers = { taskView: (task) => Object.assign({}, task, { terminal: task.status === 'done' }) };
  const view = page('advisor-tasks', api, helpers, { set: (fn, delay) => { delays.push(delay); callbacks.push(fn); return callbacks.length; }, clear: () => { cleared += 1; } });
  view.onShow(); await flush();
  for (let n = 0; n < 5; n += 1) {
    callbacks[n](); resolveTask({ id: 1, status: 'running' }); await flush();
  }
  assert.deepEqual(delays, [3000, 6000, 9000, 12000, 15000, 15000]);
  callbacks[5](); view.onHide(); resolveTask({ id: 1, status: 'done' }); await flush();
  assert.ok(cleared); assert.equal(view.data.items[0].status, 'running'); assert.equal(delays.length, 6);
  view.onShow(); await flush(); assert.equal(delays[6], 3000);
  callbacks[6](); resolveTask({ id: 1, status: 'done' }); await flush(); assert.equal(delays.length, 7);
});

test('表单重试沿用幂等键，上传预览失败不能提交', async () => {
  const keys = []; const api = { createAdvisorTask: (body, key) => { keys.push(key); return Promise.reject(new Error('超时')); }, advisorPreview: () => Promise.reject(new Error('读取失败')) };
  const helpers = { KINDS: { compare: '名单比对' }, pasteRows: (kind, text) => text.split('\n') };
  const view = page('advisor-form', api, helpers);
  view.onLoad({}); view.setData({ agreed: true, text: '甲公司', rowCount: 1 });
  view.submit(); await flush(); view.submit(); await flush(); assert.equal(keys[0], keys[1]);
  view.file = { name: 'a.xlsx' }; view.setData({ mode: 'file', rowCount: 1 });
  view.preview(); await flush(); assert.equal(view.data.rowCount, 0); view.submit(); assert.equal(keys.length, 2);
});
