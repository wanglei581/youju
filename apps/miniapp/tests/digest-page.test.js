const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const mock = require('../mock/data.js');
const { render, textOf, findAll, hasClass, tapsTo } = require('./wxml-render.js');

// 公众号方案 PR #287 第 3 件事：小程序"本期清单"页、"我的 → 本周周报"、截止页/我的/关于的"关注公众号"、
// 首页扫码关注条、接口走常驻服务。依赖都用替身，不读取 legal.js 或本机配置。
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const plain = (value) => JSON.parse(JSON.stringify(value));
// 关注入口不能带的字样：不诱导关注、不给奖励、不挡功能（D-041、D-070，《互联网用户公众账号信息服务管理规定》第十二条）
const PROMO = ['奖励', '邀请', '红包', '领取', '返现', '积分', '分享', '朋友圈', '关注后', '才能', '解锁', '免费领'];

function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, exports: module.exports, setTimeout, clearTimeout, console, Date, Promise,
    require(name) {
      assert.ok(Object.hasOwn(deps, name), `${file} 缺少依赖 ${name}`);
      return deps[name];
    },
    Page(d) { def = d; },
    Component(d) { def = d; },
    ...globals,
  }, { filename: file });
  if (!def) return module.exports;
  return { ...def, data: plain(def.data || {}), setData(values) { Object.assign(this.data, plain(values)); } };
}

// ---------- 关注公众号：配置、基础库版本、只在点击时打开 ----------

function officialAccount(config, wx) {
  return load('utils/official-account.js', { './config.js': config }, { wx });
}
const ACCOUNT = { username: 'gh_0123456789ab', name: '有据申报与行业助手' };

test('关注公众号：没填原始 ID、填错、基础库低于 3.7.10 或没有接口时不显示；版本逐段比较', () => {
  const opened = [];
  const wx = (sdk, api = true) => ({
    getAppBaseInfo: () => ({ SDKVersion: sdk }),
    ...(api ? { openOfficialAccountProfile: (o) => opened.push(o) } : {}),
  });
  assert.equal(officialAccount({ officialAccount: { username: '', name: '' } }, wx('3.8.0')).canFollow(), false);
  assert.equal(officialAccount({}, wx('3.8.0')).canFollow(), false);
  assert.equal(officialAccount({ officialAccount: { username: '有据申报', name: '' } }, wx('3.8.0')).canFollow(), false);
  assert.equal(officialAccount({ officialAccount: ACCOUNT }, wx('3.7.9')).canFollow(), false);
  assert.equal(officialAccount({ officialAccount: ACCOUNT }, wx('2.32.3')).canFollow(), false);
  assert.equal(officialAccount({ officialAccount: ACCOUNT }, wx('3.7.10', false)).canFollow(), false);
  assert.equal(officialAccount({ officialAccount: ACCOUNT }, {}).canFollow(), false); // 取不到版本号按太低算
  assert.equal(officialAccount({ officialAccount: ACCOUNT }, wx('3.7.10')).canFollow(), true);
  assert.equal(officialAccount({ officialAccount: ACCOUNT }, wx('3.10.0')).canFollow(), true);
  const { compareVersion, MIN_SDK } = officialAccount({}, {});
  assert.equal(MIN_SDK, '3.7.10');
  assert.equal(compareVersion('3.10.0', '3.7.10'), 1);
  assert.equal(compareVersion('3.7.10', '3.7.10'), 0);
  assert.equal(compareVersion('3.7', '3.7.10'), -1);
  // 首页扫码关注条只看有没有配原始 ID（显示与否由微信按场景决定）
  assert.equal(officialAccount({ officialAccount: ACCOUNT }, {}).showBar(), true);
  assert.equal(officialAccount({ officialAccount: { username: '' } }, {}).showBar(), false);
});

test('关注公众号：点了才打开公众号主页，传原始 ID；打不开时提示在微信里搜名称', () => {
  const opened = [];
  const toasts = [];
  const wx = {
    getAppBaseInfo: () => ({ SDKVersion: '3.8.0' }),
    openOfficialAccountProfile: (o) => opened.push(o),
    showToast: (o) => toasts.push(o),
  };
  const oa = officialAccount({ officialAccount: ACCOUNT }, wx);
  oa.openProfile();
  assert.equal(opened.length, 1);
  assert.equal(opened[0].username, 'gh_0123456789ab');
  opened[0].fail({ errMsg: 'openOfficialAccountProfile:fail' });
  assert.deepEqual(plain(toasts), [{ title: '没能打开公众号，可以在微信里搜「有据申报与行业助手」', icon: 'none' }]);
  // 没配原始 ID 时什么都不做
  officialAccount({ officialAccount: { username: '' } }, wx).openProfile();
  assert.equal(opened.length, 1);
});

function followComponent(canFollow) {
  let opened = 0;
  const def = load('components/follow-account/index.js', {
    '../../utils/official-account.js': { canFollow: () => canFollow, openProfile: () => { opened += 1; } },
  });
  const self = { data: { ...def.data, text: def.properties.text.value, variant: def.properties.variant.value }, setData(v) { Object.assign(this.data, v); } };
  def.lifetimes.attached.call(self);
  return { def, self, opened: () => opened };
}

test('"关注公众号"组件：能打开才显示一行，点了打开；两种样式；文字不诱导、不给奖励', () => {
  const on = followComponent(true);
  assert.equal(on.self.data.show, true);
  on.def.methods.open.call(on.self);
  assert.equal(on.opened(), 1);
  const bar = render('components/follow-account/index.wxml', on.self.data);
  assert.equal(textOf(bar).trim(), '关注公众号，每周一收申报周报');
  assert.equal(findAll(bar, hasClass('bar')).length, 1);
  assert.equal(findAll(bar, tapsTo('open')).length, 1);
  const row = render('components/follow-account/index.wxml', { ...on.self.data, variant: 'row' });
  assert.equal(findAll(row, hasClass('row')).length, 1);
  const off = followComponent(false);
  assert.equal(off.self.data.show, false);
  assert.equal(textOf(render('components/follow-account/index.wxml', off.self.data)).trim(), '');
  for (const word of PROMO) assert.ok(!read('components/follow-account/index.wxml').includes(word), word);
});

test('关注入口放在截止页底部、我的、关于；首页只放一个扫码关注条，配了原始 ID 才放', () => {
  const report = read('pages/report/index.wxml');
  assert.match(report, /<follow-account text="每周四推送 7 天内已核对的截止，关注公众号" \/>/);
  // 在页面最后的说明之后：不挡筛选和列表
  assert.ok(report.indexOf('<follow-account') > report.indexOf('class="note"'));
  assert.match(read('pages/me/index.wxml'), /<follow-account variant="row" \/>/);
  assert.match(read('pages/about/index.wxml'), /<follow-account \/>/);
  for (const page of ['report', 'me', 'about']) {
    const json = JSON.parse(read(`pages/${page}/index.json`));
    assert.equal(json.usingComponents['follow-account'], '/components/follow-account/index', page);
  }
  for (const text of [report, read('pages/me/index.wxml'), read('pages/about/index.wxml')]) {
    const lines = text.split('\n').filter((line) => line.includes('follow-account') || line.includes('公众号'));
    for (const word of PROMO) assert.ok(!lines.join('').includes(word), word);
  }
  const home = read('pages/home/index.wxml');
  assert.equal(home.match(/<official-account/g).length, 1); // 微信限定每页一个
  assert.match(home, /<official-account wx:if="\{\{followBar\}\}"/);
  // 别的页面不放扫码关注条
  for (const page of JSON.parse(read('app.json')).pages.filter((p) => p !== 'pages/home/index')) {
    assert.doesNotMatch(read(`${page}.wxml`), /<official-account/, page);
  }
});

test('首页扫码关注条：app.js 按配置算好，首页读 globalData；没配时不放', () => {
  const run = (showBar) => {
    let app;
    vm.runInNewContext(read('app.js'), {
      App(def) { app = def; },
      require(name) {
        if (name === './utils/config.js') return {};
        if (name === './utils/nav.js') return { navMetrics: () => ({}) };
        if (name === './utils/official-account.js') return { showBar: () => showBar };
        if (name === './utils/api.js') return { prewarm() {} };
        throw new Error(`没有替身：${name}`);
      },
      wx: { getWindowInfo: () => ({}), getMenuButtonBoundingClientRect: () => ({}) },
    });
    app.onLaunch();
    return app.globalData.followBar;
  };
  assert.equal(run(true), true);
  assert.equal(run(false), false);
  const home = render('pages/home/index.wxml', { followBar: true, scopes: [{ label: '查申报', placeholder: '' }], scope: 0, hot: [] });
  assert.equal(findAll(home, (n) => n.tag === 'official-account').length, 1);
  const without = render('pages/home/index.wxml', { followBar: false, scopes: [{ label: '查申报', placeholder: '' }], scope: 0, hot: [] });
  assert.equal(findAll(without, (n) => n.tag === 'official-account').length, 0);
  assert.match(read('pages/home/index.js'), /followBar: Boolean\(followBar\)/);
});

// ---------- 本期清单页 ----------

function row(id, province, section, extra = {}) {
  return { id: String(id), kind: section === 'lists' ? '名单' : section === 'policies' ? '政策' : '申报通知', title: `第 ${id} 条`, issuer: `${province}科技厅`, region: province, province, date: '2026-09-28', url: `https://example.gov.cn/${id}`, deadline: null, deadline_state: 'none', section, ...extra };
}
const ITEMS = [
  row(1, '国家', 'applications'),
  row(2, '浙江', 'deadlines', { deadline: { date: '2026-10-10', time: '', verified: true }, deadline_state: 'verified' }),
  row(3, '浙江', 'applications'),
  row(4, '浙江', 'lists'),
  row(5, '浙江', 'policies'),
  row(6, '江苏', 'applications'),
];
// 和后端一样：按省份筛、按 offset/limit 分页；省份列表总是给全的
function server(items, extra = {}) {
  const provinces = [];
  items.forEach((d) => {
    const found = provinces.find((p) => p.province === d.province);
    if (found) found.count += 1; else provinces.push({ province: d.province, count: 1 });
  });
  return (id, params = {}) => {
    const chosen = params.province ? items.filter((d) => d.province === params.province) : items;
    return {
      id: Number(id), title: '申报周报｜9月28日—10月4日（全国）：新通知3条', weekStart: '2026-09-28', weekEnd: '2026-10-04',
      dataAt: '2026-10-04T23:30:00+00:00', hidden: 1, provinces, province: params.province || '',
      total: chosen.length, offset: params.offset, items: chosen.slice(params.offset, params.offset + params.limit), ...extra,
    };
  };
}

function digestPage(api, wx = {}) {
  return load('pages/digest/index.js', { '../../utils/api.js': api, '../../utils/format.js': format }, { wx });
}

function apiStub(responses) {
  const calls = [];
  const answer = (name, label, value) => {
    calls.push(label);
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  };
  return {
    calls,
    digest: (id, params) => {
      const value = typeof responses.digest === 'function' ? responses.digest(id, params) : responses.digest;
      return answer('digest', `digest:${id}:${params.province || '全部'}:${params.offset}+${params.limit}`, value);
    },
    latestDigest: () => answer('latestDigest', 'latestDigest', responses.latestDigest),
  };
}
const notFound = (message) => Object.assign(new Error(message), { status: 404 });

test('本期清单：带期号打开，按省份分组（顺序照接口），组内按栏目，写统计周、数据截至和已下架条数', async () => {
  const api = apiStub({ digest: server(ITEMS) });
  const page = digestPage(api);
  page.onLoad({ id: '12' });
  await flush();
  assert.deepEqual(api.calls, ['digest:12:全部:0+100']);
  assert.equal(page.data.loading, false);
  assert.equal(page.data.weekText, '9月28日—10月4日');
  assert.equal(page.data.dataText, '10月5日 07:30');
  assert.deepEqual(page.data.labels, ['全部地区（6 条）', '国家部委（1 条）', '浙江（4 条）', '江苏（1 条）']);
  assert.deepEqual(page.data.groups.map((g) => [g.name, g.count, g.sections.map((s) => s.label)]), [
    ['国家部委', 1, ['新申报通知']],
    ['浙江', 4, ['30 天内截止', '新申报通知', '名单', '政策文件']],
    ['江苏', 1, ['新申报通知']],
  ]);
  assert.equal(page.data.rest, 0);
  const root = render('pages/digest/index.wxml', page.data);
  const text = textOf(root);
  assert.match(text, /9月28日—10月4日 申报周报/);
  assert.match(text, /共 6 条，按省份分好；点一条看详情和原文出处。/);
  assert.match(text, /数据截至 10月5日 07:30/);
  // 出处说明只在顶部 notice-bar 写一次（#339）：页面里不再重复
  assert.doesNotMatch(text, /第三方整理/);
  assert.equal(findAll(root, (n) => n.tag === 'notice-bar').length, 1);
  assert.match(text, /另有 1 条现已不再提供，这里不列出。/);
  assert.match(text, /地市、区县组织初审或推荐的，报送时间可能更早。/);
  assert.doesNotMatch(text, /显示更多/);
  // 每条用列表行组件：点进详情看原文（doc-row 自己打开 /pages/detail/index?id=）
  assert.equal(findAll(root, (n) => n.tag === 'doc-row').length, 6);
  assert.match(read('components/doc-row/index.js'), /\/pages\/detail\/index\?id=\$\{this\.data\.doc\.id\}/);
});

test('本期清单：分页取，每次 100 条，"显示更多"接着取；按省份筛由服务端筛、从头取；组标题写整省条数', async () => {
  const items = [];
  for (let n = 1; n <= 130; n += 1) items.push(row(n, n <= 100 ? '浙江' : '江苏', 'applications'));
  const api = apiStub({ digest: server(items, { hidden: 0 }) });
  const page = digestPage(api, { pageScrollTo() {} });
  page.onLoad({ id: '12' });
  await flush();
  const count = () => page.data.groups.reduce((sum, g) => sum + g.sections.reduce((s, x) => s + x.items.length, 0), 0);
  assert.equal(count(), 100);
  assert.equal(page.data.rest, 30);
  assert.deepEqual(page.data.groups.map((g) => [g.name, g.count]), [['浙江', 100]]);
  assert.match(textOf(render('pages/digest/index.wxml', page.data)), /显示更多（还有 30 条）/);
  page.more();
  assert.equal(page.data.moreLoading, true);
  assert.match(textOf(render('pages/digest/index.wxml', page.data)), /正在加载…/);
  page.more(); // 取的时候再点不重复取
  await flush();
  assert.equal(count(), 130);
  assert.equal(page.data.rest, 0);
  assert.deepEqual(page.data.groups.map((g) => [g.name, g.count]), [['浙江', 100], ['江苏', 30]]);
  page.pickProvince({ detail: { value: '2' } });
  await flush();
  assert.deepEqual(page.data.groups.map((g) => g.name), ['江苏']);
  assert.equal(count(), 30);
  page.pickProvince({ detail: { value: '2' } }); // 选同一项不重取
  page.pickProvince({ detail: { value: '9' } }); // 不存在的选项不理会
  assert.deepEqual(api.calls, ['digest:12:全部:0+100', 'digest:12:全部:100+100', 'digest:12:江苏:0+100']);
  page.pickProvince({ detail: { value: '0' } });
  await flush();
  assert.equal(count(), 100);
  assert.equal(page.data.labels[0], '全部地区（130 条）');
});

test('本期清单："显示更多"没取到时提示、保留已显示的；换省份时取不到写明可重试', async () => {
  let fail = false;
  const items = [];
  for (let n = 1; n <= 120; n += 1) items.push(row(n, '浙江', 'applications'));
  const base = server(items);
  const toasts = [];
  const page = digestPage(apiStub({ digest: (id, params) => (fail ? new Error('网络连接失败，请检查网络后重试') : base(id, params)) }), { showToast: (o) => toasts.push(o.title), pageScrollTo() {} });
  page.onLoad({ id: '12' });
  await flush();
  fail = true;
  page.more();
  await flush();
  assert.deepEqual(toasts, ['没加载出来：网络连接失败，请检查网络后重试']);
  assert.equal(page.data.moreLoading, false);
  assert.equal(page.data.rest, 20);
  page.pickProvince({ detail: { value: '1' } });
  await flush();
  assert.equal(page.data.error, '本期清单没有加载出来：网络连接失败，请检查网络后重试');
  assert.equal(findAll(render('pages/digest/index.wxml', page.data), tapsTo('load')).length, 1);
});

test('本期清单：从"我的"打开（不带期号）取最近一期', async () => {
  const api = apiStub({ latestDigest: { id: 12, weekStart: '2026-09-28', weekEnd: '2026-10-04' }, digest: server(ITEMS) });
  const page = digestPage(api);
  page.onLoad({});
  await flush();
  assert.deepEqual(api.calls, ['latestDigest', 'digest:12:全部:0+100']);
  assert.equal(page.data.groups.length, 3);
});

test('本期清单：带了期号但不完整时说"链接不完整"，不悄悄换成最近一期', async () => {
  for (const id of ['12;drop', 'abc', '']) {
    const api = apiStub({ latestDigest: { id: 3 }, digest: server(ITEMS) });
    const page = digestPage(api);
    page.onLoad({ id });
    await flush();
    if (id === '') {
      assert.deepEqual(api.calls, ['latestDigest', 'digest:3:全部:0+100']); // 空参数按没带算
      continue;
    }
    assert.deepEqual(api.calls, []);
    assert.equal(page.data.missing, '这一期的链接不完整，打不开');
    assert.match(textOf(render('pages/digest/index.wxml', page.data)), /这一期的链接不完整，打不开/);
  }
});

test('本期清单：不存在或已撤回、还没有发布的、加载失败分别说明，给出路', async () => {
  let page = digestPage(apiStub({ digest: notFound('这一期周报不存在，或已撤回') }));
  page.onLoad({ id: '99' });
  await flush();
  assert.equal(page.data.missing, '这一期周报不存在，或已撤回');
  let root = render('pages/digest/index.wxml', page.data);
  assert.match(textOf(root), /公众号每周一发申报周报；截止日期、名单也可以直接在小程序里查。/);
  assert.equal(findAll(root, tapsTo('goReport')).length, 1);
  assert.equal(findAll(root, tapsTo('goHome')).length, 1);
  page = digestPage(apiStub({ latestDigest: notFound('还没有发布的周报') }));
  page.onLoad({});
  await flush();
  assert.equal(page.data.missing, '还没有发布的周报');
  const api = apiStub({ digest: new Error('网络连接失败，请检查网络后重试') });
  page = digestPage(api);
  page.onLoad({ id: '12' });
  await flush();
  assert.equal(page.data.error, '本期清单没有加载出来：网络连接失败，请检查网络后重试');
  root = render('pages/digest/index.wxml', page.data);
  assert.equal(findAll(root, tapsTo('load')).length, 1);
  const urls = [];
  const nav = digestPage(apiStub({}), { navigateTo: (o) => urls.push(o.url), switchTab: (o) => urls.push(o.url) });
  nav.goReport();
  nav.goHome();
  assert.deepEqual(urls, ['/pages/report/index?days=7', '/pages/home/index']);
});

test('本期清单：条目全下架时写明；跨年的统计周两头写年份', async () => {
  const page = digestPage(apiStub({ digest: () => ({ ...server([])(12, { offset: 0, limit: 100 }), hidden: 6, weekStart: '2025-12-29', weekEnd: '2026-01-04' }) }));
  page.onLoad({ id: '12' });
  await flush();
  const text = textOf(render('pages/digest/index.wxml', page.data));
  assert.match(text, /这一期的条目现已都不再提供。/);
  assert.match(text, /另有 6 条现已不再提供/);
  assert.equal(page.data.weekText, '2025年12月29日—2026年1月4日');
});

test('本期清单页的样式类名不和列表行组件 doc-row 撞名（doc-row 共享页面样式，撞了会把每条的排版带歪）', () => {
  const wxss = read('pages/digest/index.wxss').replace(/\/\*[\s\S]*?\*\//g, '');
  const pageClasses = new Set([...wxss.matchAll(/\.([a-z][\w-]*)/g)].map((m) => m[1]));
  const rowClasses = new Set([...read('components/doc-row/index.wxml').matchAll(/class="([^"]+)"/g)]
    .flatMap((m) => m[1].replace(/\{\{[^}]*\}\}/g, ' ').split(/\s+/)).filter(Boolean));
  assert.deepEqual([...pageClasses].filter((name) => rowClasses.has(name)), []);
});

test('本期清单页注册了、用默认导航栏（从公众号打开时微信给"回首页"），不能转发、不加奖励字样', () => {
  const app = JSON.parse(read('app.json'));
  assert.ok(app.pages.includes('pages/digest/index'));
  assert.deepEqual(JSON.parse(read('pages/digest/index.json')), { navigationBarTitleText: '本期清单', usingComponents: {} });
  assert.doesNotMatch(read('pages/digest/index.js'), /onShareAppMessage|onShareTimeline/);
  for (const word of PROMO) assert.ok(!read('pages/digest/index.wxml').includes(word), word);
});

// ---------- 我的 → 本周周报 ----------

function mePage(latest) {
  const urls = [];
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: false, version: 'v-test' },
    '../../utils/api.js': { isLive: () => true, latestDigest: () => latest },
    '../../utils/consent.js': { applyServer() {}, consentEpoch: () => 0, withdraw() {}, markAgreed() {}, loginOpen: () => false, openPrivacy() {}, CONSENT_PAGE: '', agreeFailed() {}, featureNames: () => '' },
    '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null), notCoveredNames: () => '' },
    // 完整版的页面清单（仓库里的 app.json，含本期清单页）；过审版不显示这一行见 package-pages.test.js
    '../../utils/pages.js': require('../utils/pages.js'),
  }, { getApp: () => ({ globalData: {} }), wx: { navigateTo: (o) => urls.push(o.url) } });
  return { page, urls };
}

test('我的：有已发布的周报时显示"本周周报"和统计周，点了打开本期清单；没有或取不到时不显示', async () => {
  const { page, urls } = mePage(Promise.resolve({ id: 12, title: '申报周报', weekStart: '2026-09-28', weekEnd: '2026-10-04' }));
  page.onLoad();
  page.onShow();
  await flush();
  assert.deepEqual(page.data.digestEntry, { id: '12', week: '9月28日—10月4日' });
  const root = render('pages/me/index.wxml', page.data);
  const rows = findAll(root, hasClass('row')).map((n) => textOf(n).replace(/\s+/g, ' ').trim());
  assert.equal(rows[0], '本周周报9月28日—10月4日');
  page.openDigest();
  assert.deepEqual(urls, ['/pages/digest/index?id=12']);
  const none = mePage(Promise.reject(Object.assign(new Error('还没有发布的周报'), { status: 404 })));
  none.page.onLoad();
  none.page.onShow();
  await flush();
  assert.equal(none.page.data.digestEntry, null);
  assert.ok(!textOf(render('pages/me/index.wxml', none.page.data)).includes('本周周报'));
});

// ---------- 接口：走常驻服务、演示数据、"本周周报"不重复问 ----------

function liveApi(config, answer) {
  const sent = [];
  const api = load('utils/api.js', {
    './config.js': config, '../mock/data.js': mock, './mockSearch.js': { mockSearch() {} }, './format.js': format,
  }, {
    wx: {
      cloud: { callContainer: (o) => { sent.push([o.path, o.header['X-WX-SERVICE']]); answer(o); } },
      request: (o) => { sent.push([o.url, '']); answer(o); },
    },
  });
  return { api, sent };
}

test('接口：本期清单和最近一期先走常驻的 admin 服务（公众号文章点进来不等冷启动）', async () => {
  const { api, sent } = liveApi({ cloudEnv: 'prod-test', cloudService: 'api', devBaseUrl: '' }, (o) => o.success({ statusCode: 200, data: { id: 12, items: [] } }));
  await api.digest(12);
  await api.digest(12, { province: '浙江', offset: 100, limit: 100 });
  await api.latestDigest();
  assert.deepEqual(sent, [
    ['/api/v1/digests/12', 'admin'],
    ['/api/v1/digests/12?province=%E6%B5%99%E6%B1%9F&offset=100&limit=100', 'admin'],
    ['/api/v1/digests/latest', 'admin'],
  ]);
  const routes = read('../../backend/src/youju/api/app.py');
  assert.match(routes, /"\/api\/v1\/digests\/latest"/);
  assert.match(routes, /\(\?:documents\|digests\)\/\\d\+/);
});

test('接口："本周周报"10 分钟内不重复问；还没有发布（404）也记住，网络出错下次再问', async () => {
  let status = 404;
  const { api, sent } = liveApi({ cloudEnv: '', cloudService: '', devBaseUrl: 'https://test.invalid' }, (o) => {
    if (status === 0) o.fail({ errMsg: 'request:fail' });
    else o.success({ statusCode: status, data: status === 200 ? { id: 3 } : { detail: '还没有发布的周报' } });
  });
  await assert.rejects(api.latestDigest(), (err) => err.status === 404);
  await assert.rejects(api.latestDigest(), (err) => err.status === 404);
  assert.equal(sent.length, 1);
  const fresh = liveApi({ cloudEnv: '', cloudService: '', devBaseUrl: 'https://test.invalid' }, (o) => {
    if (status === 0) o.fail({ errMsg: 'request:fail' });
    else o.success({ statusCode: status, data: { id: 3 } });
  });
  status = 0;
  await assert.rejects(fresh.api.latestDigest());
  status = 200;
  assert.deepEqual(plain(await fresh.api.latestDigest()), { id: 3 });
  assert.equal(fresh.sent.length, 2);
});

test('演示数据：用首页的几份文件拼一期，标"演示数据"；国家部委在前，组内先截止再新通知、名单、政策', async () => {
  const { api } = liveApi({ cloudEnv: '', cloudService: '', devBaseUrl: '' }, () => {});
  const d = await api.digest(0);
  assert.equal(d.demo, true);
  assert.equal(d.items.length, mock.home.length);
  assert.equal(d.total, mock.home.length);
  assert.equal(d.provinces.reduce((sum, p) => sum + p.count, 0), mock.home.length);
  const one = await api.digest(0, { province: d.provinces[1].province, offset: 0, limit: 100 });
  assert.ok(one.items.length && one.items.every((item) => item.province === d.provinces[1].province));
  assert.equal((await api.digest(0, { offset: 2, limit: 3 })).items.length, 3);
  assert.equal(d.items[0].province, '国家');
  const order = ['deadlines', 'applications', 'lists', 'policies'];
  for (let n = 1; n < d.items.length; n += 1) {
    const [a, b] = [d.items[n - 1], d.items[n]];
    if (a.province === b.province) assert.ok(order.indexOf(a.section) <= order.indexOf(b.section));
  }
  assert.equal((await api.latestDigest()).demo, true);
});
