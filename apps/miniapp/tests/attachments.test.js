const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');

// 仅加载被测代码；依赖都用替身，不访问 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let page;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout, Promise,
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(def) { page = def; }, ...globals,
  }, { filename: file });
  return page ? { ...page, data: JSON.parse(JSON.stringify(page.data)), setData(values) { Object.assign(this.data, values); } } : module.exports;
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');

// 内存里的本机文件系统（只实现用到的同步接口）
function memoryFs() {
  const files = new Map();
  const dirs = new Map();
  let tick = 0;
  return {
    files, dirs,
    statSync(p) {
      if (files.has(p)) return { size: files.get(p).length, lastModifiedTime: 0 };
      if (dirs.has(p)) return { size: 0, lastModifiedTime: dirs.get(p) };
      throw new Error('no such file');
    },
    mkdirSync(p) { tick += 1; dirs.set(p, tick); },
    rmdirSync(p) {
      if (!dirs.has(p)) throw new Error('no such dir');
      dirs.delete(p);
      [...files.keys()].filter((k) => k.startsWith(`${p}/`)).forEach((k) => files.delete(k));
    },
    writeFileSync(p, data) { files.set(p, Buffer.from(data)); },
    appendFileSync(p, data) { files.set(p, Buffer.concat([files.get(p), Buffer.from(data)])); },
    readdirSync(p) {
      return [...dirs.keys()].filter((d) => d.startsWith(`${p}/`) && !d.slice(p.length + 1).includes('/'))
        .map((d) => d.slice(p.length + 1));
    },
  };
}

const ATT = {
  id: '7', ext: 'PDF', name: '附件1：申报指南.pdf', url: 'https://kjt.example.gov.cn/a.pdf',
  preview: true, fileType: 'pdf', size: 2500, version: 'abcdef0123456789', parts: 3, partSize: 1000,
};
const DOC_URL = 'https://kjt.example.gov.cn/notice.html';

function setup(options = {}) {
  const memory = memoryFs();
  const calls = [];
  const copied = [];
  const opened = [];
  const loading = [];
  let hidden = 0;
  const sizes = options.sizes || [1000, 1000, 500];
  const api = {
    attachmentPart(id, version, part) {
      calls.push([id, version, part]);
      if (options.failPart === part) return Promise.reject(new Error('网络不太好'));
      return Promise.resolve(new Uint8Array(sizes[part]).fill(part + 1).buffer);
    },
  };
  const wx = {
    env: { USER_DATA_PATH: '/user' },
    getFileSystemManager: () => memory,
    showLoading: ({ title }) => loading.push(title),
    hideLoading: () => { hidden += 1; },
    openDocument: (o) => { opened.push(o); if (options.openFails) o.fail(); else o.success(); },
  };
  const mod = load('utils/attachments.js', {
    './api.js': api,
    './copy.js': { copyLink: (url, message) => copied.push([url, message]) },
  }, { wx });
  return { mod, memory, calls, copied, opened, loading, hidden: () => hidden };
}

test('附件列表：能打开的显示大小，不能打开的写明原因；有副本时注明以原网站为准', () => {
  const { mod } = setup();
  const list = mod.decorate([
    Object.assign({}, ATT, { size: 1572864 }),
    { id: '8', ext: 'WPS', name: '汇总表', preview: false, reason: 'format' },
    { id: '9', ext: 'PDF', name: '演示附件' },
  ]);
  assert.deepEqual(list.map((a) => [a.canOpen, a.meta, a.action]), [
    [true, '1.5 MB', '打开'],
    [false, '该格式不能在小程序内打开', '复制链接'],
    [false, '附件在原文页面下载', '复制链接'],
  ]);
  assert.equal(mod.sizeText(2048), '2 KB');
  assert.equal(mod.note([ATT]), '附件副本来自原网站，以原网站为准');
  assert.equal(mod.note([{ preview: false }]), '附件在原文页面下载');
  // 提示最多两行：原因加"，已复制原文链接"不超过 24 个字
  for (const text of Object.values(mod.REASONS)) assert.ok(`${text}，已复制原文链接`.length <= 24, text);
});

test('能打开的附件：分段下载、按附件名存到本机、用微信打开并显示右上角菜单，再次打开不重复下载', async () => {
  const s = setup();
  assert.equal(await s.mod.open(ATT, DOC_URL), true);
  assert.deepEqual(s.calls, [['7', ATT.version, 0], ['7', ATT.version, 1], ['7', ATT.version, 2]]);
  const filePath = `/user/attachments/7-${ATT.version}/附件1：申报指南.pdf`;
  assert.deepEqual(s.opened.map((o) => [o.filePath, o.fileType, o.showMenu]), [[filePath, 'pdf', true]]);
  const saved = s.memory.files.get(filePath);
  assert.equal(saved.length, 2500);
  assert.deepEqual([saved[0], saved[1000], saved[2499]], [1, 2, 3]);
  assert.deepEqual(s.loading, ['正在打开附件…', '正在下载 1/3', '正在下载 2/3', '正在下载 3/3']);
  assert.ok(s.hidden() >= 1);
  assert.deepEqual(s.copied, []);

  assert.equal(await s.mod.open(ATT, DOC_URL), true);
  assert.equal(s.calls.length, 3);
  assert.equal(s.opened.length, 2);
});

test('本机只留最近打开的 3 份附件', async () => {
  const s = setup({ sizes: [10] });
  for (let i = 1; i <= 5; i += 1) {
    const att = Object.assign({}, ATT, { id: String(i), size: 10, parts: 1, partSize: 1000 });
    assert.equal(await s.mod.open(att, DOC_URL), true);
  }
  assert.deepEqual([...s.memory.dirs.keys()].map((d) => d.split('/').pop().split('-')[0]).sort(), ['3', '4', '5']);
});

test('不能打开的附件：不下载，复制原文链接并说明原因', async () => {
  const s = setup();
  const att = { id: '8', ext: 'WPS', name: '汇总表', preview: false, reason: 'format' };
  assert.equal(await s.mod.open(att, DOC_URL), false);
  assert.deepEqual(s.copied, [[DOC_URL, '该格式不能在小程序内打开，已复制原文链接']]);
  for (const [reason, text] of [['pending', '附件副本还在准备'], ['personal', '附件含个人信息，请看原文'], ['failed', '附件副本没能从原网站取到']]) {
    await s.mod.open({ preview: false, reason }, DOC_URL);
    assert.deepEqual(s.copied.at(-1), [DOC_URL, `${text}，已复制原文链接`]);
  }
  assert.deepEqual(s.calls, []);
  assert.deepEqual(s.opened, []);
});

test('下载失败、内容不完整、打不开时：删掉半截文件，改为复制原文链接', async () => {
  for (const options of [{ failPart: 1 }, { sizes: [1000, 999, 500] }, { openFails: true }]) {
    const s = setup(options);
    assert.equal(await s.mod.open(ATT, DOC_URL), false);
    assert.deepEqual(s.copied, [[DOC_URL, '附件没能打开，已复制原文链接']]);
    assert.ok(s.hidden() >= 1);
    if (!options.openFails) {
      assert.equal(s.opened.length, 0);
      assert.equal(s.memory.files.size, 0);
    }
  }
  // 附件信息对不上（分段数和大小不符）不下载
  const s = setup();
  assert.equal(await s.mod.open(Object.assign({}, ATT, { parts: 2 }), DOC_URL), false);
  assert.deepEqual(s.calls, []);
});

test('详情页：点附件交给附件模块打开，打开过程中重复点击不重复下载', async () => {
  const doc = { id: '1', title: '合成通知', url: DOC_URL, tags: [], attachments: [ATT, { id: '8', ext: 'WPS', name: '表', preview: false, reason: 'format' }] };
  const opens = [];
  let release;
  const real = setup().mod;
  const page = load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { detail: () => Promise.resolve(JSON.parse(JSON.stringify(doc))) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': {},
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {},
    '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/attachments.js': {
      decorate: real.decorate, note: real.note,
      open: (att, url) => { opens.push([att.id, url]); return new Promise((resolve) => { release = resolve; }); },
    },
  }, { wx: {} });
  page.onLoad({ id: '1' }); await flush();
  assert.deepEqual(page.data.atts.map((a) => a.meta), ['2 KB', '该格式不能在小程序内打开']);
  assert.equal(page.data.attNote, '附件副本来自原网站，以原网站为准');
  page.openAttachment({ currentTarget: { dataset: { index: 0 } } });
  page.openAttachment({ currentTarget: { dataset: { index: 1 } } });
  assert.deepEqual(opens, [['7', DOC_URL]]);
  release(true); await flush();
  page.openAttachment({ currentTarget: { dataset: { index: 1 } } });
  assert.deepEqual(opens, [['7', DOC_URL], ['8', DOC_URL]]);

  const wxml = read('pages/detail/index.wxml');
  assert.match(wxml, /wx:for="{{atts}}"[^>]*bindtap="openAttachment"/);
  assert.match(wxml, /{{attNote}}/);
  // 转发只来自 openDocument 自带的右上角菜单：页面不加分享按钮；正式版不出现 AI、智能
  for (const source of [wxml, read('utils/attachments.js'), read('pages/detail/index.js')]) {
    assert.doesNotMatch(source, /open-type="share"|分享给|智能|\bAI\b/);
  }
});

test('取附件分段：经云托管 GET，按二进制返回；出错时带上状态码', async () => {
  const seen = [];
  let status = 200;
  const api = load('utils/api.js', {
    './config.js': { cloudEnv: 'prod-test', cloudService: 'api', devBaseUrl: '', templates: {} },
    '../mock/data.js': {}, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  }, {
    wx: { cloud: { callContainer: (o) => { seen.push(o); o.success({ statusCode: status, data: new ArrayBuffer(4) }); } } },
  });
  const buffer = await api.attachmentPart('7', 'abcdef0123456789', 2);
  assert.equal(buffer.byteLength, 4);
  assert.equal(seen[0].path, '/api/v1/attachments/7/content?v=abcdef0123456789&part=2');
  assert.equal(seen[0].method, 'GET');
  assert.equal(seen[0].responseType, 'arraybuffer');
  assert.equal(seen[0].header['X-WX-SERVICE'], 'api');
  status = 409;
  await assert.rejects(api.attachmentPart('7', 'abcdef0123456789', 0), (err) => err.status === 409);
});

test('复制原文链接可以带上原因', () => {
  const toasts = [];
  const { copyLink } = load('utils/copy.js', {}, {
    wx: { setClipboardData: (o) => o.success(), showToast: (o) => toasts.push(o.title) },
  });
  copyLink(DOC_URL, '该格式不能在小程序内打开，已复制原文链接');
  copyLink(DOC_URL);
  assert.deepEqual(toasts, ['该格式不能在小程序内打开，已复制原文链接', '已复制，请粘贴到浏览器打开']);
});

test('取附件分段：二进制要设 dataType other；冷启动 503 自动重试', async () => {
  const seen = [];
  const codes = [503, 200];
  const api = load('utils/api.js', {
    './config.js': { cloudEnv: 'prod-test', cloudService: 'api', devBaseUrl: '', templates: {} },
    '../mock/data.js': {}, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  }, {
    wx: { cloud: { callContainer: (o) => { seen.push(o); o.success({ statusCode: codes.shift(), data: new ArrayBuffer(4) }); } } },
  });
  const buffer = await api.attachmentPart('7', 'abcdef0123456789', 0);
  assert.equal(buffer.byteLength, 4);
  assert.equal(seen.length, 2);
  assert.equal(seen[0].dataType, 'other');
  assert.equal(seen[0].responseType, 'arraybuffer');
});

test('查询类请求遇到冷启动 503 自动重试；提交类请求不重试', async () => {
  const seen = [];
  let codes = [503, 503, 200];
  const api = load('utils/api.js', {
    './config.js': { cloudEnv: 'prod-test', cloudService: 'api', devBaseUrl: '', templates: {} },
    '../mock/data.js': {}, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  }, {
    wx: {
      cloud: { callContainer: (o) => { seen.push(o.method); o.success({ statusCode: codes.shift(), data: { ok: 1 } }); } },
      removeStorageSync() {}, getStorageSync() { return ''; }, setStorageSync() {},
    },
  });
  const data = await api.coverage();
  assert.deepEqual(data, { ok: 1 });
  assert.deepEqual(seen, ['GET', 'GET', 'GET']);
  seen.length = 0;
  codes = [503, 200];
  await assert.rejects(api.agree('2026-10'));
  assert.deepEqual(seen, ['POST']);
});

// 假时钟：setTimeout 记下来，由 drive() 按时间先后逐个触发，Date.now() 跟着走
function fakeClock() {
  const clock = { now: 0, timers: new Map(), seq: 0 };
  clock.Date = { now: () => clock.now };
  clock.setTimeout = (fn, ms) => { clock.seq += 1; clock.timers.set(clock.seq, { fn, at: clock.now + ms }); return clock.seq; };
  clock.clearTimeout = (id) => { clock.timers.delete(id); };
  return clock;
}

async function drive(clock) {
  for (let i = 0; i < 100; i += 1) {
    await flush();
    if (!clock.timers.size) return;
    const [id, timer] = [...clock.timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
    clock.timers.delete(id);
    clock.now = Math.max(clock.now, timer.at);
    timer.fn();
  }
}

// 每次调用怎么回：{ after: 毫秒, code } 返回状态码；{ after, fail: true } 调用失败；null 一直不回
function retryApi(plan) {
  const clock = fakeClock();
  const sent = [];
  const api = load('utils/api.js', {
    './config.js': { cloudEnv: 'prod-test', cloudService: 'api', devBaseUrl: '', templates: {} },
    '../mock/data.js': {}, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  }, {
    Date: clock.Date, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    wx: {
      cloud: {
        callContainer: (o) => {
          sent.push(clock.now);
          const step = plan.shift();
          if (!step) return;
          clock.setTimeout(() => (step.fail
            ? o.fail({ errMsg: step.errMsg || 'cloud.callContainer:fail' })
            : o.success({ statusCode: step.code, data: step.code === 200 ? { ok: 1 } : {} })), step.after);
        },
      },
      removeStorageSync() {}, getStorageSync() { return ''; }, setStorageSync() {},
    },
  });
  return { api, clock, sent };
}

async function outcome({ api, clock }, call) {
  let result;
  call(api).then((value) => { result = { value }; }, (error) => { result = { error }; });
  await drive(clock);
  return { ...result, at: clock.now };
}

test('超时（15 秒没返回）不重试：一次查询最多等约 20 秒', async () => {
  // 一直不回：15 秒超时就报错，不再重试
  let s = retryApi([null, { after: 100, code: 200 }]);
  let r = await outcome(s, (api) => api.coverage());
  assert.ok(r.error && r.error.timeout);
  assert.deepEqual(s.sent, [0]);
  assert.equal(r.at, 15000);
  // 云托管自己报超时的调用失败，同样不重试
  s = retryApi([{ after: 14000, fail: true, errMsg: 'cloud.callContainer:fail timeout' }, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.coverage());
  assert.ok(r.error && r.error.timeout);
  assert.deepEqual(s.sent, [0]);
  // 很快失败的重试，最后一次超时：从发出到报错不超过 20 秒
  s = retryApi([{ after: 300, fail: true }, { after: 200, code: 503 }, null]);
  r = await outcome(s, (api) => api.coverage());
  assert.ok(r.error && r.error.timeout);
  assert.deepEqual(s.sent, [0, 1300, 4000]);
  assert.ok(r.at <= 20000, `等了 ${r.at} 毫秒`);
  // 附件分段同样处理
  s = retryApi([null, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0));
  assert.ok(r.error && r.error.timeout);
  assert.deepEqual(s.sent, [0]);
});

test('只重试很快的失败和网关 502/503/504：重试要在第一次发出后 5 秒内开始', async () => {
  // 很快失败（调用失败、网关 503）：重试，成功就返回
  let s = retryApi([{ after: 200, fail: true }, { after: 300, code: 503 }, { after: 100, code: 200 }]);
  let r = await outcome(s, (api) => api.coverage());
  assert.deepEqual(r.value, { ok: 1 });
  assert.deepEqual(s.sent, [0, 1200, 4000]);
  // 慢的失败（发出 6 秒后才失败）：不重试
  s = retryApi([{ after: 6000, fail: true }, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.coverage());
  assert.ok(r.error);
  assert.deepEqual(s.sent, [0]);
  // 网关 504 来得慢（4.5 秒）：再等 1 秒才能重试，超出 5 秒，不重试
  s = retryApi([{ after: 4500, code: 504 }, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.coverage());
  assert.equal(r.error.status, 504);
  assert.deepEqual(s.sent, [0]);
  // 第二次失败时已过了 2.5 秒：再等 2.5 秒就超出 5 秒，不再重试第三次
  s = retryApi([{ after: 500, code: 503 }, { after: 1200, code: 503 }, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.coverage());
  assert.equal(r.error.status, 503);
  assert.deepEqual(s.sent, [0, 1500]);
  // 别的错误（如 404、422）不重试
  s = retryApi([{ after: 100, code: 404 }, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.coverage());
  assert.equal(r.error.status, 404);
  assert.deepEqual(s.sent, [0]);
});
