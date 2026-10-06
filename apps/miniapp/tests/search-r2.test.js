const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const { mockSearch } = require('../utils/mockSearch.js');

// UI 第二轮：搜索页的两栏筛选弹层、国家部委只在层级里、已选条件条、结果信息栏、空结果。
// 依赖都用替身，不读取 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout,
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  return def ? { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, values); } } : module.exports;
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const event = (dataset) => ({ currentTarget: { dataset } });
const daywatch = { watchDay() {}, stopDay() {} };
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

function searchPage({ respond = () => ({ total: 0, total_all: 0, items: [] }), final = false, consent = {}, wx = {} } = {}) {
  const calls = [];
  const page = load('pages/search/index.js', {
    '../../utils/api.js': { search: (p) => { calls.push(p); return Promise.resolve(respond(p)); } },
    '../../content/legal.js': { final },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch, '../../utils/exporter.js': {},
    '../../utils/consent.js': consent, '../../utils/subscribe.js': {}, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx });
  return { page, calls };
}
const chipLabels = (page) => plain(page.data.activeChips.map((c) => c.label));

test('两栏筛选：左栏按约定顺序列出 10 组，组名旁边是已选个数；默认的不限地区、近 30 天不算', async () => {
  const { page } = searchPage();
  page.onLoad({}); await flush();
  assert.deepEqual(page.data.groups.map((g) => g.title),
    ['地区', '类型', '申报状态', '发布时间', '项目类别', '支持方式', '产业方向', '名单状态', '层级', '发文部门']);
  assert.ok(Object.values(page.data.counts).every((n) => n === 0));
  page.pickOption(event({ group: 'region', option: '江苏' })); await flush();
  page.pickOption(event({ group: 'region', option: '山东' })); await flush();
  page.pickOption(event({ group: 'support', option: '项目立项' })); await flush();
  page.pickOption(event({ group: 'time', option: '近 7 天' })); await flush();
  assert.equal(page.data.counts.region, 2);
  assert.equal(page.data.counts.support, 1);
  assert.equal(page.data.counts.time, 1);
  assert.equal(page.data.counts.type, 0);
  page.pickOption(event({ group: 'time', option: '近 30 天' })); await flush();
  assert.equal(page.data.counts.time, 0);
  page.pickOption(event({ group: 'region', option: '不限地区' })); await flush();
  assert.equal(page.data.counts.region, 0);
  // 申报状态只有一项：再点一次取消
  page.pickOption(event({ group: 'open', option: '只看未截止' })); await flush();
  assert.equal(page.data.counts.open, 1);
  page.pickOption(event({ group: 'open', option: '只看未截止' })); await flush();
  assert.equal(page.data.counts.open, 0);
  assert.equal(page.conditions().open, undefined);
});

test('两栏筛选：从"地区""时间""状态"进来直接显示那一组，从"筛选"进来显示上次看的组；左栏切换组', () => {
  const { page } = searchPage();
  page.openFilter(event({ group: 'time' }));
  assert.equal(page.data.filterOpen, true);
  assert.equal(page.data.activeGroup, 'time');
  page.pickGroup(event({ key: 'industry' }));
  assert.equal(page.data.activeGroup, 'industry');
  page.closeFilter();
  page.openFilter(event({}));
  assert.equal(page.data.activeGroup, 'industry');
  const template = read('pages/search/index.wxml');
  assert.match(template, /class="chip[^"]*" data-group="region" bindtap="openFilter"/);
  assert.match(template, /data-group="time" bindtap="openFilter"/);
  assert.match(template, /data-group="status" bindtap="openFilter"/);
  // 左栏：组名 + 已选个数（品牌色数字，不用角标）；右栏每组一个滚动区，只显示当前组
  assert.match(template, /<scroll-view class="side" scroll-y[\s\S]*?wx:for="{{groups}}"[\s\S]*?bindtap="pickGroup"[\s\S]*?<text>{{g.title}}<\/text><text wx:if="{{counts\[g.key\]}}" class="side-count">{{counts\[g.key\]}}<\/text>/);
  assert.match(template, /<scroll-view wx:if="{{activeGroup === g.key}}" class="pane" scroll-y/);
  // 底部固定"重置"和带实时份数的"查看结果"
  assert.match(template, /<view class="sheet-foot">\s*<view class="btn-reset" bindtap="resetFilter">重置<\/view>\s*<view class="btn-primary btn-apply" bindtap="closeFilter">查看 {{total}} 份结果<\/view>/);
  // 面板高度不超过屏幕的 85%
  const sheet = /\.sheet \{([^}]*)\}/.exec(read('pages/search/index.wxss'))[1];
  const vh = [...sheet.matchAll(/(?:^|;)\s*(?:max-)?height:\s*(\d+)vh/g)].map((m) => Number(m[1]));
  assert.ok(vh.length >= 1 && vh.every((v) => v <= 85), sheet);
});

test('国家部委只在层级里：地区选项（含展开全部省份）没有国家部委', async () => {
  const { page } = searchPage();
  page.onLoad({}); await flush();
  const region = () => page.data.groups.find((g) => g.key === 'region');
  assert.ok(!region().options.includes('国家部委'));
  assert.ok(page.data.groups.find((g) => g.key === 'level').options.includes('国家部委'));
  assert.match(region().note, /国家部委的文件在“层级”里选/);
  page.pickOption(event({ group: 'region', option: '更多省份' })); await flush();
  assert.equal(region().options.length, 32); // 不限地区 + 31 个省级地区
  assert.ok(!region().options.includes('国家部委'));
  assert.equal(page.data.counts.region, 0); // "更多省份"不是条件
  // 选了常用列表以外的省份（如从关注打开）时直接列出全部省份，看得到选中的那一项
  const other = searchPage();
  other.page.onLoad({ regions: '浙江' }); await flush();
  assert.ok(other.page.data.groups.find((g) => g.key === 'region').options.includes('浙江'));
});

test('国家部委兼容转换：结果相同的才转成层级，其余原样保留；演示数据按服务端规则核对结果一致', () => {
  const m = (regions, levels) => plain(conditions.migrateNational(regions, levels));
  // 转换
  assert.deepEqual(m(['国家部委'], []), { regions: ['不限地区'], levels: ['国家部委'] });
  assert.deepEqual(m(['国家部委'], ['国家部委']), { regions: ['不限地区'], levels: ['国家部委'] });
  assert.deepEqual(m(['国家部委'], ['国家部委', '省级']), { regions: ['不限地区'], levels: ['国家部委'] });
  assert.deepEqual(m(['不限地区', '国家部委'], []), { regions: ['不限地区'], levels: ['国家部委'] });
  assert.deepEqual(m(['国家部委', '山东'], ['国家部委']), { regions: ['不限地区'], levels: ['国家部委'] });
  assert.deepEqual(m(['国家部委', '山东', '北京'], ['省级']), { regions: ['山东', '北京'], levels: ['省级'] });
  // 新写法表达不了的：原样保留
  assert.deepEqual(m(['国家部委', '山东'], []), { regions: ['国家部委', '山东'], levels: [] });
  assert.deepEqual(m(['国家部委', '山东'], ['国家部委', '省级']), { regions: ['国家部委', '山东'], levels: ['国家部委', '省级'] });
  assert.deepEqual(m(['国家部委'], ['省级']), { regions: ['国家部委'], levels: ['省级'] });
  // 没有国家部委的不动
  assert.deepEqual(m(['山东'], ['国家部委']), { regions: ['山东'], levels: ['国家部委'] });
  assert.deepEqual(m(['不限地区'], []), { regions: ['不限地区'], levels: [] });

  // 每种可能的旧条件，转换前后在演示数据（规则与服务端一致，含市级文件）上结果相同
  const docs = [
    { title: '国家', kind: '申报通知', region: '国家', date: '2026-09-01' },
    { title: '山东省级', kind: '申报通知', region: '山东 · 省级', date: '2026-09-01' },
    { title: '山东市级', kind: '申报通知', region: '山东 · 市级', date: '2026-09-01' },
    { title: '北京省级', kind: '申报通知', region: '北京 · 省级', date: '2026-09-01' },
  ];
  const titles = (regions, levels) => mockSearch(docs, { time: '不限', regions: regions.join(','), levels: levels.join(',') })
    .items.map((d) => d.title).sort();
  const regionSets = [['国家部委'], ['国家部委', '山东'], ['国家部委', '山东', '北京'], ['不限地区', '国家部委']];
  const levelSets = [[], ['国家部委'], ['省级'], ['国家部委', '省级']];
  for (const regions of regionSets) {
    for (const levels of levelSets) {
      const after = conditions.migrateNational(regions, levels);
      assert.deepEqual(titles(after.regions, after.levels), titles(regions, levels), `${regions} / ${levels}`);
    }
  }
});

test('国家部委兼容转换：网址参数和关注条件打开搜索时转换，请求和关注都用层级；保留下来的显示为已选，去掉后不再出现', async () => {
  // 截止雷达、旧版本的关注：regions=国家部委
  const { page, calls } = searchPage();
  const url = new URL(conditions.searchUrl({ kind: '申报通知', regions: '国家部委', time: '不限' }), 'https://test.invalid');
  page.onLoad(Object.fromEntries(url.searchParams)); await flush();
  assert.equal(calls.at(-1).regions, '不限地区');
  assert.equal(calls.at(-1).levels, '国家部委');
  assert.equal(page.conditions().levels, '国家部委');
  assert.deepEqual(chipLabels(page), ['不限时间', '国家部委']);
  assert.equal(page.data.counts.level, 1);
  assert.equal(page.data.counts.region, 0);
  assert.ok(!page.data.groups.find((g) => g.key === 'region').options.includes('国家部委'));
  // 国家部委 + 省份、层级只选省级：地区去掉国家部委
  page.onLoad({ regions: '国家部委,山东', levels: '省级' }); await flush();
  assert.equal(calls.at(-1).regions, '山东');
  assert.equal(calls.at(-1).levels, '省级');
  // 国家部委 + 省份、没选层级：没有结果相同的新写法，原样查，在地区里显示为已选
  const legacy = searchPage();
  legacy.page.onLoad({ regions: '国家部委,山东' }); await flush();
  assert.equal(legacy.calls.at(-1).regions, '国家部委,山东');
  assert.equal(legacy.calls.at(-1).levels, undefined);
  const region = () => legacy.page.data.groups.find((g) => g.key === 'region');
  assert.deepEqual(plain(region().options.slice(0, 2)), ['不限地区', '国家部委']);
  assert.equal(legacy.page.data.pickedMap['region:国家部委'], true);
  assert.equal(legacy.page.data.counts.region, 2);
  legacy.page.removePicked(event({ group: 'region', option: '国家部委' })); await flush();
  assert.equal(legacy.calls.at(-1).regions, '山东');
  assert.ok(!region().options.includes('国家部委'));
});

test('已选条件条：按组的顺序列出，点一个去掉并立即重新查；最后是"清空"；没有已选条件时不显示', async () => {
  const { page, calls } = searchPage();
  page.onLoad({}); await flush();
  assert.deepEqual(plain(page.data.activeChips), []);
  page.pickOption(event({ group: 'level', option: '省级' })); await flush();
  page.pickOption(event({ group: 'region', option: '江苏' })); await flush();
  page.pickOption(event({ group: 'support', option: '项目立项' })); await flush();
  page.pickOption(event({ group: 'type', option: '其他' })); await flush();
  page.pickOption(event({ group: 'status', option: '公示' })); await flush();
  page.pickOption(event({ group: 'time', option: '不限' })); await flush();
  assert.deepEqual(chipLabels(page), ['江苏', '其他类型', '不限时间', '项目立项', '名单公示', '省级']);
  // 每组已选个数和条上的个数一致
  const perGroup = {};
  page.data.activeChips.forEach((c) => { perGroup[c.group] = (perGroup[c.group] || 0) + 1; });
  for (const [group, n] of Object.entries(page.data.counts)) assert.equal(n, perGroup[group] || 0, group);

  const before = calls.length;
  page.removePicked(event({ group: 'support', option: '项目立项' })); await flush();
  assert.equal(calls.length, before + 1);
  assert.equal(calls.at(-1).supports, undefined);
  page.removePicked(event({ group: 'region', option: '江苏' })); await flush();
  assert.equal(calls.at(-1).regions, '不限地区'); // 地区去光了回到不限地区
  page.removePicked(event({ group: 'time', option: '不限' })); await flush();
  assert.equal(calls.at(-1).time, '近 30 天'); // 发布时间回到默认
  assert.deepEqual(chipLabels(page), ['其他类型', '名单公示', '省级']);
  page.resetFilter(); await flush();
  assert.deepEqual(plain(page.data.activeChips), []);
  assert.equal(calls.at(-1).levels, undefined);
  assert.equal(calls.at(-1).types, '');

  const template = read('pages/search/index.wxml');
  const bar = /<scroll-view wx:if="{{activeChips.length}}" class="picked-bar" scroll-x[\s\S]*?<\/scroll-view>/.exec(template)[0];
  assert.match(bar, /wx:for="{{activeChips}}"[^>]*data-group="{{item.group}}" data-option="{{item.option}}" bindtap="removePicked"[^>]*>{{item.label}}<t-icon name="close"/);
  assert.match(bar, /<view class="picked-clear" bindtap="resetFilter"[^>]*>清空<\/view>\s*<\/view>\s*<\/scroll-view>$/);
  // 在页签、筛选入口下面，结果信息上面
  assert.ok(template.indexOf('class="chips"') < template.indexOf('picked-bar'));
  assert.ok(template.indexOf('picked-bar') < template.indexOf('class="summary"'));
});

test('结果信息栏：第一行份数和淡色"共 X 份"（不限时间时不写）、排序、选择、关注小按钮；折叠说明单独一行，文字不变', async () => {
  const { page } = searchPage({ respond: () => ({ total: 544, total_all: 1268, folded: 141, guides: 141, items: [] }) });
  page.onLoad({}); await flush();
  assert.equal(`${page.data.fold.text}${page.data.fold.link}`, '已折叠 141 篇非申报类文件（资讯与指南），点开查看');
  const template = read('pages/search/index.wxml');
  const summary = /<view wx:if="{{!loading && !error}}" class="summary">[\s\S]*?\n<\/view>/.exec(template)[0];
  assert.match(summary, /<text class="b">{{total}}<\/text> 份<text wx:if="{{chipTime !== '不限'}}" class="weak summary-all">共 {{totalAll}} 份<\/text>/);
  assert.match(summary, /按发布时间 ↓/);
  assert.match(summary, /bindtap="select"/);
  assert.match(summary, /class="follow-mini {{followOff \? 'follow-mini--off' : ''}}" bindtap="follow"[^>]*><t-icon name="notification"[^>]*\/>关注<\/view>/);
  // 大横幅去掉了
  assert.doesNotMatch(template, /follow-bar|关注这个搜索<\/text>/);
  assert.doesNotMatch(read('pages/search/index.wxss'), /\.follow-bar/);
});

test('关注小按钮：协议定稿前是灰色，点了弹出原因，不报错；定稿后是正常样式', async () => {
  let consentCalls = 0;
  const off = searchPage({ final: false, consent: { ensureConsent: () => { consentCalls += 1; return Promise.resolve(false); } },
    wx: { showToast: () => assert.fail('不应弹出错误提示'), showModal: () => assert.fail('同意前不应打开关注对话框') } });
  assert.equal(off.page.data.followOff, true);
  off.page.follow(); await flush();
  assert.equal(consentCalls, 1);
  assert.equal(searchPage({ final: true }).page.data.followOff, false);
  // 原因由 ensureConsent 弹出：协议还是草案时写明定稿后开放
  let modal;
  const consent = load('utils/consent.js', {
    '../content/legal.js': { final: false }, './api.js': {}, './exports.js': {}, './exporter.js': {},
  }, { wx: { showModal: (o) => { modal = o; } } });
  assert.equal(await consent.ensureConsent(), false);
  assert.equal(modal.title, '协议定稿后开放');
  assert.match(modal.content, /协议正在定稿，定稿后开放/);
  const style = read('pages/search/index.wxss');
  assert.match(style, /\.follow-mini--off \{[^}]*background: var\(--fill\);[^}]*color: var\(--t3\);/);
});

test('空结果：写"没有符合条件的文件"，列出已选条件各带"去掉"和"清空条件"；保留"告诉我们"', async () => {
  const { page, calls } = searchPage();
  page.onLoad({ regions: '江苏', supports: '项目立项' }); await flush();
  assert.deepEqual(chipLabels(page), ['江苏', '项目立项']);
  page.removePicked(event({ group: 'region', option: '江苏' })); await flush();
  assert.equal(calls.at(-1).regions, '不限地区');
  const template = read('pages/search/index.wxml');
  const empty = /<view wx:elif="{{!items.length && !loading}}" class="empty">[\s\S]*?\n  <\/view>/.exec(template)[0];
  assert.match(empty, /<view class="empty-title">没有符合条件的文件<\/view>/);
  assert.match(empty, /wx:if="{{emptyHint}}" class="empty-note">{{emptyHint}}/);
  assert.match(empty, /<block wx:if="{{activeChips.length}}">[\s\S]*wx:for="{{activeChips}}"[\s\S]*{{item.label}}[\s\S]*bindtap="removePicked"[^>]*>去掉<\/text>[\s\S]*<view class="empty-clear" bindtap="resetFilter"[^>]*>清空条件<\/view>/);
  assert.match(empty, /<view wx:else class="empty-note">换个关键词或放宽筛选试试<\/view>/);
  assert.match(empty, /暂未收录的地区：{{notCoveredNote}}/);
  assert.match(template, /<view class="feedback" bindtap="feedback">没找到想要的？<text class="link">告诉我们<\/text><\/view>/);
});

test('按标题归类的三组：右栏顶部统一一行说明，下面只写各组特有的补充；两段合起来与原来的说明一字不差', () => {
  const { page } = searchPage();
  assert.equal(conditions.TAG_NOTE, '按标题归类，仅供检索参考，非官方分类');
  const full = (key) => `${conditions.TAG_NOTE}；${page.data.groups.find((g) => g.key === key).note}`;
  // 原来每组各自写的完整说明（UI 第一轮）
  assert.equal(full('category'), '按标题归类，仅供检索参考，非官方分类；只含申报通知和名单');
  assert.equal(full('support'), '按标题归类，仅供检索参考，非官方分类；只含申报通知和名单，不代表一定提供这种支持。文件详情里的“支持方式”是核对原文后标注的；“征集入选”只用于筛选，详情里没有这一项');
  assert.equal(full('industry'), '按标题归类，仅供检索参考，非官方分类；一份文件可能有多个方向，也可能没有');
  // 只有这三组显示统一说明，补充里不再重复它
  assert.deepEqual(page.data.groups.filter((g) => g.tagged).map((g) => g.key), ['category', 'support', 'industry']);
  for (const g of page.data.groups) assert.ok(!(g.note || '').includes('仅供检索参考'), g.key);
  const template = read('pages/search/index.wxml');
  assert.match(template, /<view class="group-title">{{g.title}}<text class="group-hint">{{g.hint}}<\/text><\/view>\s*<view wx:if="{{g.tagged}}" class="tag-note">{{tagNote}}<\/view>\s*<view class="options">/);
  assert.equal((template.match(/{{tagNote}}/g) || []).length, 1);
});

test('本轮新加的文字不出现禁用字样', async () => {
  const { page } = searchPage();
  page.onLoad({ regions: '国家部委,山东', types: '其他', status: '公示,公布', time: '不限', open: '1' }); await flush();
  const texts = [
    ...chipLabels(page), conditions.TAG_NOTE, page.data.groups.find((g) => g.key === 'region').note,
    read('pages/search/index.wxml'), read('pages/search/index.js'), read('utils/conditions.js'),
  ];
  for (const text of texts) {
    for (const word of ['AI', '人工智能', '智能', '大模型']) assert.ok(!text.includes(word), `${word} 出现在：${text.slice(0, 40)}`);
  }
});
