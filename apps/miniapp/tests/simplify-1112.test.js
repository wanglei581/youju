// 11/12 全站简化走查（只改字的这批）：创始人"不要过度设计复杂……整个小程序整体也是"。
// 原则：每屏只做一件事；解释放进使用帮助（D-108）；同一个东西只用一个名字。这里守住这批改动：
// 删掉的重复说明不再出现，合规要求的话（AI 生成标识、不判断能否申报、发布阶段、没查到不代表不在、隐私）还在页面上。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
// 只看会显示出来的部分：去掉 <!-- 注释 -->
const wxml = (page) => read(`pages/${page}/index.wxml`).replace(/<!--[\s\S]*?-->/g, '');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 6; i += 1) await flush(); };

// ---- 列表行 ----
function rowData(doc) {
  let def;
  vm.runInNewContext(read('components/doc-row/index.js'), {
    module: { exports: {} }, Date, Math,
    require(name) {
      if (name === '../../utils/format.js') return require('../utils/format.js');
      if (name === '../../utils/repost.js') return require('../utils/repost.js');
      throw new Error(name);
    },
    Component(d) { def = d; }, wx: { navigateTo() {} },
  });
  const c = { ...def, data: { ...(def.data || {}) }, properties: {}, setData(v) { Object.assign(this.data, v); } };
  c.observers['doc, q, today'].call(c, doc, '');
  return c.data;
}

test('列表行：申报通知不写状态标签（和截止日期、剩余天数说的是同一件事）；名单的发布阶段照写（D-090 ③）', () => {
  const apply = rowData({ id: '1', kind: '申报通知', title: '关于组织申报的通知', app_status: { code: 'closing' }, due: null });
  assert.equal(apply.statusText, '');
  assert.equal(rowData({ id: '1', kind: '申报通知', title: '关于组织申报的通知', status: '征集中' }).statusText, '');
  const list = rowData({ id: '2', kind: '名单', title: '关于公示拟认定企业名单的通知', status: '公示' });
  assert.equal(list.statusText, '公示');
  const row = (data, doc) => textOf(render('components/doc-row/index.wxml', { ...data, doc }));
  assert.doesNotMatch(row(apply, { kind: '申报通知' }), /征集中|即将截止|已截止/);
  assert.match(row(list, { kind: '名单' }), /公示/);
});

test('列表行：没有日期时仍写"截止时间见原文"、可能 7 天内截止的照旧提醒（D-061 第八节、D-108②，创始人定的写法，这批没改）', () => {
  assert.equal(rowData({ id: '1', kind: '申报通知', title: '甲', due: null }).dueNote, '截止时间见原文');
  assert.equal(rowData({ id: '1', kind: '申报通知', title: '甲', due: null, dueSoonUnverified: true }).dueNote, '可能 7 天内截止');
  assert.equal(rowData({ id: '1', kind: '申报通知', title: '甲', due: null, link_gone: '2026-10-27' }).dueNote, '');
});

// ---- 关注 ----
test('关注页：没订阅的关注不再每行写"未订阅微信提醒"，页底没有"微信提醒"说明卡；订阅了的写还剩几次', () => {
  const follows = [{ id: 1, name: '甲', tone: 'a', icon: 'search' }, { id: 2, name: '乙', tone: 'a', icon: 'search' }];
  const text = textOf(render('pages/follow/index.wxml', { loginOpen: true, mode: 'live', loading: false, error: '', remindOpen: true, follows, changes: [], remindLeft: { 1: 2, 2: 0 } }));
  assert.match(text, /微信提醒还剩 2 次/);
  assert.doesNotMatch(text, /未订阅微信提醒|按次订阅|想继续收到/);
  assert.equal((text.match(/微信提醒/g) || []).length, 1);
  // "提醒"按钮还在
  assert.equal(findAll(render('pages/follow/index.wxml', { loginOpen: true, mode: 'live', loading: false, error: '', remindOpen: true, follows, changes: [], remindLeft: {} }), hasClass('sub-remind')).length, 2);
});

test('使用帮助：查名单前三条说明和微信提醒怎么用写在帮助里；微信提醒没开通时帮助页不写后者', () => {
  const help = require('../content/help.js');
  const by = (key) => help.items.find((item) => item.key === key);
  const how = by('lists-how');
  assert.match(how.a.join(''), /逐行对照.*不一定是同一家.*看原名单里的这一行/);
  // D-118：除查名单页外，页面和帮助里不出现"比对"
  assert.ok(!JSON.stringify(how).includes('比对'));
  assert.equal(by('remind').login, true); assert.equal(by('remind').remind, true);
  assert.match(by('remind').a.join(''), /点某个关注旁的“提醒”.*提醒后再点一次.*还剩几次/);
  const keysWith = (open) => {
    let def;
    vm.runInNewContext(read('pages/help/index.js'), {
      require: (name) => ({
        '../../content/help.js': help,
        '../../utils/ai-access.js': { current: () => null, subscribe: () => () => {}, ensure() {} },
        '../../utils/consent.js': { loginOpen: () => true },
        '../../utils/subscribe.js': { remindOpen: () => open },
      })[name],
      Page: (d) => { def = d; }, wx: { setNavigationBarTitle() {}, pageScrollTo() {} },
    });
    return def.data.items.map((item) => item.key);
  };
  assert.ok(keysWith(true).includes('remind')); assert.ok(!keysWith(false).includes('remind'));
  assert.ok(keysWith(false).includes('lists-how'));
});

// ---- 搜索 ----
function searchPage(coverage, hasHelp = true) {
  let def; const urls = [];
  vm.runInNewContext(read('pages/search/index.js'), {
    module: { exports: {} }, Date, Math, Promise, setTimeout, clearTimeout,
    require(name) {
      const deps = {
        '../../utils/api.js': { search: () => Promise.resolve({ total: 0, total_all: 0, items: [] }) },
        '../../utils/regions.js': require('../utils/regions.js'),
        '../../utils/format.js': require('../utils/format.js'),
        '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
        '../../utils/exporter.js': { previewDocs() {}, MAX: 200 },
        '../../utils/consent.js': { loginOpen: () => true, ensureConsent: () => Promise.resolve(true) },
        '../../utils/subscribe.js': { remindOpen: () => false, requestOnce: () => Promise.resolve(false) },
        '../../utils/conditions.js': require('../utils/conditions.js'),
        '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(coverage), notCoveredNames: () => '', allUncovered: () => [] },
        '../../utils/ai-access.js': require('./helpers/ai-off.js'),
        '../../utils/pages.js': { flags: () => ({ help: hasHelp }) },
      };
      if (!Object.hasOwn(deps, name)) throw new Error(`缺少依赖 ${name}`);
      return deps[name];
    },
    Page(d) { def = d; }, wx: { navigateTo: ({ url }) => urls.push(url) },
  });
  return { page: { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(v) { Object.assign(this.data, JSON.parse(JSON.stringify(v))); } }, urls };
}

test('搜索：已收录的城市、区县个数不再写，没接入、没取到时才说；"全部"的范围有帮助页时只留"范围说明 ›"', async () => {
  const full = searchPage({ cities: [{ province: '山东', city: '烟台' }], districts: [{ province: '山东', city: '烟台', district: '市南区' }] });
  full.page.onLoad({}); await settle();
  assert.equal(full.page.data.cityCoverageNote, '');
  const none = searchPage({ cities: [], districts: [] });
  none.page.onLoad({}); await settle();
  assert.match(none.page.data.cityCoverageNote, /当前尚未接入市级来源.*当前尚未接入区县来源/);
  const missing = searchPage(null);
  missing.page.onLoad({}); await settle();
  assert.match(missing.page.data.cityCoverageNote, /城市收录范围未加载.*区县收录范围没有取到/);
  // 模板：没有话要说时连"刷新收录范围"也不出现
  const sheet = (data) => textOf(render('pages/search/index.wxml', { ...full.page.data, filterOpen: true, activeGroup: 'city', ...data }));
  assert.doesNotMatch(sheet({}), /刷新收录范围|已收录/);
  assert.match(sheet({ cityCoverageNote: '当前尚未接入市级来源。' }), /当前尚未接入市级来源。 刷新收录范围/);
  // 范围说明：有帮助页跳到"全部"那一条，没有帮助页（过审版）时不显示入口、照旧写那一句
  assert.match(sheet({ has: { help: true } }), /范围说明 ›/);
  assert.doesNotMatch(sheet({ has: { help: true } }), /城市选“全部”查全省/);
  assert.match(sheet({ has: { help: false } }), /城市选“全部”查全省；区县选“全部”只查这个市的市级来源，直辖市查整个直辖市。/);
  assert.doesNotMatch(sheet({ has: { help: false } }), /范围说明 ›/);
  full.page.help(); assert.deepEqual(full.urls, ['/pages/help/index?topic=region']);
  const bare = searchPage({ cities: [], districts: [] }, false);
  bare.page.onLoad({}); await settle(); bare.page.help(); assert.deepEqual(bare.urls, []);
  const help = require('../content/help.js');
  assert.ok(help.items.some((item) => item.key === 'region'), '帮助里有"全部是什么范围"这一条');
});

test('搜索结果条：不再写"按发布时间 ↓"；选择条的按钮叫"导出"', () => {
  const text = textOf(render('pages/search/index.wxml', { loading: false, error: '', total: 3, totalAll: 9, chipTime: '近 30 天', items: [], activeChips: [], picked: { open: [] }, fold: { text: '' }, entries: {}, groups: [], selecting: true, chosenCount: 2, has: {} }));
  assert.doesNotMatch(text, /按发布时间/);
  assert.match(text, /已选 2 份 全选已加载 导出/);
  assert.doesNotMatch(text, /查看结果/);
});

// ---- 首页、我的、关于：一个名字 ----
test('首页：本周新增那块不再写"来自 N 个省和国家部委"；"覆盖说明"改叫"收录范围"；政策的搜索提示不写"政策文件"', () => {
  const data = { stats: { week_total: 5, apply: 3, lists: 1, other: 1, provinces: 28, cities: 2, sources: 72 }, loaded: true, items: [], loginOpen: false, scopes: [], scope: 0 };
  const week = textOf(render('pages/home/index.wxml', data));
  assert.doesNotMatch(week, /个省和国家部委|个城市/);
  assert.match(week, /申报 3 · 名单与公示 1 · 其他 1/);
  assert.match(week, /数据来自 72 个政府网站栏目，每条都注明出处。有据不代办申报。/);
  assert.match(week, /收录范围 ›/); assert.doesNotMatch(week, /覆盖说明/);
  const zero = textOf(render('pages/home/index.wxml', { ...data, stats: { ...data.stats, week_total: 0 }, checkedText: '11月12日 06:30' }));
  assert.match(zero, /本周暂无新文件 · 最近检查 11月12日 06:30/); assert.doesNotMatch(zero, /个省和国家部委/);
  assert.match(read('pages/home/index.js'), /label: '查政策', placeholder: '搜政策，如“数字经济”'/);
});

test('"申请收录"：我的、搜索、关于有据、反馈页都叫"收录"；提交时加的前缀不变（后台按它认出申请）', () => {
  for (const file of ['pages/me/index.wxml', 'pages/search/index.wxml', 'pages/about/index.wxml', 'pages/feedback/index.wxml', 'pages/home/index.wxml']) {
    assert.ok(!read(file).replace(/<!--[\s\S]*?-->/g, '').includes('申请覆盖'), file);
  }
  assert.match(wxml('me'), /申请收录：你关心的地区还没有？/);
  assert.match(wxml('about'), /在“我的 → 申请收录”告诉我们/);
  const feedback = read('pages/feedback/index.js');
  assert.match(feedback, /setNavigationBarTitle\(\{ title: '申请收录' \}\)/);
  assert.match(feedback, /const COVERAGE_PREFIX = '申请覆盖：';/);
  // 页面上不再写"最多 495 字，提交时会自动加上……"（输入框右下角有 x/495）
  assert.doesNotMatch(wxml('feedback'), /自动加上/);
  assert.match(wxml('feedback'), /<view class="count weak">{{count}}\/{{max}}<\/view>/);
});

// ---- 详情、导出 ----
test('详情页：支持方式、申报对象、申报条件的核对说明合成一句写在原文出处里；底栏叫"导出"；名单入口统一写"去查名单 ›"；入口按去处起名', () => {
  const doc = { id: '1', title: '关于公示拟认定企业名单的通知', issuer: '科技厅', date: '2026-10-01', fields: { support: ['资金补助'], audience: { text: '注册在本省的企业' } }, body: { status: 'list' } };
  const data = { doc, loading: false, has: { help: true }, conds: [], checkedText: '', noBody: '这份通知的正文里有名单' };
  const text = textOf(render('pages/detail/index.wxml', data));
  assert.equal(text.split('支持方式、申报对象、申报条件只显示已核对原文的。').length - 1, 1);
  assert.doesNotMatch(text, /申报对象（已核对原文）|申报条件（已核对原文）|支持方式已核对原文/);
  assert.match(text, /支持方式、申报对象、申报条件只显示已核对原文的。内容以原文为准；申报请在主管部门的申报系统办理，有据不提供填报入口。/);
  assert.match(text, /去查名单 ›/); assert.doesNotMatch(text, /按企业名称查询/);
  assert.match(text, /内容怎么来的 ›/); assert.doesNotMatch(text, /数据说明/);
  assert.match(text, /内容怎么来的 › 导出 转发/);
  assert.doesNotMatch(text, /结果/);
  // 没有这三张卡片时不写它们的核对说明；有核对日期时两句并排
  const bare = textOf(render('pages/detail/index.wxml', { doc: { id: '1', title: 't', issuer: '厅', date: '2026-10-01' }, loading: false, has: {}, conds: [], checkedText: '2026年11月12日' }));
  assert.doesNotMatch(bare, /支持方式、申报对象、申报条件只显示已核对原文的/); assert.match(bare, /截止日期已于 2026年11月12日 对照原文核对。内容以原文为准/);
  // 只有申报条件时也写
  const conds = textOf(render('pages/detail/index.wxml', { doc: { id: '1', title: 't', issuer: '厅', date: '2026-10-01' }, loading: false, has: {}, conds: [{ category: '主体资格', quote: '注册在本省', showCat: true }], checkedText: '' }));
  assert.match(conds, /支持方式、申报对象、申报条件只显示已核对原文的。/);
  // 发布阶段的标签 D-090 ③ 没动
  assert.match(wxml('detail'), /class="tag">发布阶段：{{doc.status}}</);
});

test('截止页：选了城市区县时，有帮助页只写"没收录的地方查不到，不代表当地没有通知"，没有帮助页（过审版）时照旧多写"全部"的范围；不写"（按月看不筛部门）"；每行叫"导出"', () => {
  const note = (has) => findAll(render('pages/report/index.wxml', { city: '烟台', district: '', tab: 0, has }), hasClass('cached-note')).map(textOf);
  assert.deepEqual(note({ help: true }), ['没收录的地方查不到，不代表当地没有通知。']);
  assert.deepEqual(note({ help: false }), ['城市选“全部”查全省；区县选“全部”只查这个市的市级来源，直辖市查整个直辖市。没收录的地方查不到，不代表当地没有通知。']);
  assert.deepEqual(note({ help: true }).concat(findAll(render('pages/report/index.wxml', { city: '', district: '', has: {} }), hasClass('cached-note')).map(textOf)), ['没收录的地方查不到，不代表当地没有通知。']);
  const page = textOf(render('pages/report/index.wxml', { tab: 0, has: {}, rows: [{ key: 'a', docId: '1', title: '甲', kindText: '企业申报截止', dateText: '2026-12-01', week: '周二', days: 19, tone: 'normal' }] }));
  assert.doesNotMatch(page, /按月看不筛部门|查看结果/);
  assert.match(page, /只看已核对/);
  assert.match(page, /导出/);
});

test('查名单：命中的一行平时只留"名单中写作……"和批次，有同名不同代码或没有原名单入口时照旧列全；查询说明只挪走一条；阅读页的按钮叫"去查名单"', () => {
  const item = (matched) => ({ id: '9', kind: '名单', title: '某名单', matched });
  const hit = (matched, result = {}) => textOf(findAll(render('pages/lists/index.wxml', { queried: '某某科技有限公司', result: { ...result, items: [item(matched)], similar: [], coverage: { lists: 3 } }, items: [], shown: [], chips: [] }), hasClass('hit-line'))[0]);
  const full = { name: '某某科技有限公司', category: '专精特新', batch: '第二批', sequence: '12', region: '烟台市', creditCode: '91000000MA00000000', via: 'name', entryToken: 'x' };
  assert.equal(hit(full), '名单中写作“某某科技有限公司” · 第二批');
  assert.equal(hit({ ...full, batch: '', via: 'creditCode', name: '某某科技（原名）' }), '名单中写作“某某科技（原名）”（信用代码相同）');
  // 同名不同代码：用代码才分得开；没有原名单入口：读不到那一行时阅读页的兜底没有序号和信用代码——这两种照旧列全
  const everything = '名单中写作“某某科技有限公司” · 专精特新 · 第二批 · 序号 12 · 烟台市 · 信用代码 91000000MA00000000';
  assert.equal(hit(full, { ambiguous: true }), everything);
  assert.equal(hit({ ...full, entryToken: '' }), everything);
  const lists = wxml('lists');
  assert.ok(!lists.includes('查到后可以看这家企业在原名单里的那一行'));
  assert.match(lists, /按企业全称逐行比对名单原文/); assert.match(lists, /名称相近的企业分开列出，不一定是同一家/);
  assert.match(lists, /只查企业和机构，不查个人/); assert.match(lists, /是否还在公示期以原文为准/);
  assert.match(lists, /名称相近的企业（不一定是同一家，点名称按全称查）/);
  assert.match(wxml('reader'), />去查名单<\/button>/);
});

test('结果页：没有的项不写"未提供"；去阅读页的按钮叫"看正文"；去帮助的入口叫"截止日期怎么看 ›"', () => {
  const result = { total: 1, generatedAtText: '2026年11月12日', items: [{ number: 1, doc_id: '1', title: '甲', metaText: '申报通知', siteText: '某厅', issuer: '', doc_number: '', published: '' }] };
  const text = textOf(render('pages/results/index.wxml', { advisor: false, result, has: { help: true } }));
  assert.doesNotMatch(text, /未提供|发文机关|文号|发布日期/);
  assert.match(text, /看正文/); assert.doesNotMatch(text, /站内阅读正文和依据/);
  assert.match(text, /截止日期怎么看 ›/);
  const only = textOf(render('pages/results/index.wxml', { advisor: false, result: { ...result, items: [{ ...result.items[0], doc_number: '某〔2026〕1号', published: '2026-10-01' }] }, has: {} }));
  assert.match(only, /文号：某〔2026〕1号 · 发布日期：2026-10-01/);
});

test('文件页和我的客户：导出记录的按钮叫"查看"；客户页的隐私说明"只保存在本机"只写一遍，导出的表里没有什么另说；按钮叫"导出这 N 份"', () => {
  const files = textOf(render('pages/files/index.wxml', { files: [{ id: 'a', name: 'x.xlsx', badge: 'XLSX', meta: '3 份' }], demo: false }));
  assert.match(files, /XLSX x\.xlsx 3 份 查看 转发/); assert.doesNotMatch(files, /站内查看/);
  const clients = textOf(render('pages/clients/index.wxml', { tab: 'docs', loaded: true, loading: false, clients: [{ id: 'c1', displayName: '甲' }], rows: [1], shown: [{ id: '1', doc: { id: '1' }, names: '甲' }], editing: false }));
  assert.equal((clients.match(/只保存在本机|只存在本机/g) || []).length, 1);
  assert.match(clients, /这里的客户卡片只保存在本机，不上传；检索只发送所选地区（省、市、区县）和产业方向。/);
  assert.match(clients, /导出这 1 份 导出的表里不含客户名称。/);
  // 两句"不判断客户能不能申报"一句在没有客户时、一句在有客户时，不会同屏
  const empty = textOf(render('pages/clients/index.wxml', { tab: 'docs', loaded: true, clients: [], editing: false }));
  assert.equal((empty.match(/不判断客户能不能申报/g) || []).length, 1);
  assert.equal((clients.match(/不判断客户能不能申报/g) || []).length, 1);
});

test('批量工具表单：计次规则只在提交按钮上面写一次，先预览后提交只在灰着的提交按钮上面写一次', () => {
  const form = wxml('advisor-form');
  assert.equal((form.match(/每提交一次算 1 次/g) || []).length, 1);
  assert.ok(!form.includes('{{countRule}}'));
  assert.equal((form.match(/预览通过后才能提交/g) || []).length, 1);
  assert.ok(!form.includes('先点上面的'));
});

test('AI 首页：页头那句说明去掉了，AI 生成标识、"说明"（适用人群、场合、用途）、模型和备案号、撤回同意、投诉举报都还在', () => {
  const ai = wxml('ai');
  assert.ok(!ai.includes('在通知原文里找出申报对象') && !ai.includes('hero-sub'));
  for (const text of ['AI 生成 · 以原文为准', "{{scopeOpen ? '收起' : '说明'}}", '{{disclosure}}', '撤回同意', '投诉举报 ›', '使用帮助 ›']) assert.ok(ai.includes(text), text);
  for (const item of ['{{item}}', '{{keep}}']) assert.ok(ai.includes(item), item);
});

test('我的数据：自己提交过的“申请收录”在列表里也叫“申请收录”；提交时加的前缀“申请覆盖：”不变，复制完整记录用原始内容', async () => {
  const raw = { user: { openid: 'synthetic-openid', agreed: null }, follows: [], subscriptions: [], advisorTasks: [], advisorTasksTotal: 0, retention: {},
    feedback: [
      { id: 1, docId: null, content: '申请覆盖：增加某省科技厅通知公告栏目', checkResult: 'pass', handled: false, createdAt: '2026-11-12T10:00:00+08:00' },
      { id: 2, docId: 7, docTitle: '某通知', content: '截止日期和原文不一致，申请覆盖：这句不在开头', checkResult: 'pass', handled: false, createdAt: '2026-11-12T11:00:00+08:00' },
    ] };
  const copied = [];
  let def;
  vm.runInNewContext(read('pages/my-data/index.js'), {
    module: { exports: {} }, setTimeout, clearTimeout, Date, Promise,
    require: (name) => ({
      '../../utils/api.js': { myData: () => Promise.resolve(JSON.parse(JSON.stringify(raw))) },
      '../../utils/conditions.js': require('../utils/conditions.js'),
      '../../utils/consent.js': { loginOpen: () => true, remindAny: () => false, withdrawAccount: () => Promise.resolve(false) },
      '../../utils/advisor.js': require('../utils/advisor.js'),
      '../../utils/pages.js': require('../utils/pages.js'),
    })[name],
    Page(d) { def = d; }, wx: { setClipboardData: ({ data }) => copied.push(JSON.parse(data)), showToast() {} },
  });
  const page = { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(v) { Object.assign(this.data, JSON.parse(JSON.stringify(v))); } };
  page.load(); await settle();
  assert.deepEqual(page.data.feedback.map((f) => f.content), ['申请收录：增加某省科技厅通知公告栏目', '截止日期和原文不一致，申请覆盖：这句不在开头']);
  page.copyOne({ currentTarget: { dataset: { kind: 'feedback', id: 1 } } });
  assert.equal(copied[0].content, '申请覆盖：增加某省科技厅通知公告栏目');
});
