const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');

// UI 第一轮打磨（D-053）：骨架屏、本周 0 份的说明、截止雷达单标题、名单状态标签。
// 依赖都用替身，不读取 legal.js 或本机配置。
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
const wxml = (page) => read(`pages/${page}/index.wxml`);
const daywatch = { watchDay() {}, stopDay() {} };
const app = { getApp: () => ({ globalData: {} }) };
const FORBIDDEN = ['AI', '智能', '大模型'];

function homePage(stats) {
  return load('pages/home/index.js', {
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
  assert.match(template, /<view wx:if="{{\(loading \|\| !stats\) && !error}}" class="summary" aria-label="正在加载">/);
  assert.match(template, /<view wx:if="{{\(loading \|\| !loaded\) && !error}}" aria-label="正在加载">\s*<view wx:for="{{\[1, 2, 3\]}}" wx:key="\*this" class="sk-row">/);
  assert.match(template, /<view wx:if="{{!shown.length && !loading && loaded && !error}}" class="list-tip">最新的文件里暂时没有这一类<\/view>/);
  // {{stats.sources}}、{{stats.provinces}} 只出现在 stats 到了以后才显示的地方
  const footer = template.slice(template.indexOf('<view class="footer">'));
  assert.match(footer, /<text wx:if="{{stats}}">数据来自 {{stats.sources}} 个政府网站栏目，覆盖国家部委和 {{stats.provinces}} 个省<text wx:if="{{stats.cities}}">、{{stats.cities}} 个城市<\/text>，<\/text>每条都附原文链接。有据是第三方整理的信息工具，不是政府官方平台，也不代办申报。/);
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

test('截止雷达只有一处标题和一个返回：自定义导航，原来的副标题保留为一行说明', () => {
  const template = wxml('report');
  assert.equal(JSON.parse(read('pages/report/index.json')).navigationStyle, 'custom');
  assert.equal((template.match(/截止雷达/g) || []).length, 2); // 顶栏标题 + 底部说明里的"截止雷达只含……"
  assert.equal((template.match(/class="nav-title"/g) || []).length, 1);
  assert.equal((template.match(/bindtap="goBack"/g) || []).length, 1);
  assert.match(template, /<view class="sub">只看核对过的申报截止日期<\/view>/);
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

test('名单看板的公示、公布和列表行用同一种标签：公示橙色虚线框，公布灰色实线框', async () => {
  const page = load('pages/list-board/index.js', {
    '../../utils/format.js': format,
    '../../utils/api.js': { listBoard: () => Promise.resolve({ recent: { total: 3, items: [
      { id: 1, title: 'A', wording: '公示' }, { id: 2, title: 'B', wording: '公布' }, { id: 3, title: 'C', wording: null },
    ] }, stats: {} }) },
  });
  page.load(true); await flush();
  assert.deepEqual(page.data.items.map((i) => i.wordingCls), ['public', 'final', '']);
  const template = wxml('list-board');
  assert.match(template, /<text wx:if="{{item.wording}}" class="status status--{{item.wordingCls}}">{{item.wording}}<\/text>/);
  assert.ok(!/class="wording"/.test(template));
  assert.match(read('components/doc-row/index.wxml'), /class="status status--{{statusCls}}"/);
  assert.match(read('app.wxss'), /\.status--public \{ border: 2rpx dashed var\(--o-txt\); color: var\(--o-txt\); \}/);
});

test('本轮改过的界面文字不出现禁用字样，各页保留"第三方整理 · 非政府平台"', () => {
  const texts = ['home', 'detail', 'lists', 'report', 'follow', 'follow-new', 'me', 'list-board'].map(wxml);
  for (const text of texts) for (const word of FORBIDDEN) assert.ok(!text.includes(word), `${word} 出现在界面文字里`);
  assert.match(read('components/notice-bar/index.wxml'), /第三方整理 · 非政府平台/);
  for (const p of ['detail', 'report', 'follow', 'follow-new', 'list-board']) assert.match(wxml(p), /<notice-bar\b/);
  assert.match(wxml('home'), /第三方整理 · 非政府平台/);
});
