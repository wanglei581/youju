// 2.0 自建服务器（config.apiBaseUrl）：wx.request 直连 https 域名、自己的登录凭证；不碰云托管。
// 默认（apiBaseUrl 空）照旧走云托管，见其他测试。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const flush = async (n = 6) => { for (let i = 0; i < n; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
const timers = new Set();
test.afterEach(() => { timers.forEach(clearTimeout); timers.clear(); });
const tracked = (fn, delay) => { const id = setTimeout(() => { timers.delete(id); fn(); }, delay); timers.add(id); return id; };

function harness(config = { apiBaseUrl: 'https://api.example.com', cloudEnv: 'prod-x', cloudService: 'api' }, stored = {}) {
  const requests = []; const logins = []; const cloud = []; const storage = new Map(Object.entries(stored));
  const wx = {
    request(options) { requests.push(options); },
    login(options) { logins.push(options); },
    cloud: { callContainer(options) { cloud.push(options); } },
    getStorageSync: (key) => storage.get(key),
    setStorageSync: (key, value) => storage.set(key, value),
    removeStorageSync: (key) => storage.delete(key),
    showLoading() {}, hideLoading() {},
  };
  const module = { exports: {} };
  vm.runInNewContext(read('utils/api.js'), {
    module, Date, Math, Promise, setTimeout: tracked, clearTimeout,
    require(name) {
      if (name === './config.js') return config;
      if (name === '../mock/data.js' || name === './mockSearch.js' || name === './format.js') return {};
      throw new Error(`未提供依赖 ${name}`);
    },
    wx,
  }, { filename: 'utils/api.js' });
  return { api: module.exports, requests, logins, cloud, storage };
}

const ok = (data, statusCode = 200) => ({ statusCode, data });

test('首次请求先登录，之后每个请求都带凭证；不走云托管', async () => {
  const h = harness();
  const result = h.api.request('/api/v1/home');
  await flush();
  assert.equal(h.logins.length, 1);
  h.logins[0].success({ code: 'c1' });
  await flush();
  const login = h.requests.shift();
  assert.equal(login.url, 'https://api.example.com/api/v1/auth/login');
  assert.equal(login.method, 'POST');
  // vm 里建的对象原型不同，比较前转成普通对象
  assert.deepEqual(JSON.parse(JSON.stringify(login.data)), { code: 'c1' });
  assert.equal(login.header.Authorization, undefined);
  login.success(ok({ token: 'ys1.t1', expiresIn: 604800 }));
  await flush();
  const home = h.requests.shift();
  assert.equal(home.url, 'https://api.example.com/api/v1/home');
  assert.equal(home.header.Authorization, 'Bearer ys1.t1');
  home.success(ok({ items: [1] }));
  assert.deepEqual(await result, { items: [1] });
  assert.equal(h.cloud.length, 0);
  // 凭证存在本地缓存里，下一个请求不再登录
  assert.equal(h.storage.get('youjuSession').token, 'ys1.t1');
  h.api.request('/api/v1/me');
  await flush();
  assert.equal(h.logins.length, 1);
  assert.equal(h.requests[0].header.Authorization, 'Bearer ys1.t1');
});

test('几个请求一起等同一次登录', async () => {
  const h = harness();
  h.api.request('/api/v1/home');
  h.api.request('/api/v1/coverage');
  h.api.request('/api/v1/calendar');
  await flush();
  assert.equal(h.logins.length, 1);
  h.logins[0].success({ code: 'c' });
  await flush();
  h.requests.shift().success(ok({ token: 'ys1.t', expiresIn: 3600 }));
  await flush();
  assert.equal(h.requests.length, 3);
  assert.ok(h.requests.every((r) => r.header.Authorization === 'Bearer ys1.t'));
});

test('凭证过期（服务器回 401）：重新登录、重发一次', async () => {
  const h = harness(undefined, { youjuSession: { token: 'ys1.old', expiresAt: Date.now() + 3600e3 } });
  const result = h.api.request('/api/v1/me');
  await flush();
  assert.equal(h.logins.length, 0);
  h.requests.shift().success(ok({ detail: '请在微信小程序里使用' }, 401));
  await flush();
  assert.equal(h.storage.get('youjuSession'), undefined);
  h.logins[0].success({ code: 'c2' });
  await flush();
  h.requests.shift().success(ok({ token: 'ys1.new', expiresIn: 3600 }));
  await flush();
  const retry = h.requests.shift();
  assert.equal(retry.header.Authorization, 'Bearer ys1.new');
  retry.success(ok({ agreed: null }));
  assert.deepEqual(await result, { agreed: null });
});

test('只重发一次：重新登录后还是 401，就照常报错', async () => {
  const h = harness(undefined, { youjuSession: { token: 'ys1.old', expiresAt: Date.now() + 3600e3 } });
  const result = h.api.request('/api/v1/me');
  await flush();
  h.requests.shift().success(ok({ detail: 'x' }, 401));
  await flush();
  h.logins[0].success({ code: 'c' });
  await flush();
  h.requests.shift().success(ok({ token: 'ys1.n', expiresIn: 3600 }));
  await flush();
  h.requests.shift().success(ok({ detail: '请在微信小程序里使用' }, 401));
  await assert.rejects(result, (err) => err.status === 401 && err.message === '请在微信小程序里使用');
});

test('登录失败也照样发（不要身份的接口照常能用）', async () => {
  const h = harness();
  const result = h.api.request('/api/v1/home');
  await flush();
  h.logins[0].fail({ errMsg: 'login:fail' });
  await flush();
  const home = h.requests.shift();
  assert.equal(home.header.Authorization, undefined);
  home.success(ok({ items: [] }));
  assert.deepEqual(await result, { items: [] });
});

test('只读请求不先问 admin、写操作不先叫醒：一次就发', async () => {
  const h = harness(undefined, { youjuSession: { token: 'ys1.t', expiresAt: Date.now() + 3600e3 } });
  h.api.request('/api/v1/search', { q: '高企' });
  h.api.request('/api/v1/feedback', null, { method: 'POST', data: { content: 'x' } });
  await flush();
  assert.deepEqual(h.requests.map((r) => [r.method, r.url]), [
    ['GET', 'https://api.example.com/api/v1/search?q=%E9%AB%98%E4%BC%81'],
    ['POST', 'https://api.example.com/api/v1/feedback'],
  ]);
  assert.ok(h.requests.every((r) => !('X-WX-SERVICE' in r.header)));
});

test('导出和附件分段走 wx.request，二进制、带凭证', async () => {
  const h = harness(undefined, { youjuSession: { token: 'ys1.t', expiresAt: Date.now() + 3600e3 } });
  const file = h.api.exportFile(['1'], 'xlsx');
  const part = h.api.attachmentPart(7, 'v1', 0);
  await flush();
  const [exp, att] = h.requests;
  assert.equal(exp.url, 'https://api.example.com/api/v1/exports');
  assert.equal(exp.responseType, 'arraybuffer');
  assert.equal(exp.dataType, 'other');
  assert.equal(exp.header.Authorization, 'Bearer ys1.t');
  assert.equal(att.url, 'https://api.example.com/api/v1/attachments/7/content?v=v1&part=0');
  assert.equal(att.responseType, 'arraybuffer');
  const bytes = new ArrayBuffer(3);
  exp.success({ statusCode: 200, data: bytes });
  att.success({ statusCode: 200, data: bytes });
  assert.equal(await file, bytes);
  assert.equal(await part, bytes);
});

test('打开小程序时悄悄登录好（prewarm）', async () => {
  const h = harness();
  h.api.prewarm();
  await flush();
  assert.equal(h.logins.length, 1);
  assert.equal(h.requests.length, 0);
});

test('不填 apiBaseUrl：照旧走云托管（callContainer），不登录', async () => {
  const h = harness({ cloudEnv: 'prod-x', cloudService: 'api', apiBaseUrl: '' });
  h.api.request('/api/v1/me');
  await flush();
  assert.equal(h.logins.length, 0);
  assert.equal(h.requests.length, 0);
  // 写之前先叫醒（GET /health），都走 callContainer
  assert.ok(h.cloud.length >= 1);
  assert.equal(h.cloud[0].config.env, 'prod-x');
});

test('配置文件：默认不填 apiBaseUrl；app.js 填了就不初始化云托管', () => {
  const config = require('../utils/config.js');
  assert.equal(config.apiBaseUrl, '');
  assert.match(read('app.js'), /!config\.apiBaseUrl && config\.cloudEnv && wx\.cloud/);
});

test('几个请求同时拿旧凭证回 401：后回来的不会把刚换好的新凭证清掉', async () => {
  const h = harness(undefined, { youjuSession: { token: 'ys1.old', expiresAt: Date.now() + 3600e3 } });
  h.api.request('/api/v1/me');
  h.api.request('/api/v1/me/follows');
  await flush();
  const [a, b] = h.requests.splice(0, 2);
  a.success(ok({ detail: 'x' }, 401));
  await flush();
  h.logins[0].success({ code: 'c' });
  await flush();
  h.requests.shift().success(ok({ token: 'ys1.new', expiresIn: 3600 }));
  await flush();
  assert.equal(h.storage.get('youjuSession').token, 'ys1.new');
  b.success(ok({ detail: 'x' }, 401));
  await flush();
  // 新凭证还在，B 直接用它重发，不再登录一次
  assert.equal(h.storage.get('youjuSession').token, 'ys1.new');
  assert.equal(h.logins.length, 1);
  assert.ok(h.requests.every((r) => r.header.Authorization === 'Bearer ys1.new'));
});
