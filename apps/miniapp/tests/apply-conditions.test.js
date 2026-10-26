const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const mock = require('../mock/data.js');

// 详情页的申报条件卡片（D-052 第④项）：只显示接口给的、人工整份确认过的条件，每条只显示类别和
// 通知原文里的原句（逐字引用），不显示概括的说法；下方固定一行"条件以原文为准，不作为能否申报的判断"。
// 依赖都用替身，不读取 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout,
    require(name) { if (!Object.hasOwn(deps, name) && /ai-access\.js$/.test(name)) return AI_OFF; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, ...globals,
  }, { filename: file });
  return { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, values); } };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
// 页面代码在另一个上下文里跑，比较前转成本上下文的普通对象
const plain = (value) => JSON.parse(JSON.stringify(value));
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const daywatch = { watchDay() {}, stopDay() {} };
const FORBIDDEN = ['AI', '智能', '大模型', '模型'];
const NOTE = '条件以原文为准，不作为能否申报的判断';

const BASE = { id: '7', kind: '申报通知', tags: ['申报通知'], title: '关于组织申报科技项目的通知', url: 'https://example.gov.cn/7', deadline: null, deadline_state: 'none', attachments: [] };
const CONFIRMED = {
  ...BASE,
  apply_conditions: {
    items: [
      { category: '主体资格', quote: '（一）申报单位注册地在本市，具有独立法人资格。' },
      { category: '主体资格', quote: '（四）成立满一年。' },
      { category: '财务指标', quote: '（二）上年度研发投入占营业收入比例不低于３％。' },
    ],
    checked: '2026-10-06',
  },
};

function detailPage(respond) {
  return load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { detail: () => respond() },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/repost.js': require('../utils/repost.js'),
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { wx: {} });
}
const tap = (index) => ({ currentTarget: { dataset: { index } } });

test('有确认过的条件才有卡片：每条是类别和原文原句，同类连着的只在第一条写类别', async () => {
  let doc = BASE;
  const page = detailPage(() => Promise.resolve(JSON.parse(JSON.stringify(doc))));
  page.onLoad({ id: '7' }); await flush();
  assert.equal(page.data.conds.length, 0);
  doc = CONFIRMED; page.refreshing = false; page.load(); await flush();
  assert.deepEqual(plain(page.data.conds), [
    { category: '主体资格', quote: '（一）申报单位注册地在本市，具有独立法人资格。', showCat: true },
    { category: '主体资格', quote: '（四）成立满一年。', showCat: false },
    { category: '财务指标', quote: '（二）上年度研发投入占营业收入比例不低于３％。', showCat: true },
  ]);
  // 换成没有条件的文件（或加载失败）时卡片消失
  doc = BASE; page.refreshing = false; page.load(); await flush();
  assert.equal(page.data.conds.length, 0);
  doc = CONFIRMED; page.refreshing = false; page.load(); await flush();
  const failing = detailPage(() => Promise.reject(Object.assign(new Error('网络不好'), { status: 0 })));
  failing.setData({ conds: page.data.conds });
  failing.onLoad({ id: '7' }); await flush();
  assert.equal(failing.data.conds.length, 0);
});

test('概括的说法（旧接口还带着的 text）不显示；同一句原文只列一次；没有原句的不显示', async () => {
  const old = {
    ...BASE,
    apply_conditions: {
      items: [
        { category: '主体资格', text: '注册地在本市', quote: '（一）申报单位注册地在本市，具有独立法人资格。' },
        { category: '财务指标', text: '具有独立法人资格', quote: '（一）申报单位注册地在本市，具有独立法人资格。' },
        { category: '其他', text: '没有原句的条目' },
      ],
      checked: '2026-10-06',
    },
  };
  const page = detailPage(() => Promise.resolve(JSON.parse(JSON.stringify(old))));
  page.onLoad({ id: '7' }); await flush();
  assert.deepEqual(plain(page.data.conds), [
    { category: '主体资格', quote: '（一）申报单位注册地在本市，具有独立法人资格。', showCat: true },
  ]);
  assert.ok(!('toggleCond' in page));
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
  // 每条只显示类别和原文原句，不显示概括的说法（item.text）
  assert.match(card, /<text class="cond-text" user-select>{{item.quote}}<\/text>/);
  assert.ok(!card.includes('item.text'));
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
