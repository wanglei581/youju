const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
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
    require(name) { if (!Object.hasOwn(deps, name) && /ai-access\.js$/.test(name)) return AI_OFF; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
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
  const navigated = [];
  const toasts = [];
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
    navigateTo: (o) => { navigated.push(o.url); if (options.navigateFails) o.fail(); else o.success(); },
    showToast: (o) => toasts.push(o.title),
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
  return { mod, memory, calls, copied, opened, loading, navigated, toasts, hidden: () => hidden };
}

test('附件直接写能不能读（和阅读页同一个判断，D-097）：能读的写"阅读"可点，读不了的灰色、不能点、写原因；有副本也不算能读', () => {
  const { mod } = setup();
  const list = mod.decorate([
    { ...ATT, readable: true, state: 'ok', stateText: '' },
    { id: '8', preview: true, readable: false, state: 'scan', stateText: '扫描件，暂不能识别文字' },
    { id: '9', readable: true, state: 'ok', stateText: '能读，另有 2 页扫描件暂不能识别文字', scannedPages: 2 },
    // 旧接口没有 readable：能点进阅读页，由阅读页说明
    { id: '10', preview: false, reason: 'pending' },
  ]);
  assert.deepEqual(list.map((a) => [a.canOpen, a.action, a.meta]), [
    [true, '阅读', ''], [false, '', '扫描件，暂不能识别文字'], [true, '阅读', '能读，另有 2 页扫描件暂不能识别文字'], [true, '阅读', ''],
  ]);
  assert.equal(mod.note(list), '附件文字自动提取，未经人工逐字核对，以发文机关公布的文本为准');
  assert.equal(mod.note(list.slice(1, 2)), '', '没有能读的附件不写这句');
  assert.equal(mod.sizeText(2048), '2 KB');
});

test('附件有无二进制副本均进入原生阅读页，不下载、不复制、不打开外部文件', async () => {
  const s = setup();
  for (const att of [ATT, { id: '8', preview: false, reason: 'scan' }]) assert.equal(await s.mod.open(att, '1'), true);
  assert.deepEqual(s.navigated, ['/pages/reader/index?id=1&attachment=7', '/pages/reader/index?id=1&attachment=8']);
  assert.deepEqual(s.calls, []); assert.deepEqual(s.copied, []); assert.deepEqual(s.opened, []);
});

test('原生导航失败和无效文档编号不会转复制链接', async () => {
  const s = setup({ navigateFails: true });
  assert.equal(await s.mod.open(ATT, '1'), false);
  assert.equal(await s.mod.open(ATT, DOC_URL), false);
  assert.deepEqual(s.copied, []); assert.deepEqual(s.opened, []);
  assert.deepEqual(s.toasts, ['阅读页未打开，请重试', '附件信息不完整，请刷新详情']);
});

test('可选下载保留逐段校验和同版本缓存，残缺文件删除', async () => {
  const s = setup();
  const path = await s.mod.download(ATT, () => {});
  assert.equal(s.memory.files.get(path).length, 2500);
  assert.equal(await s.mod.download(ATT, () => {}), path); assert.equal(s.calls.length, 3);
  for (const options of [{ failPart: 1 }, { sizes: [1000, 999, 500] }]) {
    const bad = setup(options);
    await assert.rejects(bad.mod.download(ATT, () => {}));
    assert.equal(bad.memory.files.size, 0); assert.deepEqual(bad.copied, []);
  }
  await assert.rejects(s.mod.download({ ...ATT, parts: 2 }, () => {}));
});

test('详情页：点附件交给附件模块打开，打开过程中重复点击不重复下载', async () => {
  const doc = { id: '1', title: '合成通知', url: DOC_URL, tags: [], attachments: [
    { ...ATT, readable: true, state: 'ok', stateText: '' },
    { id: '8', ext: 'ZIP', name: '表', preview: false, reason: 'format', readable: false, state: 'format', stateText: '此格式暂不能在小程序内阅读' },
    { id: '9', ext: 'DOC', name: '申报书', preview: false, reason: 'pending', readable: true, state: 'ok', stateText: '' },
  ] };
  const opens = [];
  let release;
  const real = setup().mod;
  const page = load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { detail: () => Promise.resolve(JSON.parse(JSON.stringify(doc))) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/repost.js': require('../utils/repost.js'),
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {},
    '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/attachments.js': {
      decorate: real.decorate, note: real.note,
      open: (att, url) => { opens.push([att.id, url]); return new Promise((resolve) => { release = resolve; }); },
    },
  }, { wx: {} });
  page.onLoad({ id: '1' }); await flush();
  assert.deepEqual(page.data.atts.map((a) => a.meta), ['', '此格式暂不能在小程序内阅读', '']);
  assert.equal(page.data.attNote, '附件文字自动提取，未经人工逐字核对，以发文机关公布的文本为准');
  page.openAttachment({ currentTarget: { dataset: { index: 0 } } });
  page.openAttachment({ currentTarget: { dataset: { index: 2 } } });
  assert.deepEqual(opens, [['7', '1']]);
  release(true); await flush();
  // 读不了的不能点（原因已写在附件后面）
  page.openAttachment({ currentTarget: { dataset: { index: 1 } } });
  assert.deepEqual(opens, [['7', '1']]);
  page.openAttachment({ currentTarget: { dataset: { index: 2 } } });
  assert.deepEqual(opens, [['7', '1'], ['9', '1']]);
  // 正文卡顶上一行、两处同名入口：正文没有时有能读的附件叫"阅读附件"，去第一个能读的附件
  assert.equal(page.data.bodySummary, '附件 3 个，2 个能读，1 个格式暂不支持');
  assert.deepEqual(page.data.entry, { label: '阅读附件', attachment: '7' });

  const wxml = read('pages/detail/index.wxml');
  assert.match(wxml, /wx:for="{{atts}}"[^>]*bindtap="openAttachment"/);
  assert.match(wxml, /{{attNote}}/);
  // 附件转发只来自 openDocument 自带的右上角菜单，附件区域不加分享按钮；正式版不出现 AI、智能。
  // 10/18 创始人批准验收问题 9a：整份通知可以从底栏唯一的"转发"按钮转（class bar-share），不是附件的
  const shareButtons = wxml.match(/<button[^>]*open-type="share"[^>]*>/g) || [];
  assert.equal(shareButtons.length, 1); assert.match(shareButtons[0], /class="bar-item bar-share"/);
  for (const source of [wxml.replace(shareButtons[0], ''), read('utils/attachments.js'), read('pages/detail/index.js')]) {
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
  const services = [];
  // 每次调用：[发出时间, 方法, 路径, 服务]；"正在连接…"之类的加载提示
  const calls = [];
  const loading = [];
  let hidden = 0;
  let forgotten = 0;
  const api = load('utils/api.js', {
    './config.js': { cloudEnv: 'prod-test', cloudService: 'api', devBaseUrl: '', templates: {} },
    '../mock/data.js': {}, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  }, {
    Date: clock.Date, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    wx: {
      showLoading: ({ title }) => loading.push(title),
      hideLoading: () => { hidden += 1; },
      cloud: {
        callContainer: (o) => {
          sent.push(clock.now);
          services.push(o.header['X-WX-SERVICE']);
          calls.push([clock.now, o.method, o.path, o.header['X-WX-SERVICE']]);
          const step = plan.shift();
          if (!step) return;
          clock.setTimeout(() => (step.fail
            ? o.fail({ errMsg: step.errMsg || 'cloud.callContainer:fail' })
            : o.success({ statusCode: step.code, data: step.data || (step.code === 200 ? { ok: 1 } : {}) })), step.after);
        },
      },
      removeStorageSync() { forgotten += 1; }, getStorageSync() { return ''; }, setStorageSync() {},
    },
  });
  return { api, clock, sent, services, calls, loading, hidden: () => hidden, forgotten: () => forgotten };
}

// 把假时钟往前走到 until（只触发到那时为止的定时器）
async function advance(clock, until) {
  for (let i = 0; i < 200; i += 1) {
    await flush();
    const due = [...clock.timers.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
    if (!due) break;
    clock.timers.delete(due[0]);
    clock.now = Math.max(clock.now, due[1].at);
    due[1].fn();
  }
  clock.now = Math.max(clock.now, until);
  await flush();
}

async function outcome({ api, clock }, call) {
  let result;
  call(api).then((value) => { result = { value }; }, (error) => { result = { error }; });
  await drive(clock);
  return { ...result, at: clock.now };
}

test('冷启动（10/12）：读数据的请求单次 15 秒超时、平台报超时也接着重试，90 秒内不再发新请求就报错', async () => {
  // 第一次一直不回：15 秒超时后 1.5 秒再试，成功
  let s = retryApi([null, { after: 100, code: 200 }]);
  let r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0));
  assert.deepEqual(r.value, { ok: 1 });
  assert.deepEqual(s.sent, [0, 16500]);
  // 云托管自己报超时的调用失败，同样再试
  s = retryApi([{ after: 14000, fail: true, errMsg: 'cloud.callContainer:fail timeout' }, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0));
  assert.deepEqual(r.value, { ok: 1 });
  assert.deepEqual(s.sent, [0, 15500]);
  // 一直起不来：快失败、网关错误、超时都再试，下一次发出会超过 90 秒时报错
  s = retryApi([{ after: 300, fail: true }, { after: 200, code: 503 }, null]);
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0));
  assert.ok(r.error && r.error.timeout);
  assert.deepEqual(s.sent, [0, 1800, 5000, 25000, 48000, 73000]);
  assert.equal(r.at, 88000);
  // 10/12 实测的冷启动：头两次超时，第三次 503，第 67 秒左右才起来，之后发的请求很快成功——90 秒内等得到
  s = retryApi([null, null, { after: 8000, code: 503 }, null, { after: 300, code: 200 }]);
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0));
  assert.deepEqual(r.value, { ok: 1 });
  assert.deepEqual(s.sent, [0, 16500, 34500, 47500, 70500]);
  assert.equal(r.at, 70800);
  // 附件分段同样处理
  s = retryApi([null, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0));
  assert.deepEqual(r.value, { ok: 1 });
  assert.deepEqual(s.sent, [0, 16500]);
});


test('GET 按1.5、3、5、8秒再试，之后每 10 秒一次，90 秒为止；慢的调用失败也再试；404 等不重试', async () => {
  let s = retryApi([{ after: 200, fail: true }, { after: 300, code: 503 }, { after: 100, code: 200 }]);
  let r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0));
  assert.deepEqual(r.value, { ok: 1 }); assert.deepEqual(s.sent, [0, 1700, 5000]);
  s = retryApi([{ after: 6000, fail: true }, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0)); assert.deepEqual(r.value, { ok: 1 }); assert.deepEqual(s.sent, [0, 7500]);
  s = retryApi([{ after: 4500, code: 504 }, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0)); assert.deepEqual(r.value, { ok: 1 }); assert.deepEqual(s.sent, [0, 6000]);
  s = retryApi([502, 503, 504, 503, 502, 200].map((code) => ({ after: 0, code })));
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0)); assert.deepEqual(r.value, { ok: 1 });
  assert.deepEqual(s.sent, [0, 1500, 4500, 9500, 17500, 27500]);
  s = retryApi(Array.from({ length: 20 }, () => ({ after: 0, code: 503 })));
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0)); assert.equal(r.error.status, 503);
  assert.deepEqual(s.sent, [0, 1500, 4500, 9500, 17500, 27500, 37500, 47500, 57500, 67500, 77500, 87500]); assert.equal(r.at, 87500);
  s = retryApi([{ after: 100, code: 404 }, { after: 100, code: 200 }]);
  r = await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0)); assert.equal(r.error.status, 404); assert.deepEqual(s.sent, [0]);
});

test('冷启动提示首次错误后出现，成功及全失败均消失，隐藏页面解除监听', async () => {
  for (const last of [200, 503]) {
    const s = retryApi([503, 503, 503, 503, last].map((code) => ({ after: 0, code })));
    const seen = []; const page = { setData: (v) => seen.push(v.serviceStarting) };
    s.api.watchStartup(page);
    await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0));
    assert.deepEqual(seen, [false, true, false]);
    s.api.stopStartup(page); s.api.watchStartup({ setData() {} });
    assert.equal(seen.length, 3);
  }
});

test('四页接通启动提示与生命周期，名单查询失败也能重试', () => {
  for (const page of ['home', 'search', 'detail', 'lists']) {
    const js = read(`pages/${page}/index.js`); const wxml = read(`pages/${page}/index.wxml`);
    assert.match(js, /api.watchStartup/); assert.match(js, /api.stopStartup/);
    assert.match(wxml, /正在连接，比平时慢一点，请稍等…/); assert.match(wxml, /serviceStarting/);
  }
  assert.match(read('pages/lists/index.wxml'), /bindtap="query">　重试/);
});

test('冷启动（10/14）：只读接口先走常驻的 admin；admin 拦下、出错、调用失败或超时就改走接口服务', async () => {
  const OK = { after: 50, code: 200 };
  // admin 直接答复：只发一次，走 admin
  let s = retryApi([OK]);
  let r = await outcome(s, (api) => api.coverage());
  assert.deepEqual(r.value, { ok: 1 }); assert.deepEqual(s.services, ['admin']);
  // admin 还没放行这个接口（404 Not Found）、出错（503）、调用失败：马上改走接口服务
  for (const first of [{ after: 30, code: 404, data: { detail: 'Not Found' } }, { after: 30, code: 503 }, { after: 30, fail: true }]) {
    s = retryApi([first, OK]);
    r = await outcome(s, (api) => api.search({ q: '专精特新' }));
    assert.deepEqual(r.value, { ok: 1 }); assert.deepEqual(s.services, ['admin', 'api']);
  }
  // admin 8 秒没回：放弃，改走接口服务
  s = retryApi([null, OK]);
  r = await outcome(s, (api) => api.coverage());
  assert.deepEqual(r.value, { ok: 1 }); assert.deepEqual(s.services, ['admin', 'api']); assert.deepEqual(s.sent, [0, 8000]);
  // 接口自己的答复（文件已下架、请求不对、太频繁）直接报，不再多等接口服务
  for (const [code, detail] of [[404, '该文件已下架'], [422, [{ loc: ['query', 'q'], msg: 'x' }]], [429, '查询太频繁，请稍后再试']]) {
    s = retryApi([{ after: 30, code, data: { detail } }, OK]);
    r = await outcome(s, (api) => api.coverage());
    assert.equal(r.error.status, code); assert.deepEqual(s.services, ['admin']);
  }
  // 改走接口服务后，接口服务冷启动照旧按规则重试
  s = retryApi([{ after: 30, code: 503 }, { after: 0, code: 503 }, OK]);
  r = await outcome(s, (api) => api.coverage());
  assert.deepEqual(r.value, { ok: 1 }); assert.deepEqual(s.services, ['admin', 'api', 'api']);
  // 要身份或按用户计次的（附件分段）只走接口服务
  s = retryApi([OK]);
  await outcome(s, (api) => api.attachmentPart('7', 'abcdef0123456789', 0));
  assert.deepEqual(s.services, ['api']);
});

test('冷启动（10/14，Codex 审查）：admin 8 秒没回就同时问接口服务，admin 迟到但先回也用它，接口服务就不再重试', async () => {
  // admin 第 12 秒才回；接口服务第 8 秒发出后一直不回（冷启动）：第 12 秒拿到 admin 的数据
  let s = retryApi([{ after: 12000, code: 200 }, null]);
  let r = await outcome(s, (api) => api.search({ q: '专精特新' }));
  assert.deepEqual(r.value, { ok: 1 }); assert.deepEqual(s.services, ['admin', 'api']); assert.deepEqual(s.sent, [0, 8000]);
  // 接口服务那次 15 秒超时后不再重试，冷启动提示也不留着
  const seen = []; s = retryApi([{ after: 12000, code: 200 }, null]);
  s.api.watchStartup({ setData: (v) => seen.push(v.serviceStarting) });
  await outcome(s, (api) => api.search({ q: '专精特新' }));
  assert.deepEqual(s.sent, [0, 8000]); assert.equal(seen[seen.length - 1], false);
  // admin 一直不回（30 秒放弃），接口服务第 20 秒醒来：用接口服务的
  s = retryApi([null, { after: 12000, code: 200 }]);
  r = await outcome(s, (api) => api.coverage());
  // 接口服务第 8 秒发出、12 秒后回（第 20 秒拿到）；admin 那次第 30 秒超时，结果已用接口服务的、不再变
  assert.deepEqual(r.value, { ok: 1 }); assert.deepEqual(s.sent, [0, 8000]); assert.deepEqual(s.services, ['admin', 'api']);
});


// 写操作前先叫醒接口服务（10/15 打开登录后）：同意协议、关注、纠错、删除、导出都不自动重试，
// 接口服务没醒时先用 GET /health 叫醒（按读数据的冷启动规则），答复了再发
const health = (at) => [at, 'GET', '/health', 'api'];

test('写操作（10/15）：接口服务没醒先叫醒再提交，等的时候显示"正在连接…"；20 分钟内答复过就直接提交', async () => {
  const plan = [{ after: 0, code: 503 }, { after: 0, code: 503 }, { after: 100, code: 200 }, { after: 50, code: 201, data: { id: 1 } }];
  const s = retryApi(plan);
  let r = await outcome(s, (api) => api.agree('2026-10'));
  assert.deepEqual(r.value, { id: 1 });
  assert.deepEqual(s.calls, [health(0), health(1500), health(4500), [4600, 'POST', '/api/v1/me/agreement', 'api']]);
  assert.deepEqual(s.loading, ['正在连接…']); assert.equal(s.hidden(), 1);
  // 10 分钟后再提交：上次答复还不到 20 分钟，直接发，不再叫醒、不显示提示
  s.calls.length = 0; s.clock.now += 10 * 60 * 1000;
  plan.push({ after: 50, code: 201, data: { id: 2 } });
  r = await outcome(s, (api) => api.feedback(52118, '截止日期写错了'));
  assert.deepEqual(r.value, { id: 2 });
  assert.deepEqual(s.calls.map((c) => [c[1], c[2]]), [['POST', '/api/v1/feedback']]);
  assert.equal(s.loading.length, 1);
  // 又过了 21 分钟（接口服务多半已缩到 0）：先叫醒
  s.calls.length = 0; s.clock.now += 21 * 60 * 1000;
  plan.push({ after: 0, code: 200 }, { after: 0, code: 204 });
  r = await outcome(s, (api) => api.deleteFollow(14));
  assert.deepEqual(s.calls.map((c) => [c[1], c[2]]), [['GET', '/health'], ['DELETE', '/api/v1/me/follows/14']]);
  assert.deepEqual(s.loading, ['正在连接…', '正在连接…']); assert.equal(s.hidden(), 2);
});

test('写操作（10/15）：90 秒叫不醒就不提交，请用户过一分钟再试；/health 答复了别的也算醒了', async () => {
  let s = retryApi(Array.from({ length: 20 }, () => ({ after: 0, code: 503 })));
  let r = await outcome(s, (api) => api.createFollow('专精特新', { kind: '申报通知' }));
  assert.equal(r.error.message, '服务正在启动，请过一分钟再试');
  assert.ok(s.calls.every((c) => c[2] === '/health'));
  assert.deepEqual(s.calls.map((c) => c[0]), [0, 1500, 4500, 9500, 17500, 27500, 37500, 47500, 57500, 67500, 77500, 87500]);
  assert.deepEqual(s.loading, ['正在连接…']); assert.equal(s.hidden(), 1);
  // /health 回 500（如数据库一时连不上）：接口服务本身醒着，照常提交，由提交的结果说话
  s = retryApi([{ after: 0, code: 500 }, { after: 0, code: 201, data: { id: 3 } }]);
  r = await outcome(s, (api) => api.agree('2026-10'));
  assert.deepEqual(r.value, { id: 3 });
  assert.deepEqual(s.calls.map((c) => c[2]), ['/health', '/api/v1/me/agreement']);
  // 提交本身失败照旧不重试
  s = retryApi([{ after: 0, code: 200 }, { after: 0, code: 503 }, { after: 0, code: 201 }]);
  r = await outcome(s, (api) => api.agree('2026-10'));
  assert.equal(r.error.status, 503);
  assert.deepEqual(s.calls.map((c) => c[2]), ['/health', '/api/v1/me/agreement']);
});

test('预热（10/15）：打开时悄悄叫醒、不显示提示；用户提交时赶上预热就跟着等，并从这时起再给足 90 秒', async () => {
  const plan = Array.from({ length: 13 }, () => ({ after: 0, code: 503 }));
  plan.push({ after: 0, code: 200 }, { after: 0, code: 201, data: { id: 4 } });
  const s = retryApi(plan);
  const seen = [];
  s.api.watchStartup({ setData: (v) => seen.push(v.serviceStarting) });
  s.api.prewarm();
  await advance(s.clock, 80000);
  assert.deepEqual(s.calls.map((c) => c[0]), [0, 1500, 4500, 9500, 17500, 27500, 37500, 47500, 57500, 67500, 77500]);
  // 第 80 秒点"同意"：不另起一路，跟着预热等；预热原本第 90 秒就停，现在等到第 170 秒
  const r = await outcome(s, (api) => api.agree('2026-10'));
  assert.deepEqual(r.value, { id: 4 });
  assert.deepEqual(s.calls.slice(11), [health(87500), health(97500), health(107500), [107500, 'POST', '/api/v1/me/agreement', 'api']]);
  assert.deepEqual(s.loading, ['正在连接…']); assert.equal(s.hidden(), 1);
  // 预热不碰页面上的"正在连接"提示
  assert.ok(seen.every((v) => v === false));
  // 已经醒着：再预热不发请求
  const before = s.calls.length;
  s.api.prewarm();
  await drive(s.clock);
  assert.equal(s.calls.length, before);
});

test('预热和写操作（10/15）：admin 的答复不算接口服务醒了；接口服务答复过读请求就算', async () => {
  const plan = [{ after: 0, code: 200 }, { after: 0, code: 200 }, { after: 0, code: 201 }];
  let s = retryApi(plan);
  await outcome(s, (api) => api.coverage());
  await outcome(s, (api) => api.agree('2026-10'));
  assert.deepEqual(s.calls.map((c) => [c[2], c[3]]), [['/api/v1/coverage', 'admin'], ['/health', 'api'], ['/api/v1/me/agreement', 'api']]);
  s = retryApi([{ after: 0, code: 200, data: { agreed: null } }, { after: 0, code: 201 }]);
  await outcome(s, (api) => api.me());
  await outcome(s, (api) => api.agree('2026-10'));
  assert.deepEqual(s.calls.map((c) => c[2]), ['/api/v1/me', '/api/v1/me/agreement']);
  assert.deepEqual(s.loading, []);
});

test('导出（10/15）：先叫醒接口服务，等的时候仍是导出自己的"正在生成…"，不另外弹提示', async () => {
  const s = retryApi([{ after: 0, code: 503 }, { after: 0, code: 200 }, { after: 0, code: 200, data: new ArrayBuffer(4) }]);
  const r = await outcome(s, (api) => api.exportFile([7, 8], 'xlsx'));
  assert.equal(r.value.byteLength, 4);
  assert.deepEqual(s.calls.map((c) => [c[0], c[1], c[2]]), [[0, 'GET', '/health'], [1500, 'GET', '/health'], [1500, 'POST', '/api/v1/exports']]);
  assert.deepEqual(s.loading, []); assert.equal(s.hidden(), 0);
});

test('小程序（10/15）：打开和回到前台时预热接口服务；有新版本时提示重启，选"稍后"不重启', () => {
  let app; let warmed = 0; let ready = null; let applied = 0; let answer = true;
  const modals = [];
  vm.runInNewContext(read('app.js'), {
    App(def) { app = def; },
    require(name) {
      if (name === './utils/config.js') return { cloudEnv: 'prod-test' };
      if (name === './utils/nav.js') return { navMetrics: () => ({ navHeight: 40 }) };
      if (name === './utils/api.js') return { prewarm: () => { warmed += 1; } };
      throw new Error(`没有替身：${name}`);
    },
    wx: {
      cloud: { init() {} },
      getWindowInfo: () => ({}),
      getMenuButtonBoundingClientRect: () => ({}),
      getUpdateManager: () => ({ onUpdateReady: (fn) => { ready = fn; }, applyUpdate: () => { applied += 1; } }),
      showModal: (o) => { modals.push(o); o.success({ confirm: answer }); },
    },
  });
  app.onLaunch();
  assert.equal(app.globalData.navHeight, 40);
  app.onShow();
  app.onShow();
  assert.equal(warmed, 2);
  assert.equal(typeof ready, 'function');
  ready();
  assert.equal(modals[0].confirmText, '重启'); assert.equal(modals[0].cancelText, '稍后');
  assert.equal(applied, 1);
  answer = false;
  ready();
  assert.equal(applied, 1);
});

test('登录后的四页（10/15）：接通"正在连接"提示和生命周期', () => {
  for (const page of ['me', 'follow', 'my-data', 'follow-new']) {
    const js = read(`pages/${page}/index.js`); const wxml = read(`pages/${page}/index.wxml`);
    assert.match(js, /api\.watchStartup\(this\)/, page); assert.match(js, /api\.stopStartup\(this\)/, page);
    assert.match(js, /onHide\(\)/, page); assert.match(js, /onUnload\(\)/, page);
    assert.match(wxml, /正在连接，比平时慢一点，请稍等…/, page); assert.match(wxml, /serviceStarting/, page);
  }
});

test('写操作（10/15，Codex 审查）：别的请求先拿到接口服务的答复，就不再等自己那次 /health；叫醒的也停下', async () => {
  // 预热：第一次 503，第 1.5 秒那次一直不回；第 2 秒用户点同意，同时"我的"页查登录状态，第 3 秒拿到答复
  const s = retryApi([{ after: 0, code: 503 }, null, { after: 1000, code: 200, data: { agreed: null } }, { after: 0, code: 201, data: { id: 5 } }]);
  s.api.prewarm();
  await advance(s.clock, 2000);
  let agreed = null;
  s.api.agree('2026-10').then((v) => { agreed = v; });
  s.api.me();
  await advance(s.clock, 3000);
  assert.deepEqual(agreed, { id: 5 });
  assert.deepEqual(s.calls, [health(0), health(1500), [2000, 'GET', '/api/v1/me', 'api'], [3000, 'POST', '/api/v1/me/agreement', 'api']]);
  // 那次 /health 第 16.5 秒超时后不再重试
  await drive(s.clock);
  assert.equal(s.calls.length, 4);
  assert.deepEqual(s.loading, ['正在连接…']); assert.equal(s.hidden(), 1);
});

test('写操作（10/15，Codex 审查）：几个写操作同时等，只显示一个"正在连接…"，都等完才关', async () => {
  const s = retryApi([{ after: 0, code: 503 }, { after: 0, code: 200 }, { after: 0, code: 201, data: { id: 1 } }, { after: 0, code: 201, data: { id: 2 } }]);
  const results = [];
  s.api.agree('2026-10').then((v) => results.push(v));
  s.api.createFollow('专精特新', { kind: '申报通知' }).then((v) => results.push(v));
  await drive(s.clock);
  assert.deepEqual(results, [{ id: 1 }, { id: 2 }]);
  assert.deepEqual(s.calls.map((c) => [c[0], c[1], c[2]]), [[0, 'GET', '/health'], [1500, 'GET', '/health'], [1500, 'POST', '/api/v1/me/agreement'], [1500, 'POST', '/api/v1/me/follows']]);
  assert.deepEqual(s.loading, ['正在连接…']); assert.equal(s.hidden(), 1);
});

test('后台挂起后超出重试预算，不再发出迟到的GET', async () => {
  const s = retryApi([{ after: 0, code: 503 }, { after: 0, code: 200 }]);
  let result;
  s.api.attachmentPart('7', 'abcdef0123456789', 0).then(
    (value) => { result = { value }; }, (error) => { result = { error }; },
  );
  await advance(s.clock, 0);
  s.clock.now = 120000; // 模拟小程序切后台，重试计时器到恢复前台才执行
  await drive(s.clock);
  assert.ok(result.error);
  assert.equal(result.error.status, 503);
  assert.deepEqual(s.sent, [0]);
});

test('后来的写操作不能无限延长先前用户的等待，超时写操作不会迟到提交', async () => {
  const plan = Array.from({ length: 13 }, () => ({ after: 0, code: 503 }));
  plan.push({ after: 0, code: 200 }, { after: 0, code: 201, data: { id: 2 } });
  const s = retryApi(plan);
  let first; let second;
  s.api.agree('2026-10').then((value) => { first = { value }; }, (error) => { first = { error }; });
  await advance(s.clock, 80000);
  s.api.createFollow('专精特新', { kind: '申报通知' }).then((value) => { second = { value }; }, (error) => { second = { error }; });
  await advance(s.clock, 90000);
  assert.equal(first && first.error && first.error.message, '服务正在启动，请过一分钟再试');
  assert.equal(second, undefined);
  assert.equal(s.hidden(), 0); // 第二个操作仍等待，只保留一个提示
  await drive(s.clock);
  assert.deepEqual(second.value, { id: 2 });
  assert.deepEqual(s.calls.filter((c) => c[1] === 'POST').map((c) => c[2]), ['/api/v1/me/follows']);
  assert.equal(s.hidden(), 1);
});


for (const kind of ['write', 'export', 'attachment']) {
  for (const failure of ['gateway', 'network', 'timeout']) {
    test(`暖态失效：${kind}的${failure}不自动重写，下一次用户操作重新预热`, async () => {
      const step = failure === 'timeout' ? null : failure === 'network' ? { after: 0, fail: true } : { after: 0, code: 503 };
      const plan = [{ after: 0, code: 200 }, step];
      const s = retryApi(plan);
      await outcome(s, (api) => api.me());
      let error;
      const action = kind === 'write' ? () => s.api.agree('2026-10')
        : kind === 'export' ? () => s.api.exportFile(['7'], 'xlsx')
          : () => s.api.attachmentPart('7', 'abcdef0123456789', 0);
      action().catch((err) => { error = err; });
      await advance(s.clock, 0);
      s.clock.now = 120000;
      await drive(s.clock);
      assert.ok(error);
      assert.equal(s.calls.length, 2);
      plan.push({ after: 0, code: 200 }, { after: 0, code: 201 });
      const r = await outcome(s, (api) => api.agree('2026-10'));
      assert.ok(!r.error);
      assert.deepEqual(s.calls.slice(2).map((c) => c[2]), ['/health', '/api/v1/me/agreement']);
    });
  }
}

test('较早请求的失败不覆盖后来收到的成功暖态', async () => {
  const plan = [{ after: 0, code: 200 }, { after: 100, fail: true }, { after: 10, code: 200 }];
  const s = retryApi(plan);
  await outcome(s, (api) => api.me());
  s.api.agree('2026-10').catch(() => {});
  s.api.me().catch(() => {});
  await advance(s.clock, 200);
  plan.push({ after: 0, code: 201 });
  await outcome(s, (api) => api.agree('2026-10'));
  assert.equal(s.calls.filter((c) => c[2] === '/health').length, 0);
});

test('admin公开读取的403不清除本机协议同意，api的403仍清除', async () => {
  const s = retryApi([{ after: 0, code: 403 }, { after: 0, code: 403 }]);
  await outcome(s, (api) => api.coverage());
  assert.equal(s.forgotten(), 0);
  await outcome(s, (api) => api.me());
  assert.equal(s.forgotten(), 1);
});

test('已超时请求的迟到403不清除后来完成的同意记录', async () => {
  const plan = [
    { after: 0, code: 200 }, { after: 16000, code: 403 },
    { after: 0, code: 200 }, { after: 0, code: 201 }, { after: 0, code: 200 },
  ];
  const s = retryApi(plan);
  await outcome(s, (api) => api.me());
  s.api.me().catch(() => {});
  await advance(s.clock, 15500);
  let agreed = false;
  s.api.agree('2026-10').then(() => { agreed = true; });
  await advance(s.clock, 15500);
  assert.equal(agreed, true);
  await advance(s.clock, 16000);
  assert.equal(s.forgotten(), 0);
  await drive(s.clock);
  assert.equal(s.calls.filter((c) => c[1] === 'POST').length, 1);
});
