const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');

// UI 第三轮（D-053）：查名单页顶部只留标题、副标题和搜索框，查询说明默认收起；文件页的空状态改成一张带步骤的卡片。
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
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const wxml = (page) => read(`pages/${page}/index.wxml`).replace(/<!--[\s\S]*?-->/g, '');
const FORBIDDEN = ['AI', '智能', '大模型'];
const daywatch = { watchDay() {}, stopDay() {} };

// 改版前搜索框下面的规则说明（原文），收起后的四条要一字不少
const OLD_RULES = '按企业全称逐行比对名单原文；名称相近的企业分开列出，请核对统一社会信用代码。只查企业和机构，不查个人。名单状态按标题用词标注，公示期和当前效力以原文为准。';
const bare = (text) => text.replace(/[，。；、\s]/g, '');

function listsPage(lookup = () => new Promise(() => {})) {
  return load('pages/lists/index.js', {
    '../../utils/api.js': { lists: () => new Promise(() => {}), listBoard: () => new Promise(() => {}), lookup, isLive: () => true },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { getApp: () => ({ globalData: {} }), wx: { showToast() {} } });
}

test('查名单顶部只留标题、副标题和搜索框；"第三方整理 · 非政府平台"和其他页一样放在蓝色顶部下面', () => {
  const template = wxml('lists');
  const top = template.slice(0, template.indexOf('<notice-bar />'));
  assert.match(top, /<view class="h1">查企业上过哪些名单<\/view>/);
  assert.match(top, /<view class="sub">在已收录的高企、专精特新等公示和公布名单里查<\/view>/);
  assert.match(top, /<view class="search">/);
  // 顶部的说明文字只有标题和副标题两行
  assert.deepEqual([...top.matchAll(/<view class="(h1|sub)">/g)].map((m) => m[1]), ['h1', 'sub']);
  assert.ok(!/notice-bar|第三方整理|查企业在已收录名单里的记录/.test(top));
  // 和截止页一样：顶部结束后紧接着一行出处说明（灰底，不是蓝底版本）
  assert.match(template, /<\/view>\s*<notice-bar \/>\s*<view class="rules">/);
  assert.match(wxml('report'), /<\/view>\s*<notice-bar \/>/);
  assert.match(read('components/notice-bar/index.wxml'), /第三方整理 · 非政府平台/);
});

test('查询说明默认收起成一行（info 图标、向下箭头），点开后逐条列出，原来的说明一字不少', () => {
  const page = listsPage();
  assert.equal(page.data.rulesOpen, false);
  page.toggleRules();
  assert.equal(page.data.rulesOpen, true);
  page.toggleRules();
  assert.equal(page.data.rulesOpen, false);

  const template = wxml('lists');
  const head = /<view class="rules-head" bindtap="toggleRules" aria-role="button" aria-expanded="{{rulesOpen}}">([\s\S]*?)<\/view>/.exec(template);
  assert.ok(head, '查询说明那一行可以点');
  assert.match(head[1], /<t-icon name="info-circle"/);
  assert.match(head[1], /<text>查询说明<\/text>/);
  assert.match(head[1], /<t-icon name="{{rulesOpen \? 'chevron-up' : 'chevron-down'}}"/);
  // 收起时不渲染内容；点开后的四条拼起来和原来的说明逐字相同（只差标点）
  const body = /<view wx:if="{{rulesOpen}}" class="rules-body">([\s\S]*?)\n {2}<\/view>/.exec(template);
  assert.ok(body, '查询说明的内容只在点开后显示');
  const rules = [...body[1].matchAll(/<view class="rule">([^<]+)<\/view>/g)].map((m) => m[1]);
  assert.deepEqual(rules, [
    '按企业全称逐行比对名单原文',
    '名称相近的企业分开列出，请核对统一社会信用代码',
    '只查企业和机构，不查个人',
    '名单状态按标题用词标注，公示期和当前效力以原文为准',
  ]);
  assert.equal(bare(rules.join('')), bare(OLD_RULES));
  // 原来那段灰字不再默认铺在搜索框下面
  assert.ok(!template.includes(OLD_RULES));
  assert.ok(!/class="note"/.test(template));
});

test('搜索框下面不另加提示（和搜索框里的提示重复），查询说明之后直接是查询结果和最近发布的名单', () => {
  const template = wxml('lists');
  assert.ok(!/class="hint"|输入企业全称或统一社会信用代码查询/.test(template));
  assert.ok(!/\.hint \{/.test(read('pages/lists/index.wxss')));
  assert.match(template, /placeholder="输入企业全称或统一社会信用代码"/);
  const page = listsPage();
  page.setData({ name: '某某科技有限公司' });
  page.query();
  assert.equal(page.data.queried, '某某科技有限公司');
});

function filesPage(records) {
  return load('pages/files/index.js', {
    '../../utils/api.js': { isLive: () => true },
    '../../utils/exports.js': { list: () => records },
    '../../utils/exporter.js': { exists: () => true },
    '../../utils/format.js': format,
  }, { wx: {} });
}

test('文件页还没导出过：一张卡片，图标、"还没有导出过"、三个带序号的步骤和在电脑上保存的提示', () => {
  const empty = filesPage([]);
  empty.load();
  assert.equal(empty.data.demo, false);
  assert.equal(empty.data.files.length, 0);

  const template = wxml('files');
  assert.ok(!template.includes('导出记录 0'));
  assert.match(template, /<view wx:if="{{files.length}}" class="card">\s*<view class="section-title">导出记录 {{files.length}}/);
  const card = /<view wx:else class="card empty">([\s\S]*?)\n<\/view>/.exec(template);
  assert.ok(card, '没有记录时显示空状态卡片');
  assert.match(card[1], /<view class="empty-icon"><t-icon name="file"/);
  assert.match(card[1], /<view class="empty-title">还没有导出过<\/view>/);
  const steps = [...card[1].matchAll(/<text class="step-no">(\d)<\/text><text class="step-text">([^<]+)<\/text>/g)].map((m) => [m[1], m[2]]);
  assert.deepEqual(steps, [
    ['1', '在搜索结果点“选择”，或在文件详情点“导出”'],
    ['2', '选 Excel 或 Word'],
    ['3', '导出后可转发给同事或客户'],
  ]);
  assert.match(card[1], /<view class="pc-tip">在电脑上保存：转发到“文件传输助手”，在电脑版微信打开<\/view>/);

  // 有记录时照常显示列表
  const filled = filesPage([{ id: 'a', format: 'xlsx', label: '高企名单', count: 3, size: 2048, at: '2026-10-06T08:00:00+08:00', path: '/x' }]);
  filled.load();
  assert.equal(filled.data.files.length, 1);
  assert.equal(filled.data.files[0].badge, 'XLSX');
});

test('文件页原有的说明保留：只存在这台手机上的那句在列表下方，带原文链接和数据说明的那句在页面底部', () => {
  const template = wxml('files');
  const storage = '<view class="tip">导出记录只保存在这台手机上；为节省空间只保留最近一份文件，更早的可以重新导出。</view>';
  const source = '<view class="tip">导出的表格和文档每份都带原文链接和数据说明（来源网站、核对日期、以原文为准）。</view>';
  const afterCards = template.slice(template.indexOf('<view wx:else class="card empty">'));
  // 两句都在列表和空状态卡片之后，有没有记录都显示
  assert.ok(afterCards.indexOf(storage) > 0);
  assert.ok(afterCards.indexOf(source) > afterCards.indexOf(storage));
  assert.match(template, /<\/view>\s*<view class="tip">导出记录只保存/);
  assert.match(template, /以原文为准）。<\/view>\s*<view class="tab-page-bottom"><\/view>\s*$/);
  // 顶部不再有一段说明，"第三方整理"照旧
  assert.ok(!/class="intro"/.test(template));
  assert.match(template, /^<notice-bar \/>/);
});

test('本轮改过的两页不出现禁用字样', () => {
  for (const page of ['lists', 'files']) {
    const text = read(`pages/${page}/index.wxml`);
    for (const word of FORBIDDEN) assert.ok(!text.includes(word), `${page} 出现了 ${word}`);
  }
});


test('简称至少两字可查，候选名称点击后用全称查询；无候选和过短保持提示', async () => {
  const calls = []; const toasts = [];
  const page = load('pages/lists/index.js', {
    '../../utils/api.js': { isLive: () => true, lookup: (q) => {
      calls.push(q); return Promise.resolve({ items: [], similar: [], candidates: q === '海尔' ? ['海尔集团', '青岛海尔有限公司'] : [], coverage: { lists: 1 } });
    } }, '../../utils/format.js': format, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
  }, { wx: { showToast: (v) => toasts.push(v.title) } });
  page.setData({ name: '海' }); page.query(); assert.equal(calls.length, 0); assert.match(toasts[0], /请输入企业全称/);
  page.setData({ name: '海尔' }); page.query(); await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(Array.from(page.data.result.candidates), ['海尔集团', '青岛海尔有限公司']);
  assert.match(wxml('lists'), /你要查的是不是：/);
  page.pickSimilar({ currentTarget: { dataset: { name: '海尔集团' } } }); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.at(-1), '海尔集团'); assert.equal(page.data.result.candidates.length, 0);
});
