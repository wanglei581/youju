const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const boost = require('../utils/boost.js');
const conditions = require('../utils/conditions.js');
const regions = require('../utils/regions.js');
const { render, textOf, findAll, hasClass, tapsTo } = require('./wxml-render.js');

// D-070 第一批：① 通知详情、搜索结果、截止、查名单四页能转发给好友和群（不开朋友圈、不带奖励），接收人不用登录、
// 打开后是同样的条件；④ 首页"三步上手"卡片，点"不再显示"后本机记住。依赖都用替身，不读取 legal.js 或本机配置。
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const daywatch = { watchDay() {}, stopDay() {} };
const noCache = { readCache: () => null, writeCache() {}, cachedLabel: () => '' };
const tap = (dataset) => ({ currentTarget: { dataset } });
const plain = (value) => JSON.parse(JSON.stringify(value));
// 页面在单独的沙箱里运行，返回的对象先转成普通对象再比较
const shareOf = (page) => plain(page.onShareAppMessage({ from: 'menu' }));
// 转发卡片标题和引导卡片里都不能有的字样：不带奖励、不引导转发和拉人（D-070）
const PROMO = ['奖励', '邀请', '红包', '领取', '返现', '积分', '分享', '朋友圈', 'AI', '智能'];

function load(file, deps = {}, globals = {}) {
  let def;
  vm.runInNewContext(read(file), {
    module: { exports: {} }, setTimeout, clearTimeout, console,
    require(name) {
      if (name === '../../utils/regions.js') return regions;
      if (!Object.hasOwn(deps, name) && /ai-access\.js$/.test(name)) return AI_OFF; if (!Object.hasOwn(deps, name) && /(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages; if (!Object.hasOwn(deps, name) && /utils\/advisor\.js$/.test(name)) return require('./helpers/package-pages-default.js').advisor; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`);
      return deps[name];
    },
    Page(d) { def = d; },
    ...globals,
  }, { filename: file });
  return { ...def, data: plain(def.data), setData(values) { Object.assign(this.data, values); } };
}

// 转发路径 → 页面和 onLoad 收到的参数。微信把路径里的参数原样（没解码）交给 onLoad：开发者工具实测
// navigateTo 和用场景值 1007/1008 从聊天卡片打开都是这样，页面自己解码一次
function open(sharePath) {
  const [page, search = ''] = sharePath.split('?');
  const query = {};
  for (const part of search.split('&').filter(Boolean)) {
    const at = part.indexOf('=');
    query[part.slice(0, at)] = part.slice(at + 1);
  }
  return { page, query };
}

test('只有通知详情、搜索结果、截止、查名单四页能转发；没有页面开朋友圈；转发按钮只在通知详情底栏', () => {
  const app = JSON.parse(read('app.json'));
  const sharing = app.pages.filter((p) => /onShareAppMessage\s*\(/.test(read(`${p}.js`)));
  assert.deepEqual(sharing.sort(), ['pages/detail/index', 'pages/lists/index', 'pages/report/index', 'pages/search/index']);
  for (const p of app.pages) {
    assert.doesNotMatch(read(`${p}.js`), /onShareTimeline|showShareMenu\([^)]*shareTimeline/, `${p} 开了朋友圈`);
    // 10/18 创始人批准验收问题 9a：只在通知详情底栏加一个"转发"按钮（不带任何引导语、不带奖励）；其他页仍只用右上角菜单
    const buttons = (read(`${p}.wxml`).match(/open-type="share"/g) || []).length;
    assert.equal(buttons, p === 'pages/detail/index' ? 1 : 0, `${p} 转发按钮数量不对`);
  }
  // 我的客户、我的数据、AI 相关页不转发（D-070）
  for (const p of app.pages.filter((x) => /\/(clients|my-data|advisor[\w-]*)\//.test(x))) {
    assert.doesNotMatch(read(`${p}.js`), /onShareAppMessage|showShareMenu/, `${p} 不应能转发`);
  }
});

// ---------- 通知详情 ----------
function detailPage(detail, wx = {}) {
  return load('pages/detail/index.js', {
    '../../utils/api.js': { detail },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
    '../../utils/repost.js': require('../utils/repost.js'),
  }, { wx });
}
const DOC = { id: 128, kind: '申报通知', tags: ['申报通知'], title: '关于组织申报2026年度高新技术企业认定的通知', issuer: '山东省科学技术厅', date: '2026-10-15', points: [] };

test('通知详情：加载出来才能转发，标题"发文机关｜通知标题"，路径带文件编号；接收人不登录打开同一份', async () => {
  const menu = [];
  const wx = { showShareMenu: (o) => menu.push(`show:${o.menus}`), hideShareMenu: (o) => menu.push(`hide:${o.menus}`) };
  const asked = [];
  const detail = (id) => { asked.push(id); return Promise.resolve(plain(DOC)); };
  const sender = detailPage(detail, wx);
  sender.onLoad({ id: '128' });
  assert.deepEqual(menu, ['hide:shareAppMessage,shareTimeline']); // 还没加载出来：转发是灰的
  await flush();
  assert.deepEqual(menu, ['hide:shareAppMessage,shareTimeline', 'show:shareAppMessage']); // 只开"转发给朋友"
  const share = shareOf(sender);
  assert.deepEqual(share, { title: '山东省科学技术厅｜关于组织申报2026年度高新技术企业认定的通知', path: '/pages/detail/index?id=128' });
  for (const word of PROMO) assert.ok(!share.title.includes(word), word);
  const { page, query } = open(share.path);
  assert.equal(page, '/pages/detail/index');
  const receiver = detailPage(detail); // 接收人：没有登录、没有同意协议，也不经过首页
  receiver.onLoad(query); await flush();
  assert.equal(receiver.data.doc.title, DOC.title);
  assert.deepEqual(asked, ['128', '128']);
});

test('通知详情底栏"转发"按钮：加载中、打不开时不能点，加载成功才能点；按钮文字不带引导语（10/18 验收 9a）', async () => {
  const wxml = read('pages/detail/index.wxml');
  assert.match(wxml, /<button class="bar-item bar-share" open-type="share" disabled="\{\{!shareReady\}\}"[^>]*><t-icon name="share"[^>]*\/><text>转发<\/text><\/button>/);
  assert.doesNotMatch(wxml, /转发给同事|转发得|分享有礼/);
  let reply = () => Promise.reject(Object.assign(new Error('没有找到这份文件'), { status: 404 }));
  const page = detailPage(() => reply(), { showShareMenu() {}, hideShareMenu() {} });
  page.onLoad({ id: '999999' }); await flush();
  assert.equal(page.data.shareReady, false);
  reply = () => Promise.resolve(plain(DOC));
  page.refreshing = false; page.load(); await flush();
  assert.equal(page.data.shareReady, true);
  assert.equal(page.onShareAppMessage({ from: 'button' }).path, '/pages/detail/index?id=128');
});

test('通知详情：标题里已有发文机关不重复；太长截到 60 字；没有发文机关只用标题', async () => {
  const titleFor = async (doc) => {
    const page = detailPage(() => Promise.resolve(plain(doc)));
    page.onLoad({ id: String(doc.id) }); await flush();
    return shareOf(page).title;
  };
  assert.equal(await titleFor({ ...DOC, title: '山东省科学技术厅关于组织申报的通知' }), '山东省科学技术厅关于组织申报的通知');
  assert.equal(await titleFor({ ...DOC, issuer: '' }), DOC.title);
  const long = await titleFor({ ...DOC, title: `关于${'某某专项'.repeat(20)}的通知` });
  assert.equal(long.length, 60); assert.ok(long.startsWith('山东省科学技术厅｜关于某某专项')); assert.ok(long.endsWith('…'));
  // 截断处是表情等双字节字符时不截成半个（半个字符会让微信处理标题时出错）
  const emoji = await titleFor({ ...DOC, issuer: '', title: `${'甲'.repeat(58)}😀😀😀` });
  assert.equal(Array.from(emoji).length, 60); assert.equal(emoji, `${'甲'.repeat(58)}😀…`);
  assert.doesNotThrow(() => encodeURIComponent(emoji));
});

test('通知详情：打不开（下架、编号不对、网络出错）时转发一直是灰的；回到这页刷新时先变灰，刷新成功才恢复', async () => {
  const menu = [];
  const wx = { showShareMenu: () => menu.push('show'), hideShareMenu: () => menu.push('hide') };
  let reply = () => Promise.reject(Object.assign(new Error('没有找到这份文件'), { status: 404 }));
  const page = detailPage(() => reply(), wx);
  page.onLoad({ id: '999999' }); await flush();
  assert.deepEqual(menu, ['hide']);
  assert.equal(page.data.doc, null); assert.equal(page.data.error, '没有找到这份文件');
  reply = () => Promise.reject(Object.assign(new Error('网络连接失败'), { status: 0 }));
  page.refreshing = false; page.load(); await flush();
  assert.deepEqual(menu, ['hide', 'hide']);
  reply = () => Promise.resolve(plain(DOC));
  page.refreshing = false; page.load(); await flush();
  assert.deepEqual(menu, ['hide', 'hide', 'hide', 'show']);
  // 显示着的文件在刷新：请求还没回来时不能转发（文件可能刚下架），回来以后按结果
  let settle;
  reply = () => new Promise((resolve, reject) => { settle = { resolve, reject }; });
  page.lastShow = 0; page.onShow();
  assert.equal(menu.at(-1), 'hide'); assert.equal(page.data.doc.title, DOC.title);
  settle.reject(Object.assign(new Error('该文件已下架'), { status: 404 })); await flush();
  assert.equal(menu.at(-1), 'hide'); assert.equal(page.data.doc, null); assert.equal(page.data.takenDown, true);
  // 没有编号也不报错
  const none = detailPage(() => Promise.reject(Object.assign(new Error('没有找到这份文件'), { status: 404 })), wx);
  none.onLoad({}); await flush();
  assert.equal(none.data.error, '没有找到这份文件');
});

// ---------- 搜索结果 ----------
const COVERAGE = { cities: [{ province: '山东', city: '济南' }], districts: [{ province: '山东', city: '济南', district: '历下区' }] };
function searchPage(searches = []) {
  return load('pages/search/index.js', {
    '../../utils/api.js': { search: (params) => { searches.push(params); return Promise.resolve({ total: 0, total_all: 0, items: [] }); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': {},
    '../../utils/consent.js': { loginOpen: () => true, ensureConsent: () => Promise.resolve(true) },
    '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(plain(COVERAGE)), notCoveredNames: () => '', allUncovered: () => [] },
    '../../utils/conditions.js': conditions,
  }, { wx: {} });
}

test('搜索结果：转发路径带上全部检索条件，接收人打开后条件、请求和转发人一样；标题写明是搜索结果', async () => {
  const cases = [
    { q: '高新技术企业', kind: '申报通知', regions: '山东,江苏', types: '申报通知', time: '近 90 天', depts: '科技部门', open: 1 },
    { kind: 'all', regions: '国家部委,山东', levels: '国家部委,省级', time: '不限' },
    { kind: '申报通知', cities: '山东/济南', districts: '山东/济南/历下区', time: '近 30 天' },
    { kind: '名单', categories: '人才,试点示范', supports: '资金支持', industries: '数字经济', status: '公示', time: '今年' },
    { q: 'A&B 100% +专精特新=是', kind: '政策', time: '近 7 天' },
    { q: '降本%20增效 %E4%B8', kind: 'all', time: '不限' }, // 关键词里本来就有 %20、%E4 这样的文字
  ];
  for (const c of cases) {
    const sent = [];
    const sender = searchPage(sent);
    sender.onLoad(open(conditions.searchUrl(c)).query); await flush();
    const share = shareOf(sender);
    assert.equal(share.title, `搜索结果：${sender.followName()}`);
    for (const word of PROMO) assert.ok(!share.title.includes(word), word);
    const got = [];
    const { page, query } = open(share.path);
    assert.equal(page, '/pages/search/index');
    const receiver = searchPage(got);
    receiver.onLoad(query); await flush();
    assert.deepEqual(plain(receiver.conditions()), plain(sender.conditions()), JSON.stringify(c));
    assert.deepEqual(plain(got.at(-1)), plain(sent.at(-1)), JSON.stringify(c));
    if (c.q) assert.equal(receiver.data.q, c.q);
  }
});

test('搜索结果：条件再多也不删减，接收人看到的条件和转发人一样', async () => {
  const many = {
    q: '高新技术企业认定和科技型中小企业评价入库以及专精特新中小企业认定', kind: '申报通知', regions: regions.PROVINCES.join(','),
    categories: conditions.CATEGORIES.join(','), supports: conditions.SUPPORT_MODES.join(','), industries: conditions.INDUSTRIES.join(','),
    types: '申报通知,名单与公示', depts: '科技部门,工信部门', time: '近 90 天', open: 1,
  };
  const sent = [];
  const sender = searchPage(sent);
  sender.onLoad(open(conditions.searchUrl(many)).query); await flush();
  const share = shareOf(sender);
  assert.equal(share.path, conditions.searchUrl(sender.conditions()));
  assert.ok(share.path.length > 1000, share.path.length); // 这么多条件时路径很长，由真机验证能不能转发
  const got = [];
  const receiver = searchPage(got);
  receiver.onLoad(open(share.path).query); await flush();
  assert.deepEqual(plain(receiver.conditions()), plain(sender.conditions()));
  assert.deepEqual(plain(got.at(-1)), plain(sent.at(-1)));
});

test('搜索结果：卡片标题按字截到 40 个，关键词里有表情时不截出半个字符', async () => {
  const page = searchPage();
  page.onLoad({ q: encodeURIComponent(`${'甲'.repeat(38)}😀乙乙`) }); await flush();
  const { title } = shareOf(page);
  assert.equal(title, `搜索结果：“${'甲'.repeat(38)}😀`);
  assert.doesNotMatch(title, /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/);
  assert.doesNotThrow(() => encodeURIComponent(title));
});

test('搜索结果：网址参数编码坏了也能打开，不报错', async () => {
  const page = searchPage();
  assert.doesNotThrow(() => page.onLoad({ q: '%E4%B8', kind: '%E7', regions: '%', time: '%E8%BF%91%20' }));
  await flush();
  assert.equal(page.data.q, '%E4%B8'); assert.equal(page.data.tab, 'all'); assert.deepEqual(plain(page.data.picked.time), ['近 30 天']);
});

// ---------- 截止 ----------
function reportPage({ today = '2026-10-18', pages, wx } = {}) {
  const calls = [];
  const page = load('pages/report/index.js', {
    '../../utils/api.js': {
      deadlineReport: (params, opts) => { calls.push({ list: params, verifiedOnly: Boolean(opts && opts.verifiedOnly) }); return Promise.resolve({ items: [], total: 0, summary: { 7: 0, 14: 0, 30: 0 } }); },
      calendar: (params) => { calls.push({ month: params }); return Promise.resolve({ items: [], past: [], total: 0 }); },
    },
    '../../utils/exporter.js': {}, '../../utils/format.js': { ...format, todayISO: () => today },
    '../../utils/daywatch.js': daywatch, '../../utils/cache.js': noCache,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(plain(COVERAGE)) },
  }, { getApp: () => ({ globalData: { statusBarHeight: 20, navHeight: 44 } }), ...(pages ? { getCurrentPages: () => pages } : {}), ...(wx ? { wx } : {}) });
  page.calls = calls;
  return page;
}
const VIEW = ['tab', 'year', 'month', 'province', 'city', 'district', 'dept', 'verifiedOnly', 'areaText'];
const viewOf = (page) => Object.fromEntries(VIEW.map((k) => [k, page.data[k]]));

test('截止：平常打开是 7 天内；转发路径带天数、省市区、部门、只看已核对，接收人打开是同样的视图和请求', async () => {
  const plainPage = reportPage();
  plainPage.onLoad(); await flush();
  assert.deepEqual(shareOf(plainPage), { title: '7 天内截止的申报通知', path: '/pages/report/index?days=7' });

  const sender = reportPage();
  sender.onLoad({}); await flush();
  sender.pickTab(tap({ key: 14 })); await flush();
  sender.pickProvince({ detail: { value: 2 + regions.PROVINCES.indexOf('山东') } }); await flush();
  sender.chooseCity({ detail: { value: ['山东省', '济南市', '历下区'] } }); await flush();
  sender.pickDept({ detail: { value: 1 } }); await flush();
  sender.toggleVerified({ detail: { value: true } }); await flush();
  const share = shareOf(sender);
  assert.equal(share.title, '山东·济南·历下区 14 天内截止的申报通知（科技部门），只看已核对');
  for (const word of PROMO) assert.ok(!share.title.includes(word), word);
  assert.equal(share.path, `/pages/report/index?days=14&province=${encodeURIComponent('山东')}&city=${encodeURIComponent('济南')}&district=${encodeURIComponent('历下区')}&dept=${encodeURIComponent('科技部门')}&verified=1`);
  const receiver = reportPage();
  receiver.onLoad(open(share.path).query); await flush();
  assert.deepEqual(viewOf(receiver), viewOf(sender));
  assert.deepEqual(plain(receiver.calls.at(-1)), plain(sender.calls.at(-1)));
  assert.equal(shareOf(receiver).path, share.path);
});

test('截止：按月看转发当月，接收人打开同一个月；直辖市写区县；国家部委只带省份一项', async () => {
  const sender = reportPage();
  sender.onLoad({}); await flush();
  sender.pickTab(tap({ key: 0 })); await flush();
  sender.shiftMonth(tap({ step: 1 }));
  const share = shareOf(sender);
  assert.deepEqual(share, { title: '2026年11月截止的申报通知', path: '/pages/report/index?month=2026-11' });
  const receiver = reportPage();
  receiver.onLoad(open(share.path).query); await flush();
  assert.equal(receiver.data.tab, 0); assert.equal(receiver.data.year, 2026); assert.equal(receiver.data.month, 11);
  assert.ok(receiver.calls.at(-1).month, '按月看要取日历');

  const city = reportPage();
  city.onLoad({ days: '30', province: encodeURIComponent('北京'), city: encodeURIComponent('北京'), district: encodeURIComponent('朝阳区') }); await flush();
  assert.equal(city.data.district, '朝阳区'); assert.equal(city.data.areaText, '朝阳区');
  assert.equal(shareOf(city).title, '北京·朝阳区 30 天内截止的申报通知');
  const national = reportPage();
  national.onLoad({ province: encodeURIComponent('国家部委'), city: encodeURIComponent('济南') }); await flush();
  assert.deepEqual([national.data.province, national.data.city], ['国家部委', '']);
  assert.equal(shareOf(national).path, `/pages/report/index?days=7&province=${encodeURIComponent('国家部委')}`);
});

test('截止：不认识的参数不用、编码坏了不报错；月份超出能翻到的范围回到本月', async () => {
  const bad = reportPage();
  bad.onLoad({ days: '8', province: encodeURIComponent('火星'), city: encodeURIComponent('济南'), dept: encodeURIComponent('财政部门'), verified: 'yes' }); await flush();
  assert.deepEqual(viewOf(bad), { tab: 7, year: 2026, month: 10, province: '', city: '', district: '', dept: '', verifiedOnly: false, areaText: '' });
  const area = reportPage();
  area.onLoad({ province: encodeURIComponent('山东'), city: encodeURIComponent('济南,烟台'), district: encodeURIComponent('历下区') }); await flush();
  assert.deepEqual([area.data.province, area.data.city, area.data.district], ['山东', '', '']);
  for (const month of ['2020-01', '2026-13', '2026-1', 'abc']) {
    const page = reportPage();
    page.onLoad({ month }); await flush();
    assert.deepEqual([page.data.tab, page.data.year, page.data.month], [0, 2026, 10], month);
  }
  const broken = reportPage();
  assert.doesNotThrow(() => broken.onLoad({ province: '%E5%B1', days: '%', dept: '%E7%A7' }));
  await flush();
  assert.equal(broken.data.tab, 7); assert.equal(broken.data.province, '');
});

test('截止、查名单：从转发卡片打开（页面栈只有这一页）时左上角是"首页"，点了直接回首页；平常是返回箭头，点了返回上一页', async () => {
  for (const [file, wxml] of [['report', 'pages/report/index.wxml'], ['lists', 'pages/lists/index.wxml']]) {
    for (const [pages, icon, label, went] of [[[{}], 'home', '回首页', 'switchTab:/pages/home/index'], [[{}, {}], 'chevron-left', '返回', 'navigateBack']]) {
      const nav = [];
      const wx = { showToast() {}, switchTab: ({ url }) => nav.push(`switchTab:${url}`), navigateBack: () => nav.push('navigateBack') };
      const page = file === 'report' ? reportPage({ pages, wx }) : listsPage({ pages, wx });
      page.onLoad({}); await flush();
      const root = render(wxml, page.data);
      const back = findAll(root, hasClass('nav-back'));
      assert.equal(back.length, 1);
      assert.equal(back[0].attrs['aria-label'], label);
      assert.equal(findAll(back[0], (n) => n.tag === 't-icon')[0].attrs.name, icon);
      page[back[0].attrs.bindtap]();
      assert.deepEqual(nav, [went], `${file} ${label}`);
    }
  }
});

// ---------- 查名单 ----------
function listsPage({ lookup = () => new Promise(() => {}), pages, wx = { showToast() {} } } = {}) {
  const asked = [];
  const page = load('pages/lists/index.js', {
    '../../utils/api.js': { lists: () => new Promise(() => {}), listBoard: () => new Promise(() => {}), isLive: () => true, lookup: (q) => { asked.push(q); return lookup(q); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/cache.js': noCache,
  }, { getApp: () => ({ globalData: {} }), wx, ...(pages ? { getCurrentPages: () => pages } : {}) });
  page.asked = asked;
  return page;
}
const COMPANY = '演示科技有限公司';
const FOUND = { query: COMPANY, items: [{ id: '9', kind: '名单', title: '拟认定高新技术企业名单', matched: { name: COMPANY } }], similar: [], candidates: [], coverage: { lists: 10 } };
const GENERIC = { title: '在政府公开名单里查企业', path: '/pages/lists/index' };

test('查名单：查到了名单才带企业名称转发，接收人打开自动按这个名称查一次', async () => {
  const sender = listsPage({ lookup: () => Promise.resolve(plain(FOUND)) });
  sender.onLoad({});
  assert.deepEqual(shareOf(sender), GENERIC); // 还没查
  sender.onInput({ detail: { value: ` ${COMPANY} ` } }); sender.query();
  assert.deepEqual(shareOf(sender), GENERIC); // 正在查
  await flush();
  const share = shareOf(sender);
  assert.deepEqual(share, { title: `查名单：${COMPANY}`, path: `/pages/lists/index?q=${encodeURIComponent(COMPANY)}` });
  const receiver = listsPage({ lookup: () => Promise.resolve(plain(FOUND)) });
  receiver.onLoad(open(share.path).query); await flush();
  assert.deepEqual(receiver.asked, [COMPANY]);
  assert.equal(receiver.data.queried, COMPANY); assert.equal(receiver.data.result.items.length, 1);
  // 名称里连续的空白并成一个；超过 60 个字的不带名称
  const spaced = listsPage({ lookup: () => Promise.resolve(plain(FOUND)) });
  spaced.onLoad({}); spaced.onInput({ detail: { value: 'Demo   Tech \t Co., Ltd.' } }); spaced.query(); await flush();
  assert.deepEqual(shareOf(spaced), { title: '查名单：Demo Tech Co., Ltd.', path: `/pages/lists/index?q=${encodeURIComponent('Demo Tech Co., Ltd.')}` });
  const long = listsPage({ lookup: () => Promise.resolve(plain(FOUND)) });
  long.onLoad({}); long.onInput({ detail: { value: `演${' 示'.repeat(40)}` } }); long.query(); await flush();
  assert.deepEqual(shareOf(long), GENERIC);
});

test('查名单：没查到、只有相近名称、查询出错（如输入像个人姓名）时不带输入的内容；参数太短或坏了不报错', async () => {
  const replies = [
    () => Promise.resolve({ ...plain(FOUND), items: [] }),
    () => Promise.resolve({ ...plain(FOUND), items: [], candidates: [COMPANY] }),
    () => Promise.reject(new Error('只查企业和机构，不查个人')),
  ];
  for (const reply of replies) {
    const page = listsPage({ lookup: reply });
    page.onLoad({});
    page.onInput({ detail: { value: '张三' } }); page.query(); await flush();
    const share = shareOf(page);
    assert.deepEqual(share, GENERIC);
  }
  const short = listsPage();
  short.onLoad({ q: encodeURIComponent('演') });
  assert.deepEqual(short.asked, []);
  const tooLong = listsPage();
  tooLong.onLoad({ q: encodeURIComponent('演示'.repeat(31)) });
  assert.deepEqual(tooLong.asked, []); assert.equal(tooLong.data.name, '');
  const broken = listsPage();
  assert.doesNotThrow(() => broken.onLoad({ q: '%E6%BC' }));
});

// ---------- 首页"三步上手" ----------
function homePage({ open: loginOpen = true, stored = {}, readFails = false, writeFails = false } = {}) {
  const nav = [];
  const saved = [];
  const wx = {
    getStorageSync: (key) => { if (readFails) throw new Error('读不到'); return stored[key]; },
    setStorageSync: (key, value) => { if (writeFails) throw new Error('存不下'); saved.push([key, value]); stored[key] = value; },
    navigateTo: ({ url }) => nav.push(url),
  };
  const page = load('pages/home/index.js', {
    '../../utils/boost.js': boost,
    '../../utils/api.js': { home: () => new Promise(() => {}) }, '../../utils/format.js': format,
    '../../utils/daywatch.js': daywatch, '../../utils/cache.js': noCache, '../../utils/consent.js': { loginOpen: () => loginOpen },
  }, { getApp: () => ({ globalData: {} }), wx });
  Object.assign(page, { nav, saved, stored });
  return page;
}
const guideOf = (page) => findAll(render('pages/home/index.wxml', page.data), hasClass('guide'));

test('首页：第一次打开显示"三步上手"，在入口下面、本周新增上面；是普通卡片，不弹窗、不浮在内容上', () => {
  const page = homePage();
  page.onLoad();
  assert.equal(page.data.guide, true);
  const root = render('pages/home/index.wxml', page.data);
  const order = root.children.filter((n) => n.tag).map((n) => String(n.attrs.class || ''));
  const at = (name) => order.findIndex((c) => c.split(/\s+/).includes(name));
  assert.ok(at('entries') >= 0 && at('entries') < at('guide') && at('guide') < at('summary'), order.join(' | '));
  const [card] = guideOf(page);
  assert.equal(findAll(card, hasClass('guide-title')).map(textOf).join(), '三步上手');
  assert.deepEqual(findAll(card, hasClass('guide-name')).map(textOf), ['搜通知', '看截止和原文', '批量查客户（顾问用）']);
  assert.deepEqual(findAll(card, hasClass('guide-no')).map(textOf), ['1', '2', '3']);
  const text = textOf(card);
  for (const word of PROMO) assert.ok(!text.includes(word), `引导卡片含 ${word}`);
  assert.match(text, /需先同意协议，每月有免费次数/);
  // 样式：不用 fixed、absolute 之类浮在内容上
  const css = read('pages/home/index.wxss').split('\n').filter((line) => /^\.guide/.test(line)).join('\n');
  assert.ok(css.length > 0); assert.doesNotMatch(css, /position|z-index/);
  assert.equal(findAll(card, tapsTo('closeGuide')).length, 1);
  assert.match(textOf(findAll(card, tapsTo('closeGuide'))[0]), /不再显示/);
});

test('首页：三步各去对的页面——搜索页（按当前搜索范围）、截止页、工具中心', () => {
  const page = homePage();
  page.onLoad();
  page.goGuide(tap({ key: 'search' }));
  page.goGuide(tap({ key: 'due' }));
  page.goGuide(tap({ key: 'tools' }));
  page.goGuide(tap({ key: 'nothing' }));
  assert.deepEqual(page.nav, [`/pages/search/index?q=&kind=${encodeURIComponent('申报通知')}`, '/pages/report/index', '/pages/advisor/index']);
  page.pickScope(tap({ index: 2 })); page.goGuide(tap({ key: 'search' }));
  assert.equal(page.nav.at(-1), `/pages/search/index?q=&kind=${encodeURIComponent('政策')}`);
});

test('首页：点"不再显示"后本机记住，再打开不显示；读不到本机记录时照常显示，存不下也能关掉', () => {
  const page = homePage();
  page.onLoad();
  page.closeGuide();
  assert.equal(page.data.guide, false); assert.equal(guideOf(page).length, 0);
  assert.deepEqual(page.saved, [['youjuGuideClosedV1', 1]]);
  const again = homePage({ stored: page.stored });
  again.onLoad();
  assert.equal(again.data.guide, false); assert.equal(guideOf(again).length, 0);
  const unreadable = homePage({ readFails: true });
  unreadable.onLoad();
  assert.equal(unreadable.data.guide, true);
  const unwritable = homePage({ writeFails: true });
  unwritable.onLoad();
  assert.doesNotThrow(() => unwritable.closeGuide());
  assert.equal(unwritable.data.guide, false);
});

test('首页：协议没定稿的版本（没有工具中心入口）第三步去不用登录的"我的客户"，不出现工具中心', () => {
  const page = homePage({ open: false });
  page.onLoad();
  const [card] = guideOf(page);
  assert.ok(!textOf(card).includes('工具中心'));
  assert.ok(!textOf(card).includes('同意协议'));
  page.goGuide(tap({ key: 'clients' }));
  page.goGuide(tap({ key: 'tools' }));
  assert.deepEqual(page.nav, ['/pages/clients/index']);
});
