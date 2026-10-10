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
    '../../utils/consent.js': { loginOpen: () => final, ...consent }, '../../utils/subscribe.js': { remindOpen: () => true }, '../../utils/conditions.js': conditions,
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
  assert.match(template, /data-group="{{entries.region.group}}" bindtap="openFilter">地区</);
  assert.match(template, /data-group="{{entries.time.group}}" bindtap="openFilter">时间</);
  assert.match(template, /data-group="{{entries.status.group}}" bindtap="openFilter">状态</);
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

test('筛选入口固定写"地区""时间""状态""筛选"，管的组有已选时变蓝（近 30 天不算），具体值不写在入口上', async () => {
  const { page } = searchPage();
  page.onLoad({}); await flush();
  const on = () => ({ region: page.data.entries.region.on, time: page.data.entries.time.on, status: page.data.entries.status.on, more: page.data.moreOn });
  assert.deepEqual(on(), { region: false, time: false, status: false, more: false });
  assert.equal(page.data.entries.status.group, 'open');
  page.pickOption(event({ group: 'level', option: '国家部委' })); await flush();
  assert.equal(page.data.entries.region.on, true); // 地区入口管地区和层级
  assert.equal(page.data.entries.region.group, 'level'); // 点开时显示有已选的那一组
  page.pickOption(event({ group: 'region', option: '山东' })); await flush();
  assert.equal(page.data.entries.region.group, 'region');
  page.pickOption(event({ group: 'time', option: '近 7 天' })); await flush();
  page.pickOption(event({ group: 'status', option: '公布' })); await flush();
  page.pickOption(event({ group: 'industry', option: '现代农业' })); await flush();
  assert.deepEqual(on(), { region: true, time: true, status: true, more: true });
  assert.equal(page.data.entries.status.group, 'status');
  page.pickOption(event({ group: 'time', option: '近 30 天' })); await flush();
  assert.equal(page.data.entries.time.on, false);
  page.resetFilter(); await flush();
  assert.deepEqual(on(), { region: false, time: false, status: false, more: false });
  const chips = /<view class="chips">[\s\S]*?\n<\/view>/.exec(read('pages/search/index.wxml'))[0];
  assert.match(chips, /entries.region.on \? 'chip--on'[^>]*>地区<t-icon/);
  assert.match(chips, /entries.time.on \? 'chip--on'[^>]*>时间<t-icon/);
  assert.match(chips, /entries.status.on \? 'chip--on'[^>]*>状态<t-icon/);
  assert.match(chips, /filter-link {{moreOn \? 'filter-link--on' : ''}}"[^>]*><t-icon name="filter"[^>]*\/>筛选<\/view>/);
  assert.doesNotMatch(chips, /chipRegion|{{chipTime}}/);
});

test('国家部委只在层级里：地区选项（含展开全部省份）没有国家部委；地区和层级栏写明一起选时怎样列', async () => {
  const { page } = searchPage();
  page.onLoad({}); await flush();
  const region = () => page.data.groups.find((g) => g.key === 'region');
  assert.ok(!region().options.includes('国家部委'));
  assert.ok(page.data.groups.find((g) => g.key === 'level').options.includes('国家部委'));
  assert.equal(region().note, '国家部委的文件在“层级”里选，和地区一起选时会同时列出');
  assert.match(page.data.groups.find((g) => g.key === 'level').note, /国家部委的文件同时列出，省级只列所选地区的/);
  page.pickOption(event({ group: 'region', option: '更多省份' })); await flush();
  assert.equal(region().options.length, 32); // 不限地区 + 31 个省级地区
  assert.ok(!region().options.includes('国家部委'));
  assert.equal(page.data.counts.region, 0); // "更多省份"不是条件
  // 选了常用列表以外的省份（如从关注打开）时直接列出全部省份，看得到选中的那一项
  const other = searchPage();
  other.page.onLoad({ regions: '浙江' }); await flush();
  assert.ok(other.page.data.groups.find((g) => g.key === 'region').options.includes('浙江'));
});

test('地区 + 层级的几种组合：请求怎么拼，服务端规则（演示数据）下列出哪些文件', async () => {
  const docs = [
    { title: '国家', kind: '申报通知', region: '国家', date: '2026-09-01' },
    { title: '山东省级', kind: '申报通知', region: '山东 · 省级', date: '2026-09-01' },
    { title: '山东市级', kind: '申报通知', region: '山东 · 市级', date: '2026-09-01' },
    { title: '北京省级', kind: '申报通知', region: '北京 · 省级', date: '2026-09-01' },
  ];
  const cases = [
    // [地区, 层级, 请求, 列出]
    [['山东'], ['国家部委'], { regions: '国家部委,山东' }, ['国家', '山东省级', '山东市级']],
    [['山东'], ['国家部委', '省级'], { regions: '国家部委,山东', levels: '国家部委,省级' }, ['国家', '山东省级']],
    [['山东'], ['省级'], { regions: '山东', levels: '省级' }, ['山东省级']],
    [['山东'], [], { regions: '山东' }, ['山东省级', '山东市级']],
    [['不限地区'], ['国家部委'], { regions: '不限地区', levels: '国家部委' }, ['国家']],
    [['不限地区'], ['国家部委', '省级'], { regions: '不限地区', levels: '国家部委,省级' }, ['北京省级', '国家', '山东省级']],
    [['山东', '北京'], ['国家部委'], { regions: '国家部委,山东,北京' }, ['北京省级', '国家', '山东省级', '山东市级']],
  ];
  for (const [region, level, request, listed] of cases) {
    const { page, calls } = searchPage();
    page.onLoad({}); await flush();
    for (const r of region.filter((x) => x !== '不限地区')) page.pickOption(event({ group: 'region', option: r }));
    for (const l of level) page.pickOption(event({ group: 'level', option: l }));
    await flush();
    const sent = { regions: calls.at(-1).regions, ...(calls.at(-1).levels ? { levels: calls.at(-1).levels } : {}) };
    assert.deepEqual(sent, request, `${region} / ${level}`);
    assert.deepEqual(mockSearch(docs, { time: '不限', ...sent }).items.map((d) => d.title).sort(), listed.slice().sort(), `${region} / ${level}`);
    assert.equal(page.data.emptyHint, '');
    // 关注名称按界面上选的写，国家部委不重复
    assert.equal((page.followName().match(/国家部委/g) || []).length, level.includes('国家部委') ? 1 : 0);
  }
});

test('打开旧条件（网址参数、保存的关注）：换成界面上的地区和层级，拼回去的请求结果不变', async () => {
  const m = (regions, levels) => plain(conditions.migrateNational(regions, levels));
  assert.deepEqual(m(['国家部委'], []), { regions: ['不限地区'], levels: ['国家部委'] });
  assert.deepEqual(m(['国家部委'], ['国家部委', '省级']), { regions: ['不限地区'], levels: ['国家部委'] });
  assert.deepEqual(m(['国家部委', '山东'], []), { regions: ['山东'], levels: ['国家部委'] });
  assert.deepEqual(m(['国家部委', '山东'], ['国家部委']), { regions: ['不限地区'], levels: ['国家部委'] });
  assert.deepEqual(m(['国家部委', '山东'], ['省级']), { regions: ['山东'], levels: ['省级'] });
  assert.deepEqual(m(['国家部委', '山东'], ['国家部委', '省级']), { regions: ['山东'], levels: ['国家部委', '省级'] });
  assert.deepEqual(m(['山东'], ['国家部委', '省级']), { regions: ['山东'], levels: ['省级'] });
  assert.deepEqual(m(['山东'], ['省级']), { regions: ['山东'], levels: ['省级'] });
  assert.deepEqual(m(['不限地区'], []), { regions: ['不限地区'], levels: [] });
  // 原来必然查不到的两种按字面意思换
  assert.deepEqual(m(['国家部委'], ['省级']), { regions: ['不限地区'], levels: ['国家部委', '省级'] });
  assert.deepEqual(m(['山东'], ['国家部委']), { regions: ['山东'], levels: ['国家部委'] });

  // 所有组合：旧请求 → 界面 → 新请求，在演示数据（规则与服务端一致，含市级文件）上结果相同
  const docs = [
    { title: '国家', kind: '申报通知', region: '国家', date: '2026-09-01' },
    { title: '山东省级', kind: '申报通知', region: '山东 · 省级', date: '2026-09-01' },
    { title: '山东市级', kind: '申报通知', region: '山东 · 市级', date: '2026-09-01' },
    { title: '北京省级', kind: '申报通知', region: '北京 · 省级', date: '2026-09-01' },
  ];
  const titles = (params) => mockSearch(docs, { time: '不限', ...params }).items.map((d) => d.title).sort();
  const regionSets = [['不限地区'], ['山东'], ['山东', '北京'], ['国家部委'], ['国家部委', '山东'], ['国家部委', '山东', '北京'], ['不限地区', '国家部委']];
  const levelSets = [[], ['国家部委'], ['省级'], ['国家部委', '省级']];
  const changed = [];
  for (const regions of regionSets) {
    for (const levels of levelSets) {
      const before = titles({ regions: regions.join(','), levels: levels.join(',') });
      const ui = conditions.migrateNational(regions, levels);
      const after = titles(conditions.requestScope(ui.regions, ui.levels));
      if (before.length === 0 && after.length > 0) { changed.push(`${regions}/${levels}`); continue; } // 原来必然查不到的
      assert.deepEqual(after, before, `${regions} / ${levels}`);
      // 用新请求再打开一次（如关注后从关注页回来），界面不再变
      const sent = conditions.requestScope(ui.regions, ui.levels);
      const again = conditions.migrateNational(sent.regions.split(','), sent.levels ? sent.levels.split(',') : []);
      assert.deepEqual(plain(again), plain(ui), `${regions} / ${levels}`);
    }
  }
  assert.deepEqual(changed, ['山东/国家部委', '山东,北京/国家部委', '国家部委/省级', '不限地区,国家部委/省级']);

  // 搜索页：截止雷达、旧版本的关注 regions=国家部委 → 层级
  const { page, calls } = searchPage();
  const url = new URL(conditions.searchUrl({ kind: '申报通知', regions: '国家部委', time: '不限' }), 'https://test.invalid');
  page.onLoad(Object.fromEntries(url.searchParams)); await flush();
  assert.equal(calls.at(-1).regions, '不限地区');
  assert.equal(calls.at(-1).levels, '国家部委');
  assert.deepEqual(chipLabels(page), ['不限时间', '国家部委']);
  assert.equal(page.data.counts.level, 1);
  assert.equal(page.data.counts.region, 0);
  // 旧关注"国家部委 + 山东"：界面上是地区山东、层级国家部委，请求照旧
  page.onLoad({ regions: '国家部委,山东' }); await flush();
  assert.deepEqual(plain(page.data.picked.region), ['山东']);
  assert.deepEqual(plain(page.data.picked.level), ['国家部委']);
  assert.equal(calls.at(-1).regions, '国家部委,山东');
  assert.equal(calls.at(-1).levels, undefined);
  assert.ok(!page.data.groups.find((g) => g.key === 'region').options.includes('国家部委'));
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

test('结果信息栏：第一行份数和淡色"不限时间共 X 份"（选了不限时间时不写）、排序、选择、关注小按钮；折叠说明单独一行，文字不变', async () => {
  const { page } = searchPage({ respond: () => ({ total: 544, total_all: 1268, folded: 141, guides: 141, items: [] }) });
  page.onLoad({}); await flush();
  assert.equal(`${page.data.fold.text}${page.data.fold.link}`, '已折叠 141 篇非申报类文件（资讯与指南），点开查看');
  const template = read('pages/search/index.wxml');
  const summary = /<view wx:if="{{!loading && !error}}" class="summary">[\s\S]*?\n<\/view>/.exec(template)[0];
  assert.match(summary, /<text class="b">{{total}}<\/text> 份<text wx:if="{{chipTime !== '不限'}}" class="weak summary-all">不限时间共 {{totalAll}} 份<\/text>/);
  assert.match(summary, /按发布时间 ↓/);
  assert.match(summary, /bindtap="select"/);
  assert.match(summary, /<view wx:if="{{loginOpen}}" class="follow-mini" bindtap="follow"[^>]*><t-icon name="notification"[^>]*\/>关注<\/view>/);
  // 大横幅去掉了
  assert.doesNotMatch(template, /follow-bar|关注这个搜索<\/text>/);
  assert.doesNotMatch(read('pages/search/index.wxss'), /\.follow-bar/);
});

test('关注小按钮：协议定稿前不显示（D-056），兜底的 ensureConsent 只提示暂未开放、不弹"协议定稿后开放"；定稿后确认框写"关注后，有新文件时在关注页标出"', async () => {
  const NOTE = '关注后，有新文件时在关注页标出';
  const off = searchPage({ final: false });
  assert.equal(off.page.data.loginOpen, false);
  // 兜底：协议定稿前万一走到 ensureConsent，只轻提示，不弹对话框
  let toast;
  const consent = load('utils/consent.js', {
    '../content/legal.js': { final: false }, './api.js': {}, './exports.js': {}, './exporter.js': {},
  }, { wx: { showToast: (o) => { toast = o; }, showModal: () => assert.fail('协议定稿前不弹对话框') } });
  assert.equal(consent.loginOpen(), false);
  assert.equal(await consent.ensureConsent(), false);
  assert.equal(toast.title, '这个功能暂未开放');
  // 协议开放后：确认框（可编辑名称）的标题写这句
  let confirm;
  const on = searchPage({ final: true, consent: { ensureConsent: () => Promise.resolve(true) }, wx: { showModal: (o) => { confirm = o; } } });
  assert.equal(on.page.data.loginOpen, true);
  on.page.onLoad({}); await flush();
  on.page.follow(); await flush();
  assert.equal(confirm.title, NOTE);
  assert.equal(confirm.editable, true);
  assert.equal(confirm.confirmText, '关注');
  assert.doesNotMatch(read('pages/search/index.wxss'), /follow-mini--off/);
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
  assert.match(template, /<view wx:if="{{loginOpen}}" class="feedback" bindtap="feedback">没找到想要的？<text class="link">告诉我们<\/text><\/view>/);
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

test('市级条件恢复、已选条、关注和国家部委组合均保留市级', async () => {
  const { page, calls } = searchPage();
  page.onLoad({ regions: '山东', levels: '市级', time: '不限' }); await flush();
  assert.equal(calls.at(-1).levels, '市级');
  assert.ok(chipLabels(page).includes('市级'));
  assert.match(page.followName(), /山东 · 市级/);
  const level = page.data.groups.find((g) => g.key === 'level');
  assert.match(level.note, /地市目前只收录部分城市/);
  assert.match(level.note, /我的 → 数据从哪里来/);
  page.pickOption(event({ group: 'level', option: '国家部委' })); await flush();
  assert.equal(calls.at(-1).regions, '国家部委,山东');
  assert.equal(calls.at(-1).levels, '市级,国家部委');
  const restored = searchPage();
  restored.page.onLoad(Object.fromEntries(new URL(conditions.searchUrl(page.conditions()), 'https://test.invalid').searchParams));
  await flush();
  assert.equal(restored.calls.at(-1).levels, '国家部委,市级');
  assert.deepEqual(plain(restored.page.data.picked.level), ['国家部委', '市级']);
});

test('市级演示数据用省份筛选，国家部委拼法包含所选市级且排除省级', () => {
  const docs = [
    { id: '1', title: '青岛市级', province: '山东', city: '青岛', level: '市级', kind: '申报通知', region: '青岛 · 市级' },
    { id: '2', title: '山东省级', province: '山东', level: '省级', kind: '申报通知', region: '山东 · 省级' },
    { id: '3', title: '国家', province: '国家', level: '国家级', kind: '申报通知', region: '国家' },
  ];
  const ids = (region, level) => mockSearch(docs, { ...conditions.requestScope(region, level), time: '不限' }).items.map((d) => d.id);
  assert.deepEqual(ids(['山东'], []), ['1', '2']);
  assert.deepEqual(ids(['山东'], ['市级']), ['1']);
  assert.deepEqual(ids(['山东'], ['省级']), ['2']);
  assert.deepEqual(ids(['山东'], ['国家部委', '市级']), ['1', '3']);
  assert.deepEqual(ids(['山东'], ['国家部委', '省级', '市级']), ['1', '2', '3']);
});

test('未截止选项写明当前条数，空状态首先去掉未截止；不限时间按钮改变时间并刷新', async () => {
  const { page, calls } = searchPage({ respond: (p) => ({ items: p.open || p.time !== '不限' ? [] : [{ id: '1' }], total: p.open || p.time !== '不限' ? 0 : 6, total_all: p.open ? 0 : 6, verified_open_total: 0 }) });
  page.onLoad({ open: '1' }); await flush();
  assert.match(page.data.groups.find((g) => g.key === 'open').note, /只含截止日期已核对、还没截止的通知（目前 0 份）/);
  assert.ok(read('pages/search/index.wxml').indexOf('bindtap="removeOpen"') < read('pages/search/index.wxml').indexOf('bindtap="allTime"'));
  page.removeOpen(); await flush(); assert.equal(calls.at(-1).open, undefined); assert.equal(page.data.totalAll, 6);
  assert.match(read('pages/search/index.wxml'), /查看不限发布时间的 {{totalAll}} 份/);
  page.allTime(); await flush(); assert.equal(calls.at(-1).time, '不限'); assert.equal(page.data.items.length, 1);
  page.onLoad({ time: encodeURIComponent('近 90 天') }); await flush(); assert.equal(calls.at(-1).time, '近 90 天');
});
