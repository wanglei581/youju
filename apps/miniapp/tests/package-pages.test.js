// 包里有没有某个页面（utils/pages.js）：过审版按首发 16 页打包（D-091，tools/miniapp_pack.js），
// 不带工具中心、我的记录、购买、订单、使用帮助和 AI 页面；首发包里的页面上，指向这些页面的入口都不显示，
// 点了也不跳转。完整版（仓库里的 app.json）照旧。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const AI_OFF = require('./helpers/ai-off.js');
const format = require('../utils/format.js');
const regions = require('../utils/regions.js');
const boost = require('../utils/boost.js');
const { render, textOf, findAll, hasClass, tapsTo } = require('./wxml-render.js');
const { FIRST_PAGES } = require('../../../tools/miniapp_presubmit_check.js');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (v) => JSON.parse(JSON.stringify(v));
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
const APP_PAGES = JSON.parse(read('app.json')).pages;

function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, setTimeout, clearTimeout, console, Date, Promise,
    require(name) {
      if (Object.hasOwn(deps, name)) return deps[name];
      if (/ai-access\.js$/.test(name)) return AI_OFF;
      assert.fail(`${file} 缺少依赖 ${name}`);
    },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  if (!def) return module.exports;
  return { ...def, ...(def.methods || {}), data: plain(def.data || {}), setData(values) { Object.assign(this.data, plain(values)); } };
}

// 真的 utils/pages.js，页面清单换成指定的
const pagesOf = (list) => load('utils/pages.js', { './package-pages.js': list });
const FIRST = pagesOf(FIRST_PAGES);
const FULL = pagesOf(APP_PAGES);
const inPackage = (list) => (url) => list.includes(String(url).replace(/^\//, '').split('?')[0]);
const urlsIn = (root) => findAll(root, (n) => n.attrs['data-url'] !== undefined).map((n) => n.attrs['data-url']);

test('utils/package-pages.js 和 app.json 的页面一样（打过审版时由 tools/miniapp_pack.js 重写）', () => {
  assert.deepEqual(require('../utils/package-pages.js'), APP_PAGES);
  assert.equal(FIRST_PAGES.length, 16);
  assert.deepEqual(plain(FULL.flags()), { advisor: true, records: true, pass: true, orders: true, help: true, ai: true });
  assert.deepEqual(plain(FIRST.flags()), { advisor: false, records: false, pass: false, orders: false, help: false, ai: false });
  assert.equal(FIRST.has('my-data'), true); assert.equal(FIRST.has('lists'), true); assert.equal(FIRST.has('advisor-tasks'), false);
});

function mePage(pages, server) {
  const urls = [];
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: true, version: '2026-11' },
    '../../utils/consent.js': { loginOpen: () => true, consentEpoch: () => 0, applyServer: () => true, withdraw: () => Promise.resolve(false), markAgreed() {}, openPrivacy() {}, CONSENT_PAGE: '', agreeFailed() {}, featureNames: () => '' },
    '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null), notCoveredNames: () => '' },
    '../../utils/pages.js': pages,
    '../../utils/pay.js': require('../utils/pay.js'),
    '../../utils/api.js': { isLive: () => true, me: () => Promise.resolve(plain(server)), payProducts: () => Promise.resolve({ items: [] }), advisorStatus: () => Promise.reject(new Error('没有使用期')) },
  }, { wx: { navigateTo: ({ url }) => urls.push(url), switchTab: ({ url }) => urls.push(url), showToast() {} }, getApp: () => ({ globalData: {} }) });
  return { page, urls };
}

test('我的页（过审版）：宫格三格（没有工具中心），没有"我的记录""使用帮助"；在卖、有订单、在使用期也不给购买、订单入口和标识', async () => {
  // 服务端说在卖、有订单、使用期内：完整版会显示这些入口；首发包里没有这些页面
  const server = { agreed: { version: '2026-11' }, current: true, follows: 0, subscriptions: {}, pay: { enabled: true, hasOrders: true }, pass: { active: true, expiresAt: '2026-11-30T23:59:59+08:00' } };
  const first = mePage(FIRST, server);
  first.page.refresh(); await flush();
  assert.equal(first.page.data.gridCols, 3);
  assert.equal(first.page.data.passEntry, false); assert.equal(first.page.data.ordersEntry, false); assert.equal(first.page.data.passBadge, '');
  const root = render('pages/me/index.wxml', first.page.data);
  const [grid] = findAll(root, hasClass('grid'));
  assert.ok(hasClass('grid--3')(grid));
  assert.deepEqual(findAll(grid, hasClass('grid-item')).map((n) => textOf(n).trim()), ['导出记录', '我的客户', '纠错']);
  const text = textOf(root);
  for (const word of ['工具中心', '我的记录', '使用帮助', '批量工具', '我的订单']) assert.ok(!text.includes(word), `还有：${word}`);
  for (const url of urlsIn(root)) assert.ok(inPackage(FIRST_PAGES)(url), `指向包外页面：${url}`);
  first.page.openPass();
  assert.deepEqual(first.urls, []);
  assert.match(read('pages/me/index.wxss'), /\.grid--3 \{ grid-template-columns: repeat\(3, 1fr\); \}/);

  // 完整版照旧：四格、我的记录、使用帮助、批量工具、我的订单都在
  const full = mePage(FULL, server);
  full.page.refresh(); await flush();
  assert.equal(full.page.data.gridCols, 4);
  assert.equal(full.page.data.passEntry, true); assert.equal(full.page.data.ordersEntry, true); assert.notEqual(full.page.data.passBadge, '');
  const fullText = textOf(render('pages/me/index.wxml', full.page.data));
  for (const word of ['工具中心', '我的记录', '使用帮助', '批量工具', '我的订单']) assert.ok(fullText.includes(word), `完整版少了：${word}`);
});

function homePage(pages) {
  const nav = []; let seasonAsked = 0;
  const page = load('pages/home/index.js', {
    '../../utils/api.js': { home: () => new Promise(() => {}), advisorSeason: () => { seasonAsked += 1; return Promise.resolve({ start: '2026-10-20', end: '2026-10-31', active: true, limit: 5, normalLimit: 3, maxRows: 50 }); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/boost.js': boost, '../../utils/pages.js': pages,
  }, { getApp: () => ({ globalData: {} }), wx: { navigateTo: ({ url }) => nav.push(url), getStorageSync: () => '', setStorageSync() {} } });
  return { page, nav, seasonAsked: () => seasonAsked };
}

test('首页（过审版）：没有"工具中心"入口、申报季加量一行，引导第 3 步写"我的客户"；完整版照旧', async () => {
  const first = homePage(FIRST);
  first.page.onLoad(); await flush();
  assert.equal(first.page.data.tools, false);
  assert.equal(first.seasonAsked(), 0);
  assert.deepEqual(first.page.data.guideSteps.map((s) => s.key), ['search', 'due', 'clients']);
  const root = render('pages/home/index.wxml', first.page.data);
  const [entries] = findAll(root, hasClass('entries'));
  assert.ok(!hasClass('entries--tools')(entries));
  assert.deepEqual(findAll(entries, hasClass('entry-name')).map((n) => textOf(n)), ['截止', '我的客户', '查名单']);
  assert.ok(!textOf(root).includes('工具中心'));
  for (const url of urlsIn(root)) assert.ok(inPackage(FIRST_PAGES)(url), `指向包外页面：${url}`);

  const full = homePage(FULL);
  full.page.onLoad(); await flush();
  assert.equal(full.page.data.tools, true);
  assert.equal(full.seasonAsked(), 1);
  assert.deepEqual(full.page.data.guideSteps.map((s) => s.key), ['search', 'due', 'tools']);
  assert.ok(textOf(render('pages/home/index.wxml', full.page.data)).includes('工具中心'));
});

test('我的客户（过审版）：顶部没有"打开工具中心"，点了也不跳；完整版照旧', () => {
  for (const [pages, shown] of [[FIRST, false], [FULL, true]]) {
    const nav = [];
    const page = load('pages/clients/index.js', {
      '../../utils/regions.js': regions, '../../utils/api.js': {}, '../../utils/clients.js': {},
      '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null) }, '../../utils/exporter.js': { previewDocs: () => Promise.resolve(true) },
      '../../utils/format.js': format, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} }, '../../utils/pages.js': pages,
    }, { wx: { navigateTo: ({ url }) => nav.push(url) } });
    assert.equal(page.data.tools, shown);
    const root = render('pages/clients/index.wxml', { ...page.data, editing: false });
    assert.equal(textOf(root).includes('打开工具中心'), shown);
    // "客户卡片"那一栏顶上的说明：没有工具中心就不提它
    const cards = render('pages/clients/index.wxml', { ...page.data, editing: false, tab: 'cards', clients: [] });
    assert.match(textOf(cards), /保存客户的地区和产业方向，最多 20 家。/);
    assert.equal(textOf(cards).includes('工具中心'), shown);
    page.tools();
    assert.deepEqual(nav, shown ? ['/pages/advisor/index'] : []);
  }
});

test('联系客服（过审版）：鸿蒙手机打不开客服时不给"看使用帮助"，改成去"关于有据"看联系邮箱；完整版照旧给使用帮助', () => {
  for (const [pages, help] of [[FIRST, false], [FULL, true]]) {
    for (const variant of ['row', 'block']) {
      const urls = [];
      const view = load('components/contact-button/index.js', { '../../content/legal.js': { operator: { email: 'help@example.invalid' } }, '../../utils/pages.js': pages },
        { wx: { getDeviceInfo: () => ({ platform: 'ohos' }), navigateTo: ({ url }) => urls.push(url) } });
      Object.assign(view.properties, { from: 'me', docId: '', docTitle: '', reason: '', variant });
      view.lifetimes.attached.call(view);
      const root = render('components/contact-button/index.wxml', { ...view.data, ...view.properties });
      const text = textOf(root);
      assert.equal(findAll(root, tapsTo('help')).length > 0, help, variant);
      assert.equal(text.includes('使用帮助'), help, variant);
      if (variant === 'block') assert.ok(text.includes('help@example.invalid'));
      // "我的"页那一行：没有使用帮助时点进"关于有据"（那里有联系邮箱，点一下能复制）
      if (variant === 'row' && !help) {
        assert.match(text, /这台手机打不开微信客服，看联系邮箱/);
        assert.equal(findAll(root, tapsTo('about')).length, 1);
        view.about();
        assert.deepEqual(urls, ['/pages/about/index']);
        assert.ok(FIRST_PAGES.includes('pages/about/index'));
        continue;
      }
      view.help();
      assert.deepEqual(urls, help ? ['/pages/help/index'] : []);
    }
  }
});

function accessIn(pages, saved) {
  let asked = 0;
  const api = { isLive: () => true, advisorMode: () => { asked += 1; return Promise.resolve({ open: true, available: true }); }, advisorCapabilities: () => { asked += 1; return Promise.resolve({ ai_advisor: { enabled: true } }); }, forgetCapabilities() {} };
  const access = load('utils/ai-access.js', { './api.js': api, './pages.js': pages },
    { wx: { getStorageSync: (key) => saved.get(key), setStorageSync: (key, value) => saved.set(key, value), removeStorageSync: (key) => saved.delete(key) } });
  return { access, asked: () => asked };
}

test('AI（过审版）：包里没有 AI 页面时对谁都不显示，白名单里的人、本机记着"上次显示"也一样，不去问后台；底栏没有 AI 一格', async () => {
  const saved = new Map([['youjuAiVisibleV1', { at: Date.now() - 1000 }]]);
  const first = accessIn(FIRST, saved);
  assert.equal(first.access.current().visible, false);
  const state = await first.access.refresh(true);
  assert.equal(state.visible, false);
  assert.equal(first.asked(), 0);
  // 完整版：照旧问后台，对所有人开放时显示
  const full = accessIn(FULL, new Map());
  assert.equal((await full.access.refresh(true)).visible, true);
  assert.ok(full.asked() > 0);

  const visible = { current: () => ({ visible: true, confirmed: true, available: true }), subscribe: () => () => {}, ensure: () => Promise.resolve() };
  for (const [pages, tabs] of [[FIRST, ['首页', '关注', '文件', '我的']], [FULL, ['首页', '关注', 'AI', '文件', '我的']]]) {
    const bar = load('custom-tab-bar/index.js', { '../utils/consent.js': { loginOpen: () => true }, '../utils/ai-access.js': visible, '../utils/pages.js': pages }, { wx: {} });
    bar.lifetimes.attached.call(bar);
    assert.deepEqual(bar.data.list.map((t) => t.text), tabs);
  }
});

// 首发包里的页面、组件、工具函数写到包外页面的地方，必须用 utils/pages.js 判断过（或只被 AI 入口用，
// AI 入口在包里没有 AI 页面时整个不显示）。新加的入口没判断时这里会报出来
test('首发包里写到包外页面的文件都用 utils/pages.js 判断过', () => {
  const firstNames = FIRST_PAGES.map((p) => p.split('/')[1]);
  const files = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
      const rel = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(rel);
      else if (/\.(js|wxml)$/.test(entry.name)) files.push(rel);
    }
  };
  for (const name of firstNames) walk(`pages/${name}`);
  ['components', 'utils', 'custom-tab-bar'].forEach(walk);
  files.push('app.js');
  // 只被 AI 入口用的：AI 入口靠 utils/ai-access.js（包里没有 AI 页面时不显示）
  const AI_ONLY = new Set(['utils/tool-context.js', 'utils/ai-access.js']);
  const offenders = [];
  for (const file of files) {
    const source = read(file);
    const outside = [...source.matchAll(/pages\/([a-z-]+)\/index/g)].map((m) => m[1]).filter((name) => !firstNames.includes(name));
    if (!outside.length || AI_ONLY.has(file) || file === 'utils/package-pages.js') continue;
    const owner = file.endsWith('.wxml') ? file.replace(/\.wxml$/, '.js') : file;
    const guarded = /require\('(\.\.\/)+utils\/pages\.js'\)|require\('\.\/pages\.js'\)/.test(read(owner));
    if (!guarded) offenders.push(`${file}：${[...new Set(outside)].join('、')}`);
  }
  assert.deepEqual(offenders, []);
});
