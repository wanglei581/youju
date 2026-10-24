const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const boost = require('../utils/boost.js');
const conditions = require('../utils/conditions.js');

// UI 第一轮打磨（D-053）：骨架屏、本周 0 份的说明、截止雷达单标题、名单状态标签（10/24 起叫发布阶段，D-090）。
// 依赖都用替身，不读取 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout,
    require(name) { if (name === '../../utils/regions.js') return require('../utils/regions.js'); if (!Object.hasOwn(deps, name) && /ai-access\.js$/.test(name)) return AI_OFF; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, ...globals,
  }, { filename: file });
  return { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, values); } };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const wxml = (page) => read(`pages/${page}/index.wxml`);
const daywatch = { watchDay() {}, stopDay() {} };
const app = { getApp: () => ({ globalData: {} }) };
const FORBIDDEN = ['AI', '智能', '大模型'];

function homePage(stats) {
  return load('pages/home/index.js', {
    '../../utils/boost.js': boost,
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { home: () => Promise.resolve({ stats, items: [] }) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { ...app, wx: {} });
}

test('最近检查的时间按北京时间写成"10月6日 06:30"，不是今年的带年份，格式不对不写', () => {
  const now = new Date(Date.UTC(2026, 9, 6, 4, 0));
  assert.equal(format.clockText('2026-10-06T06:30:00+08:00', now), '10月6日 06:30');
  assert.equal(format.clockText('2026-10-05T22:30:59.123456+00:00', now), '10月6日 06:30');
  assert.equal(format.clockText('2026-10-05T22:30:00Z', now), '10月6日 06:30');
  assert.equal(format.clockText('2025-12-31T23:05:00+08:00', now), '2025年12月31日 23:05');
  for (const bad of [null, undefined, '', '2026-10-06', '昨天']) assert.equal(format.clockText(bad, now), '');
});

test('首页本周 0 份：写"本周暂无新文件 · 最近检查 …"，不推测原因；有新文件时照旧显示份数', async () => {
  const page = homePage({ week_total: 0, apply: 0, lists: 0, other: 0, provinces: 28, sources: 72, last_checked_at: '2026-10-06T06:30:00+08:00' });
  page.onLoad(); await flush();
  assert.equal(page.data.stats.week_total, 0);
  assert.match(page.data.checkedText, /^10月6日 06:30$|^2026年10月6日 06:30$/);
  const template = wxml('home');
  assert.match(template, /<block wx:if="{{stats.week_total}}">[\s\S]*<text class="summary-label">本周新增<\/text>[\s\S]*<\/block>\s*<view wx:else class="summary-text">/);
  assert.match(template, /<view class="summary-zero">本周暂无新文件<text wx:if="{{checkedText}}"> · 最近检查 {{checkedText}}<\/text><\/view>/);
  const shown = template.replace(/<!--[\s\S]*?-->/g, '');
  for (const word of ['假期', '节假日', '休息', '原因', '因为']) assert.ok(!shown.includes(word), `首页出现了推测：${word}`);
  // 没有运行记录时只写"本周暂无新文件"
  const none = homePage({ week_total: 0, provinces: 1, sources: 1, last_checked_at: null });
  none.onLoad(); await flush();
  assert.equal(none.data.checkedText, '');
});

test('首页加载时用占位块：本周新增和最新列表都不写"正在加载…"，底部的栏目数、省份数到了才显示那半句', () => {
  const template = wxml('home');
  assert.ok(!template.includes('正在加载…'));
  // 有上次存在本机的数据时先显示它（10/12），占位块只在一点数据都没有时出现
  assert.match(template, /<view wx:if="{{!stats && !error}}" class="summary" aria-label="正在加载">/);
  assert.match(template, /<view wx:if="{{!loaded && !error}}" aria-label="正在加载">\s*<view wx:for="{{\[1, 2, 3\]}}" wx:key="\*this" class="sk-row">/);
  assert.match(template, /<view wx:if="{{!items.length && loaded && !error}}" class="list-tip">暂时没有新的申报通知<\/view>/);
  // {{stats.sources}}、{{stats.provinces}} 只出现在 stats 到了以后才显示的地方
  const footer = template.slice(template.indexOf('<view class="footer">'));
  assert.match(footer, /<text wx:if="{{stats}}">数据来自 {{stats.sources}} 个政府网站栏目，覆盖国家部委和 {{stats.provinces}} 个省<text wx:if="{{stats.cities}}">、{{stats.cities}} 个城市<\/text>，<\/text>每条都附原文链接。有据不代办申报。<\/text>/);
  assert.match(read('app.wxss'), /\.sk \{[^}]*background: var\(--fill\)/);
});

test('详情页和查名单的列表加载时用占位块，不写"正在加载…"', async () => {
  const detail = wxml('detail');
  assert.ok(!detail.includes('正在加载…'));
  assert.match(detail, /<view wx:if="{{!error && \(loading \|\| !doc\)}}" aria-label="正在加载">/);
  const page = load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { detail: () => new Promise(() => {}) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/repost.js': require('../utils/repost.js'),
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { wx: {} });
  page.onLoad({ id: '1' });
  assert.equal(page.data.loading, true);
  assert.equal(page.data.error, '');
  const lists = wxml('lists');
  assert.ok(!lists.includes('正在加载…'));
});

test('截止页（D-061）只有一处标题和一个返回：自定义导航，下面一行常驻"机器摘录仅供参考，以原文为准"', () => {
  const template = wxml('report');
  assert.equal(JSON.parse(read('pages/report/index.json')).navigationStyle, 'custom');
  assert.match(template, /<text class="nav-title">截止<\/text>/);
  assert.doesNotMatch(template.replace(/<!--[\s\S]*?-->/g, ''), /截止雷达|申报日历/);
  assert.equal((template.match(/class="nav-title"/g) || []).length, 1);
  assert.equal((template.match(/bindtap="goBack"/g) || []).length, 1);
  assert.match(template, /<view class="sub">机器摘录仅供参考，以原文为准<\/view>/);
  assert.match(template, /style="padding-top: {{statusBarHeight}}px"/);
});
test('关注的新文件：没同意协议（403）显示说明卡和去"我的"的入口，不写"加载失败"和"重试"', async () => {
  let fail = { status: 403, message: '请先阅读并同意用户协议和隐私保护指引' };
  let tab = '';
  const page = load('pages/follow-new/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { followNew: () => (fail ? Promise.reject(fail) : Promise.resolve({ total: 1, folded: 0, items: [{ id: '1' }] })) },
    '../../utils/format.js': format, '../../utils/conditions.js': conditions,
  }, { wx: { setNavigationBarTitle() {}, switchTab: ({ url }) => { tab = url; } } });
  page.onLoad({ id: '7' }); await flush();
  assert.equal(page.data.gated, true);
  assert.equal(page.data.error, '');
  page.goMe();
  assert.equal(tab, '/pages/me/index');
  const template = wxml('follow-new');
  const card = /<view wx:elif="{{gated}}" class="card gate">([\s\S]*?)<\/view>\s*<block wx:else>/.exec(template)[1];
  assert.match(card, /同意用户协议和隐私保护指引后，可以查看关注条件的新文件/);
  assert.match(card, /bindtap="goMe"[^>]*>去“我的” ›<\/view>/);
  assert.ok(!/加载失败|重试/.test(card));
  // 网络等其他错误仍写加载失败，可以重试；重试成功后说明卡消失
  fail = new Error('网络不太好，请稍后再试');
  page.load(true); await flush();
  assert.equal(page.data.gated, false);
  assert.match(page.data.error, /^加载失败：/);
  fail = null;
  page.load(true); await flush();
  assert.equal(page.data.gated, false);
  assert.equal(page.data.items.length, 1);
});

// 协议定稿前的入口（D-056）在 no-login.test.js：两种状态下各渲染一遍

test('名单的公示、公布在列表行里用同一种标签：公示橙色虚线框，公布灰色实线框（名单看板并入查名单后照旧）', () => {
  assert.match(read('components/doc-row/index.wxml'), /class="status status--{{statusCls}}"/);
  assert.match(read('app.wxss'), /\.status--public \{ border: 2rpx dashed var\(--o-txt\); color: var\(--o-txt\); \}/);
  assert.equal(format.statusClass('公示'), 'public'); assert.equal(format.statusClass('公布'), 'final');
});

test('本轮改过的界面文字不出现禁用字样，各页保留"第三方整理 · 非政府平台"', () => {
  const texts = ['home', 'detail', 'lists', 'report', 'clients', 'follow', 'follow-new', 'me'].map(wxml);
  for (const text of texts) for (const word of FORBIDDEN) assert.ok(!text.includes(word), `${word} 出现在界面文字里`);
  assert.match(read('components/notice-bar/index.wxml'), /第三方整理 · 非政府平台/);
  for (const p of ['detail', 'report', 'clients', 'lists', 'follow', 'follow-new']) assert.match(wxml(p), /<notice-bar\b/);
  assert.match(wxml('home'), /第三方整理 · 非政府平台/);
});
