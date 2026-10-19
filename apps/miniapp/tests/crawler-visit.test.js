const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');

// 微信爬虫（搜一搜收录，场景值 1129）打开页面时只问常驻的 admin：不叫醒按需启动的接口服务（叫醒要等一分钟以上、
// 按运行时长计费），admin 没答复也不改走接口服务。用户自己打开（别的场景值）照旧。
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));

function loadApi(scene, reply = (o) => o.success({ statusCode: 200, data: { id: 5, title: '通知' } })) {
  const calls = [];
  const module = { exports: {} };
  const deps = {
    './config.js': { cloudEnv: 'prod-test', cloudService: 'api', devBaseUrl: '', templates: {} },
    '../mock/data.js': {}, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  };
  vm.runInNewContext(read('utils/api.js'), {
    module, console, setTimeout, clearTimeout,
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    wx: {
      getEnterOptionsSync: () => ({ scene, path: 'pages/detail/index', query: { id: '5' } }),
      cloud: { callContainer(o) { calls.push([o.header['X-WX-SERVICE'], o.method, o.path]); reply(o); } },
      showLoading() {}, hideLoading() {}, getStorageSync: () => '', setStorageSync() {}, removeStorageSync() {},
    },
  }, { filename: 'utils/api.js' });
  return { api: module.exports, calls };
}

async function settle(promise) {
  try { return { value: await promise }; } catch (err) { return { err }; }
}

test('爬虫打开通知详情：只问 admin，拿到就用', async () => {
  const { api, calls } = loadApi(1129);
  const r = await settle(api.detail(5));
  assert.equal(r.value.id, 5);
  assert.deepEqual(calls, [['admin', 'GET', '/api/v1/documents/5']]);
});

test('爬虫：admin 出错也不改走接口服务', async () => {
  const { api, calls } = loadApi(1129, (o) => o.success({ statusCode: 502, data: {} }));
  const r = await settle(api.detail(5));
  assert.ok(r.err);
  await flush();
  assert.deepEqual(calls, [['admin', 'GET', '/api/v1/documents/5']]);
});

test('爬虫：预热、登录状态、AI 是否开放、申报季加量都不发给接口服务', async () => {
  const { api, calls } = loadApi(1129);
  api.prewarm();
  const me = await settle(api.me());
  assert.equal(me.err && me.err.crawler, true);
  assert.equal(await api.advisorSeason(), null);
  await settle(api.advisorCapabilities());
  // 写操作也不发（爬虫不会提交，点到了也只提示稍后再试）
  const write = await settle(api.request('/api/v1/me/agreement', {}, { method: 'POST', data: { version: '2026-11' } }));
  assert.ok(write.err);
  await flush();
  assert.deepEqual(calls, []);
});

test('用户自己打开（群聊卡片）照旧：预热叫醒接口服务，详情先问 admin', async () => {
  const { api, calls } = loadApi(1008);
  api.prewarm();
  await settle(api.detail(5));
  await flush();
  assert.deepEqual(calls, [['api', 'GET', '/health'], ['admin', 'GET', '/api/v1/documents/5']]);
});
