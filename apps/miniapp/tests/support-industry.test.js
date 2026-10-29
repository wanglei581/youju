const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const { mockSearch } = require('../utils/mockSearch.js');

// 支持方式、产业方向筛选（D-051 第三阶段）：按标题归类，只用于筛选，写明仅供检索参考、非官方分类；
// 列表和详情里不显示，详情页的支持方式只来自核对原文。依赖都用替身，不读取 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout,
    require(name) { if (name === '../../utils/regions.js') return require('../utils/regions.js'); if (!Object.hasOwn(deps, name) && /(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages; if (!Object.hasOwn(deps, name) && /ai-access\.js$/.test(name)) return AI_OFF; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  return def ? { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, values); } } : module.exports;
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const event = (dataset) => ({ currentTarget: { dataset } });
const daywatch = { watchDay() {}, stopDay() {} };
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const DISCLAIMER = '按标题归类，仅供检索参考，非官方分类';
const SUPPORTS = ['资金支持', '贷款贴息', '资格认定', '入库备案', '项目立项', '征集入选'];
const INDUSTRIES = ['数字经济', '先进制造', '新材料与化工', '生物医药与健康', '新能源与绿色低碳', '电子信息与集成电路', '现代农业', '文旅与现代服务业'];

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

test('筛选弹层有支持方式（6 项，用详情核对字段的词）和产业方向（8 项），都可多选，写明按标题归类、仅供检索参考、非官方分类', () => {
  const { page } = searchPage(() => ({ total: 0, total_all: 0, items: [] }));
  const support = page.data.groups.find((g) => g.key === 'support');
  const industry = page.data.groups.find((g) => g.key === 'industry');
  assert.equal(support.title, '支持方式');
  assert.equal(industry.title, '产业方向');
  assert.equal(support.multi, true);
  assert.equal(industry.multi, true);
  assert.deepEqual(Array.from(support.options), SUPPORTS);
  assert.deepEqual(Array.from(industry.options), INDUSTRIES);
  // 10/29 创始人："按标题归类、仅供检索参考、非官方分类""不代表一定提供这种支持"写在使用帮助里，面板里只写各组特有的补充
  assert.equal(support.tagged, true);
  assert.equal(industry.tagged, true);
  assert.equal(page.data.tagNote, undefined);
  assert.equal(industry.note, undefined);
  assert.equal(support.note, '只含申报通知和名单');
  const tags = require('../content/help.js').items.find((i) => i.key === 'tags').a.join('');
  assert.ok(tags.includes(DISCLAIMER)); assert.match(tags, /不代表一定提供这种支持/);
  // 筛选弹层把说明显示在选项下面（与项目类别同一个模板）
  assert.match(read('pages/search/index.wxml'), /wx:if="{{g.note}}" class="group-note">{{g.note}}/);
});

test('多选的支持方式和产业方向带到接口、关注名称、关注说明，重置后清空，返回搜索时恢复', async () => {
  const { page, calls } = searchPage(() => ({ total: 0, total_all: 0, items: [] }));
  page.onLoad({}); await flush();
  // 没选时不带，和以前保存的关注条件一致
  assert.equal(calls.at(-1).supports, undefined);
  assert.equal(calls.at(-1).industries, undefined);
  page.pickOption(event({ group: 'support', option: '资金支持' })); await flush();
  page.pickOption(event({ group: 'support', option: '贷款贴息' })); await flush();
  page.pickOption(event({ group: 'industry', option: '先进制造' })); await flush();
  page.pickOption(event({ group: 'industry', option: '数字经济' })); await flush();
  assert.equal(calls.at(-1).supports, '资金支持,贷款贴息');
  assert.equal(calls.at(-1).industries, '先进制造,数字经济');
  assert.equal(page.conditions().supports, '资金支持,贷款贴息');
  assert.match(page.followName(), /资金支持、贷款贴息/);
  page.pickOption(event({ group: 'support', option: '资金支持' })); await flush();
  assert.equal(calls.at(-1).supports, '贷款贴息');
  page.resetFilter(); await flush();
  assert.equal(calls.at(-1).supports, undefined);
  assert.equal(calls.at(-1).industries, undefined);
  const c = { kind: 'all', supports: '资格认定,征集入选', industries: '现代农业', time: '不限' };
  assert.equal(conditions.describe(c), '不限地区 · 全部类型 · 支持方式：资格认定、征集入选 · 产业方向：现代农业 · 不限');
  const restored = searchPage(() => ({ total: 0, total_all: 0, items: [] }));
  const url = new URL(conditions.searchUrl(c), 'https://test.invalid');
  restored.page.onLoad(Object.fromEntries(url.searchParams)); await flush();
  assert.equal(restored.calls.at(-1).supports, '资格认定,征集入选');
  assert.equal(restored.calls.at(-1).industries, '现代农业');
});

test('只选政策再选支持方式：空结果里提示支持方式只用于申报通知和名单；产业方向不受类型限制', async () => {
  assert.equal(conditions.supportConflict('政策', '', '资金支持'), true);
  assert.equal(conditions.supportConflict('all', '政策文件', '资金支持'), true);
  assert.equal(conditions.supportConflict('all', '', '资金支持'), false);
  assert.equal(conditions.supportConflict('政策', '', ''), false);
  const { page } = searchPage(() => ({ total: 0, total_all: 0, items: [] }));
  page.onLoad({ kind: '政策', supports: '资金支持' }); await flush();
  assert.match(page.data.emptyHint, /支持方式只用于申报通知和名单/);
  page.onLoad({ kind: '政策', industries: '数字经济' }); await flush();
  assert.equal(page.data.emptyHint, '');
});

test('演示数据：支持方式只在申报通知和名单里找，同组多选取并集；产业方向所有类型', () => {
  const docs = [
    { title: 'a', kind: '申报通知', region: '山东 · 省级', date: '2026-09-01', supports: ['资金支持'], industries: ['先进制造'] },
    { title: 'b', kind: '名单', region: '山东 · 省级', date: '2026-09-01', supports: ['资格认定'], industries: [] },
    { title: 'c', kind: '政策', region: '山东 · 省级', date: '2026-09-01', supports: ['资金支持'], industries: ['数字经济'] },
    { title: 'd', kind: '申报通知', region: '山东 · 省级', date: '2026-09-01' },
  ];
  const run = (params) => mockSearch(docs, { time: '不限', ...params }).items.map((d) => d.title);
  assert.deepEqual(run({ supports: '资金支持' }), ['a']);
  assert.deepEqual(run({ supports: '资金支持,资格认定' }), ['a', 'b']);
  assert.deepEqual(run({ industries: '数字经济,先进制造' }), ['a', 'c']);
  assert.deepEqual(run({ industries: '先进制造', supports: '资格认定' }), []);
});

test('按标题算的标签不显示：列表行不读这两项；详情页支持方式只显示核对过的字段，并写明已核对原文', () => {
  const row = read('components/doc-row/index.wxml') + read('components/doc-row/index.js');
  for (const word of ['supports', 'industries', 'industry']) assert.ok(!row.includes(word), `列表行不应读取 ${word}`);
  for (const file of ['pages/search/index.wxml', 'pages/detail/index.wxml', 'pages/detail/index.js']) {
    const text = read(file);
    assert.ok(!/\b(supports|industries)\b/.test(text), `${file} 不应显示按标题算的标签`);
  }
  const detail = read('pages/detail/index.wxml');
  assert.match(detail, /wx:if="{{doc.fields && doc.fields.support && doc.fields.support.length}}" class="card support-card"/);
  assert.match(detail, /<view class="section-title">支持方式<text class="weak excerpt-sec">已核对原文<\/text><\/view>/);
  assert.match(detail, /wx:for="{{doc.fields.support}}"/);
});
