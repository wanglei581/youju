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
