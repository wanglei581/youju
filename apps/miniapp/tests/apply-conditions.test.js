const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const mock = require('../mock/data.js');

// 详情页的申报条件卡片（D-052 第④项）：只显示接口给的、人工整份确认过的条件；每条可展开原文；
// 下方固定一行"条件以原文为准，不作为能否申报的判断"。依赖都用替身，不读取 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout,
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, ...globals,
  }, { filename: file });
  return { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, values); } };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const daywatch = { watchDay() {}, stopDay() {} };
const FORBIDDEN = ['AI', '智能', '大模型', '模型'];
const NOTE = '条件以原文为准，不作为能否申报的判断';

const BASE = { id: '7', kind: '申报通知', tags: ['申报通知'], title: '关于组织申报科技项目的通知', url: 'https://example.gov.cn/7', deadline: null, deadline_state: 'none', attachments: [] };
const CONFIRMED = {
  ...BASE,
  apply_conditions: {
    items: [
      { category: '主体资格', text: '注册地在本市', quote: '（一）申报单位注册地在本市，具有独立法人资格。' },
      { category: '主体资格', text: '具有独立法人资格', quote: '（一）申报单位注册地在本市，具有独立法人资格。' },
      { category: '财务指标', text: '上年度研发投入占营业收入比例不低于 3%', quote: '（二）上年度研发投入占营业收入比例不低于３％。' },
    ],
    checked: '2026-10-06',
  },
};

function detailPage(respond) {
  return load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { detail: () => respond() },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': {},
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { wx: {} });
}
const tap = (index) => ({ currentTarget: { dataset: { index } } });

test('有确认过的条件才有卡片：同类连着的只在第一条写类别，原文默认收起', async () => {
  let doc = BASE;
  const page = detailPage(() => Promise.resolve(JSON.parse(JSON.stringify(doc))));
  page.onLoad({ id: '7' }); await flush();
  assert.equal(page.data.conds.length, 0);
  doc = CONFIRMED; page.refreshing = false; page.load(); await flush();
  assert.deepEqual(page.data.conds.map((c) => [c.category, c.showCat, c.open]), [
    ['主体资格', true, false], ['主体资格', false, false], ['财务指标', true, false],
  ]);
  assert.equal(page.data.conds[2].quote, '（二）上年度研发投入占营业收入比例不低于３％。');
  // 换成没有条件的文件（或加载失败）时卡片消失
  doc = BASE; page.refreshing = false; page.load(); await flush();
  assert.equal(page.data.conds.length, 0);
  doc = CONFIRMED; page.refreshing = false; page.load(); await flush();
  const failing = detailPage(() => Promise.reject(Object.assign(new Error('网络不好'), { status: 0 })));
  failing.setData({ conds: page.data.conds });
  failing.onLoad({ id: '7' }); await flush();
  assert.equal(failing.data.conds.length, 0);
});

test('每条可以单独展开、收起原文', async () => {
  const page = detailPage(() => Promise.resolve(JSON.parse(JSON.stringify(CONFIRMED))));
  page.onLoad({ id: '7' }); await flush();
  page.toggleCond(tap(1));
  assert.deepEqual(page.data.conds.map((c) => c.open), [false, true, false]);
  page.toggleCond(tap('2'));
  assert.deepEqual(page.data.conds.map((c) => c.open), [false, true, true]);
  page.toggleCond(tap(1));
  assert.deepEqual(page.data.conds.map((c) => c.open), [false, false, true]);
  page.toggleCond(tap(9));
  assert.deepEqual(page.data.conds.map((c) => c.open), [false, false, true]);
});

test('卡片标题"申报条件（已核对原文）"，下方固定一行说明；没有条件时不显示；不出现禁用字样', () => {
  const wxml = read('pages/detail/index.wxml');
  const start = wxml.indexOf('<view wx:if="{{conds.length}}" class="card cond-card">');
  assert.ok(start > 0, '没有申报条件卡片，或卡片没有按 conds.length 判断显示');
  const end = wxml.indexOf(`<view class="weak cond-note">${NOTE}</view>`, start);
  assert.ok(end > start, '缺少固定的说明行');
  const card = wxml.slice(start, wxml.indexOf('</view>', end) + 7);
  const title = card.match(/<view class="section-title">([\s\S]*?)<\/view>/)[1].replace(/<[^>]+>/g, '');
  assert.equal(title, '申报条件（已核对原文）');
  // 说明行在逐条循环之外：一张卡片只出现一次
  const loop = card.slice(card.indexOf('wx:for="{{conds}}"'), card.indexOf('class="weak cond-note"'));
  assert.ok(!loop.includes(NOTE));
  assert.match(card, /wx:if="{{item.open}}" class="cond-quote"><text class="support-evidence-label">原文：<\/text>{{item.quote}}/);
  for (const source of [card, read('pages/detail/index.js'), read('pages/detail/index.wxss')]) {
    const shown = source.replace(/<!--[\s\S]*?-->/g, '');
    for (const word of FORBIDDEN) assert.ok(!shown.includes(word), `出现了禁用字样：${word}`);
  }
  // 不写能否申报的结论，只有说明里那句"不作为能否申报的判断"
  assert.equal(card.split('能否申报').length - 1, 1);
});

test('演示数据里的条件和原文摘录对得上，也不出现禁用字样', () => {
  const doc = mock.detail.ln;
  const conditions = doc.apply_conditions.items;
  assert.ok(conditions.length > 0);
  for (const item of conditions) {
    assert.ok(['主体资格', '财务指标', '知识产权', '人员', '其他'].includes(item.category));
    const quote = item.quote.replace(/^（[一二三四五六七八九十]+）/, '').replace(/。$/, '');
    assert.ok(doc.excerpt.text.includes(quote), `原文摘录里找不到：${item.quote}`);
    for (const word of FORBIDDEN) assert.ok(!JSON.stringify(item).includes(word));
  }
});
