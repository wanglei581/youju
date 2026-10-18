// 首页提前公布申报季加量的起止日期（D-070 ⑤）：数据来自 /advisor/status 的 season，文字用 utils/boost.js 的 seasonLine()
// （和工具中心、批量工具页同一句）。开关关着、过了结束日、批量工具没开、取不到时首页和原来一样；首页其余内容不等它。
// 页面依赖用替身；boost.js、format.js 用真的。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const boost = require('../utils/boost.js');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
const daywatch = { watchDay() {}, stopDay() {} };
const noCache = { readCache: () => null, writeCache() {}, cachedLabel: () => '' };
// 不提购买（D-070）；也不用容易误会成自动扣费的说法（同 boost-trial.test.js）
const SALES = /购买|买一|开通|付费|价格|元|客户工作台|订阅|试用|扣钱|收费/;

function load(file, deps, globals = {}) {
  let page; const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, setTimeout, clearTimeout, console,
    require(key) { assert.ok(Object.hasOwn(deps, key), `${file}: 缺少依赖 ${key}`); return deps[key]; },
    Page(def) { page = def; }, ...globals,
  }, { filename: file });
  return page ? { ...page, data: plain(page.data), setData(value) { Object.assign(this.data, plain(value)); } } : module.exports;
}

// 10 月 20 日至 31 日：在月底结束，11 月 1 日起回到每月 3 次（不写"当月已用的次数照算"）
const SEASON = (active) => ({ start: '2026-10-20', end: '2026-10-31', active, limit: 5, normalLimit: 3, maxRows: 50 });
const UPCOMING = '申报季加量将于10月20日开始：10月20日至10月31日免费次数每月 5 次（每次仍 50 家以内）';
const ACTIVE = '申报季加量：10月20日至10月31日，免费次数每月 5 次（每次仍 50 家以内）；11月1日起回到每月 3 次';
const HOME = { stats: { week_total: 2, apply: 1, lists: 1, other: 0, provinces: 28, sources: 70, last_checked_at: null }, items: [], dueSoon: [], dueSoonUnverifiedTotal: 0 };

// season：每次取申报季时返回什么；不传表示接口模块里还没有 advisorSeason（就是改之前的首页）
function homePage({ open = true, season, guideClosed = true } = {}) {
  const nav = [];
  const asked = [];
  const api = { home: () => Promise.resolve(plain(HOME)) };
  if (season) api.advisorSeason = () => { asked.push(1); return season(); };
  const wx = { navigateTo: ({ url }) => nav.push(url), getStorageSync: () => (guideClosed ? 1 : ''), setStorageSync() {} };
  const page = load('pages/home/index.js', {
    '../../utils/api.js': api, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/cache.js': noCache, '../../utils/consent.js': { loginOpen: () => open }, '../../utils/boost.js': boost,
  }, { getApp: () => ({ globalData: {} }), wx });
  return Object.assign(page, { nav, asked });
}
const view = (page) => render('pages/home/index.wxml', page.data);
const seasonOf = (page) => findAll(view(page), hasClass('season'));

test('首页：申报季加量开关关着、过了结束日（season 为 null）、批量工具没开（404）、取不到时和原来一样，不报错', async () => {
  const before = homePage();
  before.onLoad(); await flush();
  for (const season of [
    () => Promise.resolve(null),
    () => Promise.reject(Object.assign(new Error('没有找到接口'), { status: 404 })),
    () => Promise.reject(new Error('网络不太好，请稍后再试')),
    () => Promise.resolve({ ...SEASON(true), start: '10-20' }), // 字段不对当没有这项活动
  ]) {
    const page = homePage({ season });
    page.onLoad(); await flush();
    assert.equal(page.asked.length, 1);
    assert.equal(page.data.seasonText, '');
    assert.equal(page.data.error, '');
    assert.equal(seasonOf(page).length, 0);
    assert.deepEqual(view(page), view(before));
  }
});

test('首页：申报季加量还没开始时，入口下面一行写起止日期和每月 5 次（和批量工具页同一句）；点了去工具中心', async () => {
  const page = homePage({ season: () => Promise.resolve(SEASON(false)), guideClosed: false });
  page.onLoad(); await flush();
  assert.equal(page.data.seasonText, UPCOMING);
  assert.equal(page.data.seasonText, boost.seasonLine(SEASON(false)));
  const root = view(page);
  const order = root.children.filter((n) => n.tag).map((n) => String(n.attrs.class || ''));
  const at = (name) => order.findIndex((c) => c.split(/\s+/).includes(name));
  assert.ok(at('entries') >= 0 && at('entries') < at('season') && at('season') < at('guide') && at('guide') < at('summary'), order.join(' | '));
  const [row] = findAll(root, hasClass('season'));
  assert.equal(findAll(row, hasClass('season-title')).map(textOf).join(), '批量处理客户名单（机构用）');
  assert.equal(findAll(row, hasClass('season-text')).map(textOf).join(), UPCOMING);
  assert.doesNotMatch(textOf(row), SALES);
  assert.equal(row.attrs.bindtap, 'goEntry');
  page.goEntry({ currentTarget: { dataset: { url: row.attrs['data-url'] } } });
  assert.deepEqual(page.nav, ['/pages/advisor/index']);
});

test('首页：申报季加量进行中时写起止日期、每月 5 次和之后回到每月 3 次；浅蓝条，不用红色、不加红点、不浮在内容上', async () => {
  const page = homePage({ season: () => Promise.resolve(SEASON(true)) });
  page.onLoad(); await flush();
  assert.equal(page.data.seasonText, ACTIVE);
  const [row] = seasonOf(page);
  assert.equal(findAll(row, hasClass('season-text')).map(textOf).join(), ACTIVE);
  assert.doesNotMatch(textOf(row), SALES);
  assert.equal(findAll(row, (n) => /dot|badge/.test(String(n.attrs.class || ''))).length, 0);
  const css = read('pages/home/index.wxss').split('\n').filter((line) => /^\.season/.test(line)).join('\n');
  assert.ok(css.length > 0);
  assert.doesNotMatch(css, /position|z-index|--red|--orange/);
});

test('首页不等申报季：它一直没回来时其余内容照常显示；先发的慢回来不盖住后发的；协议没定稿的版本不取也不显示', async () => {
  const page = homePage({ season: () => new Promise(() => {}) });
  page.onLoad(); await flush();
  assert.equal(page.data.loaded, true); assert.equal(page.data.loading, false); assert.equal(page.data.stats.week_total, 2);
  assert.equal(page.data.seasonText, ''); assert.equal(seasonOf(page).length, 0);

  const replies = [];
  const racing = homePage({ season: () => new Promise((resolve) => replies.push(resolve)) });
  racing.onLoad(); await flush();
  racing.reload(); await flush(); // 回到首页又刷新了一次
  assert.equal(replies.length, 2);
  replies[1](SEASON(true)); await flush();
  replies[0](null); await flush();
  assert.equal(racing.data.seasonText, ACTIVE);

  const closed = homePage({ open: false, season: () => Promise.resolve(SEASON(true)) });
  closed.onLoad(); await flush();
  assert.equal(closed.asked.length, 0); assert.equal(closed.data.seasonText, ''); assert.equal(seasonOf(closed).length, 0);
});

// ---------- utils/api.js 的 advisorSeason ----------
function apiWith(reply, { live = true } = {}) {
  const calls = [];
  const clock = { now: 1000 };
  const api = load('utils/api.js', {
    './config.js': { cloudEnv: live ? 'prod-test' : '', cloudService: 'api', devBaseUrl: '', templates: {} },
    '../mock/data.js': {}, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  }, {
    Date: { now: () => clock.now },
    // 冷启动重试的间隔改成马上（15 秒、30 秒的超时计时照旧，取到就清掉）
    setTimeout: (fn, ms) => setTimeout(fn, ms >= 15000 ? ms : 0),
    wx: { cloud: { callContainer: (o) => { calls.push([o.method, o.path, o.header['X-WX-SERVICE']]); reply(o); } }, removeStorageSync() {} },
  });
  return { api, calls, clock };
}

test('api.advisorSeason：只要 /advisor/status 里的 season；10 分钟内共用一份；取不到当没有、1 分钟后再取；没连真实服务不取', async () => {
  let reply = (o) => o.success({ statusCode: 200, data: { enabled: true, quota: {}, season: SEASON(false) } });
  const { api, calls, clock } = apiWith((o) => reply(o));
  const first = await api.advisorSeason();
  assert.deepEqual(plain(first), SEASON(false));
  assert.equal(await api.advisorSeason(), first);
  assert.deepEqual(calls, [['GET', '/api/v1/advisor/status', 'api']]);
  // 10 分钟后再取：开关关了（season 为 null）
  clock.now += 10 * 60000; reply = (o) => o.success({ statusCode: 200, data: { enabled: true, season: null } });
  assert.equal(await api.advisorSeason(), null);
  assert.equal(calls.length, 2);
  // 批量工具没开（404）：当没有；1 分钟内不再取，过了再取
  clock.now += 10 * 60000; reply = (o) => o.success({ statusCode: 404, data: { detail: '没有找到接口' } });
  assert.equal(await api.advisorSeason(), null);
  clock.now += 59000;
  assert.equal(await api.advisorSeason(), null);
  assert.equal(calls.length, 3);
  clock.now += 2000; reply = (o) => o.success({ statusCode: 200, data: { season: SEASON(true) } });
  assert.deepEqual(plain(await api.advisorSeason()), SEASON(true));
  assert.equal(calls.length, 4);
  // 演示数据（没配云环境、也没有本机后端）：不发请求，当没有
  const demo = apiWith(() => assert.fail('不该发请求'), { live: false });
  assert.equal(await demo.api.advisorSeason(), null);
  assert.equal(demo.calls.length, 0);
});

test('api.advisorSeason：接口服务冷启动时悄悄重试，不让页面显示"正在连接"（批量工具页的 advisorStatus 照常显示）', async () => {
  const codes = [503, 503, 200];
  const { api, calls } = apiWith((o) => o.success({ statusCode: codes.shift(), data: { season: SEASON(true) } }));
  const seen = [];
  api.watchStartup({ setData: (values) => seen.push(values.serviceStarting) });
  assert.deepEqual(plain(await api.advisorSeason()), SEASON(true));
  assert.equal(calls.length, 3);
  assert.ok(!seen.includes(true), JSON.stringify(seen));
  codes.push(503, 200);
  await api.advisorStatus();
  assert.ok(seen.includes(true), JSON.stringify(seen));
});
