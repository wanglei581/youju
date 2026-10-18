const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 来源统计（utils/entry-stats.js）：冷启动时只在手机上记场景值、来源编号和第一个页面，首屏渲染完 1 秒后
// （取不到渲染信号时打开后 3 秒，切到后台时立刻）发一次给常驻的 admin；不发别的参数和用户编号，不重试，不提示；只统计正式版。
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

// 每个用例一个新模块（模块里记着"这次打开已经记过"）；定时器手动触发
function load({ config = { cloudEnv: 'prod-test', cloudService: 'api' }, env = 'release', wx = {} } = {}) {
  const calls = [];
  const timers = [];
  const fakeWx = Object.assign({
    getAccountInfoSync: () => ({ miniProgram: { envVersion: env } }),
    cloud: { callContainer: (o) => calls.push(['cloud', o]) },
    request: (o) => calls.push(['request', o]),
  }, wx);
  const module = { exports: {} };
  vm.runInNewContext(read('utils/entry-stats.js'), {
    module,
    require(name) { assert.equal(name, './config.js'); return config; },
    wx: fakeWx,
    setTimeout(fn, ms) { timers.push({ fn, ms, cleared: false }); return timers.length; },
    clearTimeout(id) { if (id && timers[id - 1]) timers[id - 1].cleared = true; },
  }, { filename: 'utils/entry-stats.js' });
  const fire = () => timers.filter((t) => !t.cleared).forEach((t) => { t.cleared = true; t.fn(); });
  return { stats: module.exports, calls, timers, fire };
}

const SHARED = { scene: 1008, path: 'pages/detail/index', query: { id: '128', from: 'GZH1103', q: '%E6%9F%90%E6%9F%90%E7%A7%91%E6%8A%80' } };

test('正式版（取不到性能接口）：打开时不发请求，3 秒后发一次给 admin，只有场景值、来源编号和页面', () => {
  const { stats, calls, timers, fire } = load();
  stats.capture(SHARED);
  assert.equal(calls.length, 0);
  assert.deepEqual(timers.map((t) => t.ms), [3000]);
  fire();
  assert.equal(calls.length, 1);
  const [kind, o] = calls[0];
  assert.equal(kind, 'cloud');
  assert.equal(o.path, '/api/v1/entry-stats');
  assert.equal(o.method, 'POST');
  assert.deepEqual(plain(o.config), { env: 'prod-test' });
  assert.deepEqual(plain(o.header), { 'X-WX-SERVICE': 'admin', 'content-type': 'application/json' });
  // 文件编号、检索词这些参数不发；来源编号转成小写
  assert.deepEqual(plain(o.data), { scene: 1008, from: 'gzh1103', page: 'pages/detail/index' });
  assert.equal(o.timeout, 10000);
  // 失败了不重试、不提示
  o.fail({ errMsg: 'cloud.callContainer:fail timeout' });
  fire();
  assert.equal(calls.length, 1);
  // 同一次打开再调也不重复记
  stats.capture(SHARED);
  fire();
  assert.equal(calls.length, 1);
});

test('还没发就切到后台：切走时就发；之后定时器到了不再发第二次', () => {
  const { stats, calls, timers, fire } = load();
  stats.capture({ scene: 1047, path: 'pages/home/index', query: {} });
  stats.flush();
  assert.equal(calls.length, 1);
  assert.equal(timers[0].cleared, true);
  stats.flush();
  fire();
  assert.equal(calls.length, 1);
  assert.deepEqual(plain(calls[0][1].data), { scene: 1047, from: '', page: 'pages/home/index' });
});

// 官方性能接口的替身：记下观察回调，手动送渲染指标
function perf() {
  const state = { callback: null, observed: null, disconnected: 0 };
  return {
    state,
    getPerformance: () => ({
      createObserver(cb) {
        state.callback = cb;
        return { observe(o) { state.observed = plain(o); }, disconnect() { state.disconnected += 1; } };
      },
    }),
    deliver(entries) { state.callback({ getEntries: () => entries }); },
  };
}

test('首屏：等第一个页面的 firstRender，渲染完 1 秒后发；等不到时最晚 10 秒', () => {
  const p = perf();
  const { stats, calls, timers, fire } = load({ wx: { getPerformance: p.getPerformance } });
  stats.capture(SHARED);
  assert.deepEqual(p.state.observed, { entryTypes: ['render'] });
  assert.deepEqual(timers.map((t) => t.ms), [10000]);
  // 别的渲染指标不算
  p.deliver([{ entryType: 'render', name: 'firstPaint' }, { entryType: 'navigation', name: 'appLaunch' }]);
  assert.deepEqual(timers.filter((t) => !t.cleared).map((t) => t.ms), [10000]);
  p.deliver([{ entryType: 'render', name: 'firstRender', path: 'pages/detail/index' }]);
  assert.deepEqual(timers.filter((t) => !t.cleared).map((t) => t.ms), [1000]);
  assert.equal(p.state.disconnected, 1);
  assert.equal(calls.length, 0);
  fire();
  assert.equal(calls.length, 1);
  assert.deepEqual(plain(calls[0][1].data), { scene: 1008, from: 'gzh1103', page: 'pages/detail/index' });
  // 后面页面的渲染不再触发
  p.deliver([{ entryType: 'render', name: 'firstRender', path: 'pages/home/index' }]);
  fire();
  assert.equal(calls.length, 1);
});

test('首屏前就切到后台：切走时发，停止观察，之后的渲染信号不再发', () => {
  const p = perf();
  const { stats, calls, timers, fire } = load({ wx: { getPerformance: p.getPerformance } });
  stats.capture(SHARED);
  stats.flush();
  assert.equal(calls.length, 1);
  assert.equal(p.state.disconnected, 1);
  p.deliver([{ entryType: 'render', name: 'firstRender' }]);
  fire();
  assert.equal(calls.length, 1);
  assert.equal(timers.filter((t) => !t.cleared).length, 0);
});

test('性能接口出错时退回打开后 3 秒', () => {
  const { stats, calls, timers, fire } = load({ wx: { getPerformance() { throw new Error('不支持'); } } });
  stats.capture(SHARED);
  assert.deepEqual(timers.map((t) => t.ms), [3000]);
  fire();
  assert.equal(calls.length, 1);
});

test('开发版、体验版不发；取不到版本时也不发', () => {
  for (const env of ['develop', 'trial']) {
    const { stats, calls, timers } = load({ env });
    stats.capture(SHARED);
    stats.flush();
    assert.equal(calls.length + timers.length, 0, env);
  }
  const broken = load({ wx: { getAccountInfoSync() { throw new Error('不支持'); } } });
  broken.stats.capture(SHARED);
  broken.fire();
  assert.equal(broken.calls.length, 0);
});

test('连本机后端开发时发给本机；演示数据（没配后端）时不发', () => {
  const dev = load({ config: { cloudEnv: '', devBaseUrl: 'http://127.0.0.1:8000' }, env: 'develop' });
  dev.stats.capture({ scene: 1001, path: 'pages/home/index', query: {} });
  dev.fire();
  assert.equal(dev.calls.length, 1);
  assert.equal(dev.calls[0][0], 'request');
  assert.equal(dev.calls[0][1].url, 'http://127.0.0.1:8000/api/v1/entry-stats');
  assert.equal(dev.calls[0][1].method, 'POST');
  const demo = load({ config: { cloudEnv: '', devBaseUrl: '' } });
  demo.stats.capture(SHARED);
  demo.fire();
  assert.equal(demo.calls.length + demo.timers.length, 0);
});

test('来源编号：只认渠道级短编号；小程序码的参数在 scene 里；不合格的当作没带', () => {
  const { stats } = load();
  assert.equal(stats.codeOf({ from: 'gzh1103' }), 'gzh1103');
  assert.equal(stats.codeOf({ from: 'park-qd_2' }), 'park-qd_2');
  assert.equal(stats.codeOf({ scene: 'from%3Dexport%26v%3D2' }), 'export');
  assert.equal(stats.codeOf({ scene: 'v=2&from=gzh1103' }), 'gzh1103');
  assert.equal(stats.codeOf({ from: 'gzh1103', scene: 'from=other' }), 'gzh1103');
  for (const bad of ['张三', 'Zhang San', 'a'.repeat(25), 'gzh%2F1103', '', null, undefined]) {
    assert.equal(stats.codeOf({ from: bad }), '', String(bad));
  }
  assert.equal(stats.codeOf({ scene: 'abc' }), '');
  assert.equal(stats.codeOf(undefined), '');
});

test('页面和场景值：只认 pages/xxx/index 和四位数场景值，其余记成空和 0', () => {
  const { stats } = load();
  assert.equal(stats.pageOf('pages/detail/index'), 'pages/detail/index');
  assert.equal(stats.pageOf('/pages/lists/index'), 'pages/lists/index');
  for (const bad of ['pages/detail/index?id=1', 'pages/../me/index', 'plugin://x/index', '', undefined]) {
    assert.equal(stats.pageOf(bad), '', String(bad));
  }
  assert.equal(stats.sceneOf(1129), 1129);
  assert.equal(stats.sceneOf('1053'), 1053);
  for (const bad of [12, 10000, 1008.5, 'abc', undefined]) assert.equal(stats.sceneOf(bad), 0, String(bad));
  // app.json 里每个页面都认得
  for (const page of JSON.parse(read('app.json')).pages) assert.equal(stats.pageOf(page), page);
});

test('没传启动参数时用 wx.getLaunchOptionsSync；发请求出错也不影响使用', () => {
  const { stats, calls, fire } = load({ wx: { getLaunchOptionsSync: () => ({ scene: 1053, path: 'pages/search/index', query: { q: '高新' } }) } });
  stats.capture();
  fire();
  assert.deepEqual(plain(calls[0][1].data), { scene: 1053, from: '', page: 'pages/search/index' });
  const broken = load({ wx: { cloud: { callContainer() { throw new Error('没初始化'); } } } });
  broken.stats.capture(SHARED);
  assert.doesNotThrow(() => broken.fire());
});

test('app.js：初始化云调用之后记下启动参数，切到后台时补发；打开时不等它', () => {
  const order = [];
  const launch = { scene: 1008, path: 'pages/detail/index', query: { id: '1' } };
  let app;
  vm.runInNewContext(read('app.js'), {
    App(def) { app = def; },
    require(name) {
      if (name === './utils/config.js') return { cloudEnv: 'prod-test' };
      if (name === './utils/nav.js') return { navMetrics: () => ({}) };
      if (name === './utils/api.js') return { prewarm() {} };
      if (name === './utils/entry-stats.js') {
        return { capture: (o) => order.push(['capture', o]), flush: () => order.push(['flush']) };
      }
      throw new Error(`没有替身：${name}`);
    },
    wx: {
      cloud: { init: () => order.push(['init']) },
      getWindowInfo: () => ({}),
      getMenuButtonBoundingClientRect: () => ({}),
    },
  });
  app.onLaunch(launch);
  assert.deepEqual(order.map((x) => x[0]), ['init', 'capture']);
  assert.equal(order[1][1], launch);
  app.onShow();
  app.onHide();
  assert.deepEqual(order.map((x) => x[0]), ['init', 'capture', 'flush']);
});
