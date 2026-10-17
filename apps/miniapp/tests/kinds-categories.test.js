const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const { mockSearch } = require('../utils/mockSearch.js');

// 资讯与指南默认折叠、项目类别筛选和免责声明（D-051 第二阶段）。依赖都用替身，不读取 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout,
    require(name) { if (name === '../../utils/regions.js') return require('../utils/regions.js'); assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  return def ? { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, values); } } : module.exports;
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const event = (dataset) => ({ currentTarget: { dataset } });
const daywatch = { watchDay() {}, stopDay() {} };
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const FORBIDDEN = ['AI', '人工智能', '智能', '大模型', '签约', '分享'];
const DISCLAIMER = '仅供检索参考，非官方分类';

function searchPage(respond) {
  const calls = [];
  const page = load('pages/search/index.js', {
    '../../content/legal.js': { final: false },
    '../../utils/api.js': { search: (p) => { calls.push(p); return Promise.resolve(respond(p)); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': {},
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/subscribe.js': { remindOpen: () => true }, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: {} });
  return { page, calls };
}
const foldLine = (data) => `${data.fold.text}${data.fold.link}`;

test('资讯与指南默认折叠：写明折叠了几篇，点开列出来，再点收起', async () => {
  const { page, calls } = searchPage((p) => (p.fold === 1
    ? { total: 5, total_all: 9, folded: 3, guides: 3, items: [] }
    : { total: 8, total_all: 12, folded: 0, guides: 3, items: [] }));
  page.onLoad({}); await flush();
  assert.equal(calls.at(-1).fold, 1);
  assert.equal(foldLine(page.data), '已折叠 3 篇非申报类文件（资讯与指南），点开查看');
  page.toggleGuides(); await flush();
  assert.equal(calls.at(-1).fold, 0);
  assert.equal(page.data.total, 8);
  assert.equal(foldLine(page.data), '已展开 3 篇非申报类文件（资讯与指南），收起');
  page.toggleGuides(); await flush();
  assert.equal(calls.at(-1).fold, 1);
  assert.equal(page.data.folded, 3);
  // 折叠不是筛选条件，不进关注
  assert.equal(page.conditions().fold, undefined);
  const template = read('pages/search/index.wxml');
  assert.match(template, /wx:if="{{!loading && !error && fold.text}}"[^>]*bindtap="toggleGuides"[^>]*>{{fold.text}}<text class="link">{{fold.link}}<\/text>/);
});

test('没有可折叠的、或已经点开但列表里没有时，不写折叠说明', async () => {
  const { page } = searchPage(() => ({ total: 2, total_all: 2, folded: 0, guides: 0, items: [] }));
  page.onLoad({}); await flush();
  assert.equal(foldLine(page.data), '');
  page.toggleGuides(); await flush();
  assert.equal(foldLine(page.data), '');
  // 旧接口没有这两个字段
  const legacy = searchPage(() => ({ total: 2, total_all: 2, items: [] }));
  legacy.page.onLoad({}); await flush();
  assert.equal(foldLine(legacy.page.data), '');
});

test('类型筛选多了资讯与指南；演示数据的"其他"不含资讯与指南，折叠与服务端一致', () => {
  const { page } = searchPage(() => ({ total: 0, total_all: 0, items: [] }));
  const type = page.data.groups.find((g) => g.key === 'type');
  assert.deepEqual(Array.from(type.options), ['申报通知', '名单与公示', '政策文件', '资讯与指南', '其他']);
  const docs = [
    { title: 'a', kind: '申报通知', region: '山东 · 省级', date: '2026-09-01', category: '人才' },
    { title: 'b', kind: '资讯与指南', region: '山东 · 省级', date: '2026-09-01' },
    { title: 'c', kind: '其他', region: '山东 · 省级', date: '2026-09-01' },
    { title: 'd', kind: '政策', region: '山东 · 省级', date: '2026-09-01' },
  ];
  const run = (params) => mockSearch(docs, { time: '不限', ...params });
  assert.deepEqual(run({ types: '其他' }).items.map((d) => d.title), ['c']);
  assert.deepEqual(run({ types: '资讯与指南', fold: 1 }).items.map((d) => d.title), ['b']);
  const folded = run({ fold: 1 });
  assert.deepEqual(folded.items.map((d) => d.title), ['a', 'c', 'd']);
  assert.equal(folded.folded, 1);
  assert.equal(run({ fold: 0 }).items.length, 4);
  assert.equal(run({ fold: 1, kind: '申报通知' }).folded, 0);
  assert.deepEqual(run({ categories: '人才' }).items.map((d) => d.title), ['a']);
});

test('项目类别筛选：8 类可多选，写明仅供检索参考、非官方分类；条件带到接口、关注和返回搜索', async () => {
  const { page, calls } = searchPage(() => ({ total: 0, total_all: 0, items: [] }));
  page.onLoad({}); await flush();
  const group = page.data.groups.find((g) => g.key === 'category');
  assert.equal(group.title, '项目类别');
  assert.equal(group.multi, true);
  assert.deepEqual(Array.from(group.options), ['资质与梯度培育', '科技计划与研发', '平台载体与园区', '人才', '表彰奖励', '首台套与新产品', '试点示范', '其他申报']);
  // 说明拆成两段：右栏顶部统一的一行（tagged）+ 选项下面这组特有的补充
  assert.equal(group.tagged, true);
  assert.ok(page.data.tagNote.includes(DISCLAIMER));
  assert.equal(group.note, '只含申报通知和名单');
  assert.equal(calls.at(-1).categories, undefined); // 没选时不带，和以前保存的关注条件一致
  page.pickOption(event({ group: 'category', option: '人才' })); await flush();
  page.pickOption(event({ group: 'category', option: '表彰奖励' })); await flush();
  assert.equal(calls.at(-1).categories, '人才,表彰奖励');
  assert.equal(page.conditions().categories, '人才,表彰奖励');
  assert.match(page.followName(), /人才、表彰奖励/);
  page.pickOption(event({ group: 'category', option: '人才' })); await flush();
  assert.equal(calls.at(-1).categories, '表彰奖励');
  page.resetFilter(); await flush();
  assert.equal(calls.at(-1).categories, undefined);
  // 返回搜索时恢复项目类别；关注列表里的说明写出类别
  const restored = searchPage(() => ({ total: 0, total_all: 0, items: [] }));
  const url = new URL(conditions.searchUrl({ kind: 'all', categories: '试点示范', time: '不限' }), 'https://test.invalid');
  restored.page.onLoad(Object.fromEntries(url.searchParams)); await flush();
  assert.equal(restored.calls.at(-1).categories, '试点示范');
  assert.equal(conditions.describe({ kind: 'all', categories: '试点示范,人才', time: '不限' }), '不限地区 · 全部类型 · 项目类别：试点示范、人才 · 不限');
  // 筛选弹层把说明显示在选项下面
  assert.match(read('pages/search/index.wxml'), /wx:if="{{g.note}}" class="group-note">{{g.note}}/);
});

test('只选政策或别的类型再选项目类别：空结果里提示项目类别只用于申报通知和名单', async () => {
  const c = conditions.categoryConflict;
  assert.equal(c('all', '', ''), false);
  assert.equal(c('all', '', '人才'), false);
  assert.equal(c('政策', '', '人才'), true);
  assert.equal(c('all', '政策文件,其他', '人才'), true);
  assert.equal(c('all', '政策文件,名单与公示', '人才'), false);
  const { page } = searchPage(() => ({ total: 0, total_all: 0, items: [] }));
  page.onLoad({ kind: '政策', categories: '人才' }); await flush();
  assert.match(page.data.emptyHint, /项目类别只用于申报通知和名单/);
  page.onLoad({ categories: '人才' }); await flush();
  assert.equal(page.data.emptyHint, '');
});

test('详情页：申报通知和名单显示项目类别；类型、类别和名单状态合成一行说明，写明仅供检索参考、非官方分类', async () => {
  let doc = { id: '1', kind: '申报通知', tags: ['申报通知'], category: '资质与梯度培育', title: '关于组织开展高新技术企业认定工作的通知', deadline_state: 'none' };
  const page = load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { detail: () => Promise.resolve(JSON.parse(JSON.stringify(doc))) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/repost.js': require('../utils/repost.js'),
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { wx: {} });
  page.onLoad({ id: '1' }); await flush();
  assert.equal(page.data.doc.category, '资质与梯度培育');
  doc = { ...doc, kind: '政策', tags: ['政策'], category: null };
  page.refreshing = false; page.load(); await flush();
  assert.equal(page.data.doc.category, null);
  const template = read('pages/detail/index.wxml');
  assert.match(template, /<text wx:if="{{doc.category}}" class="tag tag--cat">{{doc.category}}<\/text>/);
  // 标题下只有一行说明（D-053 UI 第一轮）：不再分"名单状态""项目类别"两三行
  assert.match(template, new RegExp(`<view class="weak status-note">类型、类别和名单状态按标题归类，${DISCLAIMER}</view>`));
  assert.equal((template.match(/status-note/g) || []).length, 1);
});

test('关注的新文件：同样折叠资讯与指南，写明篇数，可以点开', async () => {
  const calls = [];
  const page = load('pages/follow-new/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { followNew: (id, p) => { calls.push(p); return Promise.resolve(p.fold === 1 ? { total: 1, folded: 2, guides: 2, items: [] } : { total: 3, folded: 0, guides: 2, items: [] }); } },
    '../../utils/format.js': format, '../../utils/conditions.js': conditions,
  }, { wx: { setNavigationBarTitle() {} } });
  page.onLoad({ id: '7' }); await flush();
  assert.equal(calls.at(-1).fold, 1);
  assert.equal(foldLine(page.data), '已折叠 2 篇非申报类文件（资讯与指南），点开查看');
  page.toggleGuides(); await flush();
  assert.equal(calls.at(-1).fold, 0);
  assert.equal(foldLine(page.data), '已展开 2 篇非申报类文件（资讯与指南），收起');
  const template = read('pages/follow-new/index.wxml');
  assert.match(template, /上次查看之后有 {{total \+ folded}} 份新文件/);
  assert.match(template, /bindtap="toggleGuides"[^>]*>{{fold.text}}<text class="link">{{fold.link}}<\/text>/);
});

test('新加的界面文字不出现禁用字样（含支持方式、产业方向）', () => {
  const texts = [
    conditions.CATEGORY_NOTE, ...conditions.CATEGORIES, conditions.GUIDE,
    conditions.SUPPORT_NOTE, ...conditions.SUPPORT_MODES, conditions.INDUSTRY_NOTE, ...conditions.INDUSTRIES,
    conditions.describe({ kind: 'all', supports: conditions.SUPPORT_MODES.join(','), industries: conditions.INDUSTRIES.join(','), time: '不限' }),
    read('pages/search/index.js'),
    foldLine({ fold: conditions.foldNote(3, 0) }), foldLine({ fold: conditions.foldNote(0, 3) }),
    read('pages/detail/index.wxml'), read('pages/follow-new/index.wxml'), read('pages/search/index.wxml'),
  ];
  for (const text of texts) {
    for (const word of FORBIDDEN) assert.ok(!text.includes(word), `${word} 出现在：${text.slice(0, 40)}`);
  }
});
