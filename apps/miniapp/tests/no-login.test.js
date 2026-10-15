const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const { render, textOf, findAll, hasClass, tapsTo } = require('./wxml-render.js');

// D-056 第一次提审用不含登录的版本：协议未定稿（legal.final 为 false）时不显示登录、关注、订阅提醒、纠错、
// 申请覆盖的入口，也不留"协议定稿后开放"这类字样；改成 true 后一切照旧。
// 开关用真的 utils/consent.js（loginOpen 只看 legal.final），每个关键页面在两种状态下各渲染一遍。
// 依赖都用替身，不读取 legal.js 或本机配置。

const STATES = [false, true];
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const daywatch = { watchDay() {}, stopDay() {} };
const app = { getApp: () => ({ globalData: { statusBarHeight: 20, navHeight: 44, menuWidth: 96 } }) };
const COMPONENTS = { 'feature-closed': 'components/closed/index.wxml' };

// 加载页面或组件：没给替身的依赖返回空对象（别的分支给页面加依赖时这里不用跟着改）
function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, setTimeout, clearTimeout, console,
    require(name) { return Object.hasOwn(deps, name) ? deps[name] : {}; },
    Page(d) { def = d; },
    Component(d) { def = d; },
    ...globals,
  }, { filename: file });
  if (!def) return module.exports;
  const methods = def.methods || {};
  return { ...def, ...methods, data: JSON.parse(JSON.stringify(def.data || {})), setData(values) { Object.assign(this.data, values); } };
}

// 真的 consent.js，只换掉协议内容和接口
function consentFor(final, api = {}) {
  return load('utils/consent.js', {
    '../content/legal.js': { final, version: 'v-test' },
    './api.js': { isLive: () => true, forgetAgreed() {}, ...api },
    './exports.js': {}, './exporter.js': {},
  }, { wx: { getStorageSync: () => '', setStorageSync() {}, showToast() {}, showModal() {} } });
}

// 记下页面请求过哪些接口：协议未定稿时关注、纠错、我的数据等不应发任何请求
// （watchStartup / stopStartup 只是订阅"正在连接"提示，不发请求，不记）
const NOT_REQUESTS = ['watchStartup', 'stopStartup'];
function apiSpy(extra = {}) {
  const calls = [];
  const spy = new Proxy(extra, {
    get(target, name) {
      if (name === 'calls') return calls;
      if (name === 'isLive') return target.isLive || (() => true);
      if (NOT_REQUESTS.includes(name)) return () => {};
      if (Object.hasOwn(target, name)) return (...args) => { calls.push(name); return target[name](...args); };
      return () => { calls.push(name); return new Promise(() => {}); };
    },
  });
  return spy;
}

const shown = (file, data) => render(file, data, { components: COMPONENTS });
const texts = (root, pred) => findAll(root, pred).map((n) => textOf(n).trim());
const NO_GATE_WORDS = /协议定稿后开放|定稿后开放|即将开放|协议正在定稿/;
// 服务端访问日志保存的天数（关于页"数据说明"写的天数要和它一致）
const KEEP_DAYS = Number(/^KEEP_DAYS = (\d+)$/m.exec(fs.readFileSync(path.join(ROOT, '..', '..', 'backend', 'src', 'youju', 'access_log.py'), 'utf8'))[1]);

test('开关只看代码包里的 legal.final：loginOpen 和它一致，不看是否连了接口', () => {
  assert.equal(consentFor(false).loginOpen(), false);
  assert.equal(consentFor(true).loginOpen(), true);
  assert.equal(consentFor(true, { isLive: () => false }).loginOpen(), true);
  assert.equal(consentFor(false, { isLive: () => false }).loginOpen(), false);
});

test('底部导航：未定稿时只有首页、文件、我的三项，三等分；定稿后四项含"关注"；选中项按 app.json 的序号', () => {
  const tabs = JSON.parse(read('app.json')).tabBar.list.map((t) => t.pagePath);
  // app.json 仍保留关注页：定稿后不用改 app.json，分享进来的关注页链接也能打开
  assert.deepEqual(tabs, ['pages/home/index', 'pages/follow/index', 'pages/files/index', 'pages/me/index']);
  for (const final of STATES) {
    let url = '';
    const bar = load('custom-tab-bar/index.js', { '../utils/consent.js': consentFor(final) }, { wx: { switchTab: (o) => { url = o.url; } } });
    const files = tabs.indexOf('pages/files/index');
    const root = shown('custom-tab-bar/index.wxml', { ...bar.data, selected: files });
    const items = texts(root, hasClass('item'));
    assert.deepEqual(items, final ? ['首页', '关注', '文件', '我的'] : ['首页', '文件', '我的']);
    const [barNode] = findAll(root, hasClass('bar'));
    assert.equal(barNode.attrs.style, `grid-template-columns: repeat(${items.length}, 1fr)`);
    assert.deepEqual(texts(root, hasClass('item--on')), ['文件']);
    // 每一项都能点，点了按自己的路径切换
    for (const node of findAll(root, hasClass('item'))) {
      bar.switchTab({ currentTarget: { dataset: { path: node.attrs['data-path'] } } });
      assert.equal(url, node.attrs['data-path']);
      assert.ok(tabs.includes(url.slice(1)));
    }
  }
});

async function mePage(final) {
  const api = apiSpy({ me: () => Promise.resolve({ agreed: null, follows: 0, subscriptions: {} }) });
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final, version: 'v-test' },
    '../../utils/api.js': api,
    '../../utils/consent.js': consentFor(final),
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({ sources: 72, provinces: 28 }), notCoveredNames: () => '' },
    '../../utils/subscribe.js': { remindOpen: () => true },
  }, { ...app, wx: {} });
  page.onLoad(); page.onShow(); await flush();
  return { page, api, root: shown('pages/me/index.wxml', page.data) };
}

test('"我的"：未定稿时顶部只写"有据"和"检索和导出不需要登录"，不查登录状态；没有登录、勾选、纠错、申请覆盖；协议阅读入口保留', async () => {
  const { api, root } = await mePage(false);
  assert.deepEqual(api.calls, []);
  const all = textOf(root);
  assert.deepEqual(texts(root, hasClass('brand')), ['有据 检索和导出不需要登录']);
  for (const word of ['未登录', '微信登录', '我已阅读并同意', '纠错', '申请覆盖', '注销', '我的数据', '登录后']) {
    assert.ok(!all.includes(word), `出现了：${word}`);
  }
  assert.doesNotMatch(all, NO_GATE_WORDS);
  for (const handler of ['login', 'toggleAgree', 'feedback', 'coverage', 'deleteAccount', 'myData']) {
    assert.equal(findAll(root, tapsTo(handler)).length, 0, `还有入口：${handler}`);
  }
  // 宫格只剩两项，按两列排，不留空位
  const [grid] = findAll(root, hasClass('grid'));
  assert.ok(hasClass('grid--2')(grid));
  assert.deepEqual(texts(grid, hasClass('grid-item')), ['导出记录', '我的客户']);
  assert.match(read('pages/me/index.wxss'), /\.grid--2 \{ grid-template-columns: repeat\(2, 1fr\); \}/);
  // 不打开草案协议：那一行改为"隐私保护指引"，打开小程序后台填写的官方指引；关于有据照旧，最后一行没有下划线
  const rows = findAll(root, hasClass('row'));
  assert.deepEqual(rows.map((n) => textOf(n).trim()), ['隐私保护指引', '关于有据']);
  assert.equal(rows[0].attrs.bindtap, 'openPrivacy');
  assert.equal(findAll(root, tapsTo('openAgreement')).length, 0);
  assert.ok(hasClass('row--last')(rows[1]));
});

test('"我的"：定稿后照旧——未登录、勾选同意、微信登录、纠错、申请覆盖都在，宫格三项', async () => {
  const { api, root } = await mePage(true);
  assert.deepEqual(api.calls, ['me']);
  const all = textOf(root);
  for (const word of ['未登录', '登录后可以关注条件、订阅微信提醒、提交纠错', '微信登录', '我已阅读并同意', '纠错', '申请覆盖：你关心的地区还没有？']) {
    assert.ok(all.includes(word), `缺少：${word}`);
  }
  assert.doesNotMatch(all, NO_GATE_WORDS);
  assert.equal(findAll(root, hasClass('brand')).length, 0);
  const [grid] = findAll(root, hasClass('grid'));
  assert.ok(!hasClass('grid--2')(grid));
  assert.deepEqual(texts(grid, hasClass('grid-item')), ['导出记录', '我的客户', '纠错']);
  assert.equal(findAll(root, tapsTo('login')).length, 1);
  assert.equal(findAll(root, tapsTo('coverage')).length, 1);
  const rows = findAll(root, hasClass('row'));
  assert.deepEqual(rows.map((n) => textOf(n).trim()), ['申请覆盖：你关心的地区还没有？', '用户协议与隐私保护指引', '关于有据']);
  assert.equal(rows[1].attrs.bindtap, 'openAgreement');
  assert.equal(findAll(root, tapsTo('openPrivacy')).length, 0);
});

test('隐私保护指引：调用 wx.openPrivacyContract 打开后台填写的官方指引；打不开或基础库太低时提示"暂时打不开，请稍后再试"', () => {
  const run = (wxStub) => {
    const consent = load('utils/consent.js', {
      '../content/legal.js': { final: false, version: 'v' }, './api.js': {}, './exports.js': {}, './exporter.js': {},
    }, { wx: wxStub });
    consent.openPrivacy();
  };
  let opened = 0;
  let toast = null;
  run({ openPrivacyContract: () => { opened += 1; }, showToast: (o) => { toast = o; } });
  assert.equal(opened, 1);
  assert.equal(toast, null);
  run({ openPrivacyContract: (o) => o.fail({ errMsg: 'openPrivacyContract:fail' }), showToast: (o) => { toast = o; } });
  assert.equal(toast.title, '暂时打不开，请稍后再试');
  toast = null;
  run({ showToast: (o) => { toast = o; } });
  assert.equal(toast.title, '暂时打不开，请稍后再试');
});

async function searchPage(final, extra = {}) {
  const page = load('pages/search/index.js', {
    '../../utils/api.js': { search: () => Promise.resolve({ total: 1, total_all: 1, items: [{ id: '1', title: '甲' }] }) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': { MAX: 200 },
    '../../utils/consent.js': consentFor(final), '../../utils/subscribe.js': { remindOpen: () => true }, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: {} });
  page.onLoad({}); await flush();
  Object.assign(page.data, extra);
  return shown('pages/search/index.wxml', page.data);
}

test('搜索页：未定稿时信息栏没有"关注"，底部没有"没找到想要的？告诉我们"，未收录地区不提申请覆盖；定稿后照旧', async () => {
  for (const final of STATES) {
    const root = await searchPage(final);
    const follow = findAll(root, hasClass('follow-mini'));
    assert.equal(follow.length, final ? 1 : 0);
    if (final) assert.equal(follow[0].attrs.bindtap, 'follow');
    assert.equal(findAll(root, tapsTo('feedback')).length, final ? 1 : 0);
    assert.equal(textOf(root).includes('没找到想要的？告诉我们'), final);
    // 选择、导出照旧
    assert.equal(findAll(root, tapsTo('select')).length, 1);
    assert.doesNotMatch(textOf(root), NO_GATE_WORDS);
    const empty = await searchPage(final, { items: [], total: 0, uncovered: '西藏' });
    const note = texts(empty, (n) => textOf(n).includes('还没收录，暂时查不到')).at(-1);
    assert.equal(note.includes('申请覆盖'), final);
    assert.match(note, /^西藏还没收录，暂时查不到这个地区的文件。/);
  }
});

function detailRoot(final) {
  const page = load('pages/detail/index.js', {
    '../../utils/api.js': { detail: () => new Promise(() => {}) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {},
    '../../utils/daywatch.js': daywatch, '../../utils/attachments.js': { decorate: () => [], note: () => '' },
    '../../utils/consent.js': consentFor(final),
  }, { wx: {}, Date });
  page.onLoad({ id: '9' });
  Object.assign(page.data, {
    loading: false, error: '',
    doc: { id: '9', title: '关于申报的通知', tags: ['申报通知'], region: '山东', issuer: '某厅', date: '2026-10-01',
      deadline: { label: '申报截止', notes: ['以原文为准'] }, points: [['发文机关', '某厅']], body: { status: 'none' } },
    due: { days: 10, tone: 'normal' }, dueDate: '10月17日', dueWeek: '周六',
  });
  return shown('pages/detail/index.wxml', page.data);
}

test('详情页：未定稿时截止卡片里没有"订阅截止提醒"，底栏没有"纠错"（剩导出、复制原文链接）；定稿后照旧', () => {
  for (const final of STATES) {
    const root = detailRoot(final);
    assert.equal(findAll(root, tapsTo('toggleRemind')).length, final ? 1 : 0);
    assert.equal(textOf(root).includes('订阅截止提醒（一次）'), final);
    const [bar] = findAll(root, hasClass('bar'));
    assert.deepEqual(findAll(bar, (n) => !!n.attrs.bindtap).map((n) => n.attrs.bindtap), final ? ['exportDoc', 'feedback', 'copy'] : ['exportDoc', 'copy']);
    assert.equal(textOf(bar).includes('纠错'), final);
    // 截止日期照常显示
    assert.match(textOf(root), /申报截止 10月17日 ?周六/);
    assert.doesNotMatch(textOf(root), NO_GATE_WORDS);
  }
});

test('截止页（D-061 合并申报日历）：每条都没有截止提醒的铃铛，提醒只在详情页（定稿后按 remindOpen 显示）', () => {
  const template = read('pages/report/index.wxml');
  assert.doesNotMatch(template, /toggleRemind|class="bell/);
  assert.doesNotMatch(read('pages/report/index.js'), /requestOnce|loginOpen/);
});

test('首页：未定稿时不显示"你关注的条件本周有 N 份新文件"（演示数据里有这个数）；定稿后照旧', () => {
  for (const final of STATES) {
    const page = load('pages/home/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
      '../../utils/api.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
      '../../utils/consent.js': consentFor(final),
    }, app);
    Object.assign(page.data, { stats: { week_total: 3, apply: 1, lists: 1, other: 1, provinces: 28, followed_new: 10 } });
    const root = shown('pages/home/index.wxml', page.data);
    assert.equal(findAll(root, tapsTo('goFollow')).length, final ? 1 : 0);
    assert.equal(textOf(root).includes('你关注的条件'), final);
  }
});

test('关于有据：未定稿时使用说明不提关注和提醒，没有"去纠错"，纠错写发邮件；侵权投诉和协议入口照旧', () => {
  for (const final of STATES) {
    const page = load('pages/about/index.js', {
      '../../content/legal.js': { operator: { name: '运营者', address: '地址', email: 'a@example.com' }, disclaimer: '说明' },
      '../../utils/coverage.js': { loadCoverage: () => new Promise(() => {}) }, '../../utils/format.js': format,
      '../../utils/consent.js': consentFor(final),
    }, { wx: { getAccountInfoSync: () => ({ miniProgram: {} }) } });
    page.onLoad();
    Object.assign(page.data, { cov: { sources: 1, provinces: 1, regions: [], notCovered: [{ name: '西藏', reason: '网站不允许程序访问' }] } });
    const root = shown('pages/about/index.wxml', page.data);
    const all = textOf(root);
    assert.equal(findAll(root, tapsTo('feedback')).length, final ? 1 : 0);
    assert.equal(all.includes('关注这个搜索'), final);
    assert.equal(all.includes('订阅一次微信提醒'), final);
    assert.equal(all.includes('申请覆盖'), final);
    assert.equal(all.includes('内容和原文不一致：发邮件至联系邮箱'), !final);
    assert.ok(all.includes('侵权投诉：发邮件至联系邮箱'));
    assert.ok(all.includes('可导出 Excel 或 Word'));
    assert.ok(all.includes('网站不允许程序访问的，我们不绕过'));
    // 副标题和后台填的简介一个口径：不用“政务”“官方”这类容易让人以为是政府办的词（运营规范 5.14.3）
    assert.ok(all.includes('公开通知与名单检索 · 第三方整理')); assert.ok(!/政务|官方平台(?!，)/.test(all.replace('不是政府官方平台', '')));
    // 定稿前不打开草案协议，只有"隐私保护指引"一行（官方指引）；定稿后照旧是用户协议、隐私保护指引两行
    assert.deepEqual(findAll(root, tapsTo('openAgreement')).map((n) => n.attrs['data-type']), final ? ['user', 'privacy'] : []);
    assert.deepEqual(texts(root, tapsTo('openPrivacy')), final ? [] : ['隐私保护指引']);
    // 数据说明：只在定稿前显示，不提这个版本没有的功能，没有【】占位
    const notes = findAll(root, (n) => hasClass('card')(n) && textOf(n).includes('数据说明'));
    assert.equal(notes.length, final ? 0 : 1);
    if (!final) {
      const note = textOf(notes[0]);
      for (const line of ['本版本不需要登录，不收集姓名、手机号等个人信息', '只把链接或邮箱写入剪切板，不读取剪切板', '导出的文件只保存在你的手机上',
        `服务器按法律要求保存访问日志（如 IP 地址）${KEEP_DAYS} 天`]) assert.ok(note.includes(line), `数据说明缺少：${line}`);
      assert.doesNotMatch(note, /【|】|关注|提醒|纠错|注销|我的数据/);
    }
  }
});

// 直接打开（分享、旧链接）协议未定稿时不开放的页面：只显示"暂未开放"和回首页，不请求接口
const CLOSED_PAGES = [
  { name: 'follow', title: '我的关注', open: (p) => p.onShow(), deps: (api, consent) => ({
    '../../utils/api.js': api, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/subscribe.js': { remindOpen: () => true }, '../../utils/consent.js': consent, '../../utils/conditions.js': conditions,
  }), api: { isLive: () => false, follow: () => Promise.resolve({ week_start: '10-05', new_total: 0, subs: [], changes: [] }) } },
  { name: 'follow-new', title: '上次查看之后有', open: (p) => p.onLoad({ id: '7', name: '甲' }), deps: (api, consent) => ({
    '../../utils/api.js': api, '../../utils/format.js': format, '../../utils/conditions.js': conditions, '../../utils/consent.js': consent,
  }), api: { followNew: () => Promise.resolve({ total: 0, folded: 0, items: [] }) } },
  { name: 'feedback', title: '提交', open: (p) => p.onLoad({ type: 'coverage' }), deps: (api, consent) => ({
    '../../utils/api.js': api, '../../utils/consent.js': consent,
  }), api: {} },
  { name: 'my-data', title: '你的账号下没有保存个人信息', open: (p) => p.onShow(), deps: (api, consent) => ({
    '../../utils/api.js': api, '../../utils/conditions.js': conditions, '../../utils/consent.js': consent,
  }), api: { myData: () => Promise.resolve({ user: null, follows: [], subscriptions: [], feedback: [], retention: {} }) } },
  // 草案协议页：定稿前不显示（入口改为官方隐私保护指引），定稿后照旧
  { name: 'agreement', title: '《隐私保护指引》', open: (p) => p.onLoad({ type: 'privacy' }), deps: (api, consent, final) => ({
    '../../content/legal.js': { final, version: 'v', status: '草案', disclaimer: '说明',
      user: { title: '用户协议', sections: [] }, privacy: { title: '隐私保护指引', sections: [{ h: '一', p: ['甲'] }] } },
    '../../utils/api.js': api, '../../utils/consent.js': consent,
  }), api: {} },
];

for (const spec of CLOSED_PAGES) {
  test(`直接打开 ${spec.name}：未定稿时显示"这个功能暂未开放"和回首页、不请求接口；定稿后照旧`, async () => {
    for (const final of STATES) {
      const api = apiSpy(spec.api);
      const page = load(`pages/${spec.name}/index.js`, spec.deps(api, consentFor(final), final), { ...app, wx: { setNavigationBarTitle() {}, getStorageSync: () => '' } });
      spec.open(page); await flush();
      const root = shown(`pages/${spec.name}/index.wxml`, page.data);
      const all = textOf(root);
      assert.equal(all.includes('这个功能暂未开放'), !final);
      assert.equal(findAll(root, tapsTo('goHome')).length, final ? 0 : 1);
      assert.equal(all.includes(spec.title), final, `${spec.name}：${all}`);
      if (!final) assert.deepEqual(api.calls, []);
      assert.doesNotMatch(all, NO_GATE_WORDS);
    }
  });
}

test('"暂未开放"的回首页切到首页底栏', () => {
  let url = '';
  const closed = load('components/closed/index.js', {}, { wx: { switchTab: (o) => { url = o.url; } } });
  closed.goHome();
  assert.equal(url, '/pages/home/index');
  assert.equal(JSON.parse(read('components/closed/index.json')).component, true);
  for (const name of CLOSED_PAGES.map((p) => p.name)) {
    assert.equal(JSON.parse(read(`pages/${name}/index.json`)).usingComponents['feature-closed'], '/components/closed/index');
  }
});

test('兜底：未定稿时 ensureConsent 和订阅提醒只轻提示"这个功能暂未开放"，不弹"协议定稿后开放"', async () => {
  let toast = null;
  const consent = load('utils/consent.js', {
    '../content/legal.js': { final: false, version: 'v' }, './api.js': { isLive: () => true }, './exports.js': {}, './exporter.js': {},
  }, { wx: { showToast: (o) => { toast = o; }, showModal: () => assert.fail('不应弹对话框'), getStorageSync: () => '' } });
  assert.equal(await consent.ensureConsent(), false);
  assert.equal(toast.title, '这个功能暂未开放');
  const subscribe = load('utils/subscribe.js', { './config.js': { templates: {} }, './api.js': {}, './consent.js': consent }, { wx: { showModal: () => assert.fail('不应弹对话框') } });
  toast = null;
  assert.equal(await subscribe.requestOnce('deadline', { docId: '9' }), false);
  assert.equal(toast.title, '这个功能暂未开放');
});

test('小程序代码里不再有"协议定稿后开放""即将开放"这类字样', () => {
  const files = [];
  const walk = (dir) => {
    for (const name of fs.readdirSync(path.join(ROOT, dir))) {
      const rel = path.join(dir, name);
      if (['tests', 'miniprogram_npm', 'node_modules', 'mock'].includes(name)) continue;
      if (fs.statSync(path.join(ROOT, rel)).isDirectory()) walk(rel);
      else if (/\.(js|wxml|json)$/.test(name) && name !== 'legal.js') files.push(rel);
    }
  };
  walk('.');
  assert.ok(files.length > 30);
  for (const file of files) {
    const shownText = read(file).replace(/<!--[\s\S]*?-->/g, '').split('\n').filter((line) => !/^\s*\/\//.test(line)).join('\n');
    assert.doesNotMatch(shownText, NO_GATE_WORDS, file);
  }
});

test('订阅消息模板没配时不显示提醒入口：关注页、详情、我的页都按 remindOpen 判断；关注后只提示已关注', async () => {
  const consent = { ensureConsent: () => Promise.resolve(true), agreedHere: () => true, consentOpen: () => true };
  const toasts = [];
  const off = load('utils/subscribe.js', { './config.js': { templates: { newDocs: '', deadline: '' } }, './api.js': {}, './consent.js': consent },
    { wx: { showToast: (o) => toasts.push(o.title), showModal: () => assert.fail('不应弹对话框') } });
  assert.equal(off.remindOpen('newDocs'), false); assert.equal(off.remindOpen('deadline'), false);
  assert.equal(await off.requestOnce('newDocs', { followId: 1 }), false);
  assert.deepEqual(toasts, ['微信提醒暂未开通']);
  const on = load('utils/subscribe.js', { './config.js': { templates: { newDocs: 'tmpl-1', deadline: '' } }, './api.js': {}, './consent.js': consent }, { wx: {} });
  assert.equal(on.remindOpen('newDocs'), true); assert.equal(on.remindOpen('deadline'), false);

  const follows = [{ id: 1, name: '山东专精特新', desc: '山东', newCount: 0 }];
  const followData = { mode: 'live', loginOpen: true, follows, changes: [], remindLeft: {}, loading: false, error: '' };
  assert.doesNotMatch(textOf(render('pages/follow/index.wxml', { ...followData, remindOpen: false })), /提醒/);
  assert.match(textOf(render('pages/follow/index.wxml', { ...followData, remindOpen: true })), /提醒/);
  assert.match(read('pages/detail/index.wxml'), /loginOpen && remindOpen && due\.days >= 3/);
  assert.match(read('pages/me/index.wxml'), /wx:if="{{remindOpen}}"> · 还能提醒/);
  const search = read('pages/search/index.js');
  assert.ok(search.indexOf("if (!remindOpen('newDocs'))") < search.indexOf("confirmText: '订阅提醒'"));
});
