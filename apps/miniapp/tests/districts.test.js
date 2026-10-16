const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const regions = require('../utils/regions.js');
const idempotency = require('../utils/data-idempotency.js');
const advisorHelpers = require('../utils/advisor.js');
const interpret = require('../utils/ai-interpret.js');
const clause = require('../utils/ai-clause.js');
const terms = require('../content/advisor-terms.js');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');

// 区县（2026-10-15）：省市区三级选择、条件、收录范围、本机客户、截止、批量表格、条款对照和显示的端到端回归。
// 全部加载真实模块和真实页面，只替换网络、存储等外部依赖。
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 8; i += 1) await flush(); };
const tap = (dataset) => ({ currentTarget: { dataset } });
const pick = (...value) => ({ detail: { value } });
const daywatch = { watchDay() {}, stopDay() {} };
const OWNER = 'a'.repeat(64);
const URL_BASE = 'https://synthetic.invalid';

function load(file, deps, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, Date, Math, Promise, Uint8Array, setTimeout, clearTimeout,
    require(name) { if (!Object.hasOwn(deps, name) && name === '../../utils/pay.js') return require('../utils/pay.js'); assert.ok(Object.hasOwn(deps, name), `${file} 缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  return def ? { ...def, data: plain(def.data), setData(value) { Object.assign(this.data, plain(value)); } } : module.exports;
}

// 收录范围：区县按自己的省、市、区县三项给出；城市列表里没有青岛也能查市南区（不要求先有市级来源）
const COVERAGE = {
  cities: [],
  districts: [
    { province: '山东', city: '青岛', district: '市南区', sources: 1 },
    { province: '北京', city: '北京', district: '朝阳区', sources: 2 },
    { province: '江苏', city: '南京', district: '鼓楼区', sources: 1 },
    { province: '福建', city: '福州市', district: '鼓楼区', sources: 1 },
  ],
};
const DISTRICT_DOC = {
  id: '31', kind: '申报通知', title: '关于组织申报区级科技项目的通知', region: '青岛 · 区县级', province: '山东', city: '青岛',
  district: '市南区', level: '区县级', issuer: '青岛市市南区科学技术局', date: '2026-10-10', url: 'https://example.invalid/31',
};

function searchPage({ coverage = COVERAGE, search, createFollow, exported = [], wx = {} } = {}) {
  const calls = [];
  const page = load('pages/search/index.js', {
    '../../utils/api.js': {
      search: (p) => { calls.push(plain(p)); return search ? search(p) : Promise.resolve({ total: 1, total_all: 1, items: [DISTRICT_DOC] }); },
      createFollow: createFollow || (() => Promise.resolve({ id: 9 })),
    },
    '../../utils/regions.js': regions, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/exporter.js': { exportDocs: (ids, opts) => { exported.push({ ids, opts }); return Promise.resolve(true); }, MAX: 200 },
    '../../utils/consent.js': { loginOpen: () => true, ensureConsent: () => Promise.resolve(true) },
    '../../utils/subscribe.js': { remindOpen: () => false, requestOnce: () => Promise.resolve(false) },
    '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: typeof coverage === 'function' ? coverage : () => Promise.resolve(coverage), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: { showModal: (o) => o.success({ confirm: true, content: '' }), showToast() {}, ...wx } });
  return { page, calls, exported };
}
const chipLabels = (page) => plain(page.data.activeChips.map((c) => c.label));

function followPage(items, extra = {}) {
  const targets = [];
  const page = load('pages/follow/index.js', {
    '../../utils/api.js': { myFollows: () => Promise.resolve({ items, ...extra }), mySubscriptions: () => Promise.resolve({ items: [] }), followNew: () => Promise.resolve({ items: [] }) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/subscribe.js': { remindOpen: () => false, requestOnce: () => Promise.resolve(false) },
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/conditions.js': conditions,
  }, { wx: { navigateTo: ({ url }) => targets.push(url) } });
  return { page, targets };
}

test('省市区选择：三级值、每列“全部”、直辖市、区名照录、省直管占位、港澳与非法名称', () => {
  const sel = (...values) => plain(regions.regionSelection(values));
  assert.deepEqual(sel('山东省', '青岛市', '市南区'), { province: '山东', city: '青岛', district: '市南区', note: '' });
  assert.deepEqual(sel('湖南省', '长沙市', '长沙县'), { province: '湖南', city: '长沙', district: '长沙县', note: '' }, '区县名不去掉“县”');
  assert.deepEqual(sel('山东省', '青岛市', '全部'), { province: '山东', city: '青岛', district: '', note: '' });
  assert.deepEqual(sel('山东省', '全部', '全部'), { province: '山东', city: '', district: '', note: '' });
  assert.deepEqual(sel('全部', '全部', '全部'), { province: '', city: '', district: '', note: '' });
  // 直辖市：选了区县才保留市（本市简称），区县不挪到城市；没选区县按省级
  assert.deepEqual(sel('北京市', '北京市', '朝阳区'), { province: '北京', city: '北京', district: '朝阳区', note: '' });
  assert.deepEqual(sel('重庆市', '县', '城口县'), { province: '重庆', city: '重庆', district: '城口县', note: '' });
  for (const values of [['北京市', '北京市', '全部'], ['北京市', '全部', '全部'], ['北京市', '市辖区']]) {
    const choice = sel(...values);
    assert.equal(choice.city, ''); assert.equal(choice.district, ''); assert.match(choice.note, /直辖市/);
  }
  assert.throws(() => sel('河南省', '省直辖县级行政区划', '济源市'), /无法确定所属城市/);
  assert.throws(() => sel('河南省', '省直辖县级行政区划', '全部'), /具体城市/);
  assert.throws(() => sel('山东省', '全部', '市南区'), /无法确定区县/);
  assert.throws(() => sel('河北省', '石家庄市', '市辖区'), /没有给出具体区县/);
  assert.throws(() => sel('香港特别行政区', '香港岛', '中西区'), /暂不在/);
  for (const bad of ['市南区、崂山区', '市南|崂山', `${'区'.repeat(33)}`, '市南\t区']) assert.throws(() => sel('山东省', '青岛市', bad), /区县名称无效/, bad);
  // 收录范围记"青岛市"时沿用来源写法，区县名仍须完全一致
  assert.equal(regions.regionSelection(['山东省', '青岛市', '市南区'], { districts: [{ province: '山东', city: '青岛市', district: '市南区' }] }).city, '青岛市');
  assert.equal(regions.regionSelection(['山东省', '青岛市', '市南区'], { districts: [{ province: '山东', city: '青岛市', district: '市南' }] }).city, '青岛');
  assert.deepEqual(plain(regions.regionValue('山东')), ['山东省', '全部', '全部']); assert.deepEqual(plain(regions.regionValue('国家部委')), []);
  assert.equal(regions.areaLabel('北京/北京/朝阳区'), '北京·朝阳区'); assert.equal(regions.areaLabel('山东/青岛/市南区'), '山东·青岛·市南区');
});

test('区县收录只看自己的省市区三项：同名区县不同省市不混，城市只去末尾“市”，区名精确；收录范围缺失一律未确认', () => {
  const keys = ['江苏/南京/鼓楼区', '江苏/徐州/鼓楼区', '福建/福州/鼓楼区', '北京/北京/朝阳区', '吉林/长春/朝阳区', '山东/青岛/市南区', '山东/青岛市/市南区', '山东/青岛/市南'];
  assert.deepEqual(plain(regions.uncoveredDistricts(keys, COVERAGE)), ['江苏/徐州/鼓楼区', '吉林/长春/朝阳区', '山东/青岛/市南']);
  // 收录范围没取到、旧服务没有 districts：不能当作已收录
  for (const coverage of [null, undefined, {}, { cities: [{ province: '山东', city: '青岛' }] }]) {
    assert.deepEqual(plain(regions.uncoveredDistricts(['山东/青岛/市南区'], coverage)), ['山东/青岛/市南区']);
  }
  // 直辖市的来源把市写成空或"北京市"都按本市
  assert.deepEqual(plain(regions.uncoveredDistricts(['北京/北京/朝阳区'], { districts: [{ province: '北京', city: '', district: '朝阳区' }] })), []);
  assert.deepEqual(plain(regions.uncoveredDistricts(['北京/北京/朝阳区'], { districts: [{ province: '北京', city: '北京市', district: '朝阳区' }] })), []);
  // 并入多选：同名区县各留一条；同一处只留一条
  let area = { regions: ['江苏', '福建'], cities: [], districts: [] };
  for (const values of [['江苏省', '南京市', '鼓楼区'], ['福建省', '福州市', '鼓楼区'], ['江苏省', '徐州市', '鼓楼区'], ['江苏省', '南京市', '鼓楼区']]) {
    area = regions.applyAreaChoice(area, regions.regionSelection(values));
  }
  assert.deepEqual(plain(area), { regions: [], cities: [], districts: ['江苏/南京/鼓楼区', '福建/福州/鼓楼区', '江苏/徐州/鼓楼区'] });
  assert.match(conditions.describe({ kind: 'all', districts: area.districts.join(',') }), /江苏·南京·鼓楼区、福建·福州·鼓楼区、江苏·徐州·鼓楼区/);
});

test('搜索三级选到区县：请求、已选条、弹层、关注保存、关注页回跳和导出都带区县；区县替换同市和全省，别省保留', async () => {
  let saved;
  const { page, calls, exported } = searchPage({ createFollow: (name, c) => { saved = { name, conditions: plain(c) }; return Promise.resolve({ id: 7 }); } });
  page.onLoad({}); await settle();
  page.pickOption(tap({ group: 'region', option: '江苏' })); page.pickOption(tap({ group: 'region', option: '山东' }));
  page.chooseCity(pick('山东省', '青岛市', '全部')); await settle();
  assert.deepEqual(plain(page.data.picked.region), ['江苏']); assert.deepEqual(plain(page.data.picked.city), ['山东/青岛']);
  page.chooseCity(pick('山东省', '青岛市', '市南区')); await settle();
  assert.deepEqual(plain(page.data.picked.city), [], '具体区县替换同市的全市条件');
  assert.deepEqual(plain(page.data.picked.district), ['山东/青岛/市南区']);
  const sent = calls.at(-1);
  assert.equal(sent.districts, '山东/青岛/市南区'); assert.equal(sent.regions, '江苏'); assert.equal(sent.cities, undefined);
  assert.deepEqual(chipLabels(page), ['江苏', '山东·青岛·市南区']); assert.equal(page.data.counts.city, 1);
  assert.equal(page.data.entries.region.on, true);
  page.setData({ filterOpen: true, activeGroup: 'city' });
  const sheet = render('pages/search/index.wxml', page.data);
  const removable = findAll(sheet, (n) => n.attrs['data-group'] === 'district' && n.attrs.bindtap === 'removePicked');
  assert.deepEqual(removable.map((n) => n.attrs['data-option']), ['山东/青岛/市南区', '山东/青岛/市南区']);
  assert.match(textOf(sheet), /省市区选项由微信提供，选项齐全不代表已收录数据/);
  // 关注：保存的条件带区县，名称、摘要写出区县
  page.follow(); await settle();
  assert.equal(saved.conditions.districts, '山东/青岛/市南区'); assert.equal(saved.conditions.cities, undefined);
  assert.match(saved.name, /江苏、山东·青岛·市南区/);
  // 关注页：卡片摘要有区县；回搜索时网址带区县，打开后条件完全一样
  const follow = followPage([{ id: 7, name: saved.name, newCount: 0, conditions: saved.conditions }]);
  follow.page.loadLive(); await settle();
  assert.match(follow.page.data.follows[0].desc, /江苏、山东·青岛·市南区/);
  follow.page.searchFollow(tap({ id: 7 }));
  const back = searchPage();
  back.page.onLoad(Object.fromEntries(new URL(follow.targets.at(-1), URL_BASE).searchParams)); await settle();
  assert.deepEqual(plain(back.page.conditions()), plain(page.conditions()));
  assert.equal(back.calls.at(-1).districts, '山东/青岛/市南区');
  assert.deepEqual(chipLabels(back.page), ['江苏', '山东·青岛·市南区']);
  // 导出：选中的文件按编号导出，导出记录名称写出区县
  page.select(); page.pickAll(); page.exportChosen();
  assert.deepEqual(plain(exported[0].ids), ['31']); assert.match(exported[0].opts.label, /山东·青岛·市南区/);
});

test('北京/朝阳区：区县键保留本市简称，标签不重复，区县不挪到城市；同名朝阳区不同省市分开', async () => {
  const { page, calls } = searchPage();
  page.onLoad({ regions: '北京' }); await settle();
  page.chooseCity(pick('北京市', '北京市', '朝阳区')); await settle();
  assert.deepEqual(plain(page.data.picked.region), []); assert.deepEqual(plain(page.data.picked.city), []);
  assert.deepEqual(plain(page.data.picked.district), ['北京/北京/朝阳区']);
  assert.equal(calls.at(-1).districts, '北京/北京/朝阳区'); assert.ok(chipLabels(page).includes('北京·朝阳区'));
  page.chooseCity(pick('吉林省', '长春市', '朝阳区')); await settle();
  assert.deepEqual(plain(page.data.picked.district), ['北京/北京/朝阳区', '吉林/长春/朝阳区']);
  assert.match(page.data.error, /吉林·长春·朝阳区暂未收录区县来源/); assert.doesNotMatch(page.data.error, /北京·朝阳区/);
  page.removePicked(tap({ group: 'district', option: '吉林/长春/朝阳区' })); await settle();
  assert.deepEqual(plain(page.data.picked.district), ['北京/北京/朝阳区']); assert.equal(page.data.error, '');
  // 区县一栏选“全部”：直辖市按省级，替换本市已选区县
  page.chooseCity(pick('北京市', '北京市', '全部')); await settle();
  assert.deepEqual(plain(page.data.picked.region), ['北京']); assert.deepEqual(plain(page.data.picked.district), []);
  assert.match(page.data.cityError, /直辖市/);
  // 只选区县再加层级国家部委：请求、保存再打开都一样（旧条件迁移不掉区县）
  const level = searchPage();
  assert.ok(level.page.data.groups.find((g) => g.key === 'level').options.includes('区县级'));
  level.page.onLoad({}); await settle();
  level.page.chooseCity(pick('北京市', '北京市', '朝阳区')); level.page.pickOption(tap({ group: 'level', option: '国家部委' })); await settle();
  const saved = plain(level.page.conditions());
  assert.deepEqual([saved.regions, saved.levels, saved.districts], ['国家部委', undefined, '北京/北京/朝阳区']);
  const reopened = searchPage();
  reopened.page.onLoad(Object.fromEntries(new URL(conditions.searchUrl(saved), URL_BASE).searchParams)); await settle();
  assert.deepEqual(plain(reopened.page.conditions()), saved);
  assert.deepEqual(plain(reopened.page.data.picked.level), ['国家部委']); assert.deepEqual(plain(reopened.page.data.picked.region), []);
});

test('清掉最后一个区县恢复不限地区；有无关省市时只去掉这个区县；去掉最后一个省份不清掉已选城市区县', async () => {
  const { page, calls } = searchPage();
  page.onLoad({}); await settle();
  page.chooseCity(pick('山东省', '青岛市', '市南区')); await settle();
  assert.deepEqual(plain(page.data.picked.region), []);
  page.removePicked(tap({ group: 'district', option: '山东/青岛/市南区' })); await settle();
  assert.deepEqual(plain(page.data.picked.region), ['不限地区']); assert.equal(page.data.pickedMap['region:不限地区'], true);
  assert.equal(calls.at(-1).districts, undefined); assert.equal(calls.at(-1).regions, '不限地区');
  page.pickOption(tap({ group: 'region', option: '江苏' }));
  page.chooseCity(pick('广东省', '深圳市', '全部'));
  page.chooseCity(pick('山东省', '青岛市', '市南区')); await settle();
  page.removePicked(tap({ group: 'district', option: '山东/青岛/市南区' })); await settle();
  assert.deepEqual(plain(page.data.picked.region), ['江苏']); assert.deepEqual(plain(page.data.picked.city), ['广东/深圳']);
  page.chooseCity(pick('北京市', '北京市', '朝阳区'));
  page.removePicked(tap({ group: 'region', option: '江苏' })); await settle();
  assert.deepEqual(plain(page.data.picked.region), []); assert.deepEqual(plain(page.data.picked.city), ['广东/深圳']);
  assert.deepEqual(plain(page.data.picked.district), ['北京/北京/朝阳区']);
  // 选全省替换该省已选城市、区县；别省的区县保留
  page.chooseCity(pick('广东省', '全部', '全部')); await settle();
  assert.deepEqual(plain(page.data.picked.region), ['广东']); assert.deepEqual(plain(page.data.picked.city), []);
  assert.deepEqual(plain(page.data.picked.district), ['北京/北京/朝阳区']);
  // 省份一栏选“全部”、占位和港澳：不改已选条件，写明原因
  const before = plain(page.data.picked);
  for (const [values, message] of [[['全部', '全部', '全部'], /不限地区/], [['河南省', '省直辖县级行政区划', '济源市'], /无法确定所属城市/], [['香港特别行政区', '香港岛', '中西区'], /暂不在/]]) {
    page.chooseCity(pick(...values)); assert.match(page.data.cityError, message); assert.deepEqual(plain(page.data.picked), before);
  }
});

test('区县没有收录、收录范围没取到、旧服务没有区县范围、网络失败：保留条件，明确未查询，不改为全市或全省', async () => {
  // 收录范围里没有这个区县
  const none = searchPage({ coverage: { cities: [], districts: [] } });
  none.page.onLoad({}); await settle(); const before = none.calls.length;
  none.page.chooseCity(pick('山东省', '烟台市', '芝罘区')); await settle();
  assert.equal(none.calls.length, before); assert.match(none.page.data.error, /山东·烟台·芝罘区暂未收录区县来源/);
  assert.match(none.page.data.error, /未查询不代表没有相关通知/); assert.equal(none.page.conditions().districts, '山东/烟台/芝罘区');
  assert.match(none.page.data.cityCoverageNote, /尚未接入区县来源/);
  // 收录范围还在取：先不查；取回、确认收录后才查
  let release;
  const waiting = searchPage({ coverage: () => new Promise((resolve) => { release = resolve; }) });
  waiting.page.onLoad({ districts: encodeURIComponent('山东/青岛/市南区') }); await settle();
  assert.equal(waiting.calls.length, 0); assert.equal(waiting.page.data.loading, true);
  release(COVERAGE); await settle();
  assert.equal(waiting.calls.length, 1); assert.equal(waiting.calls[0].districts, '山东/青岛/市南区');
  // 收录范围没取到（null）或旧服务没给 districts：不查；收录范围恢复后点重试才查
  for (const failed of [null, { cities: [] }]) {
    let current = failed;
    const off = searchPage({ coverage: () => Promise.resolve(current) });
    off.page.onLoad({ districts: '山东/青岛/市南区', regions: '江苏' }); await settle();
    assert.equal(off.calls.length, 0); assert.match(off.page.data.error, /区县收录范围没有取到/); assert.match(off.page.data.error, /不会改为全市或全省/);
    assert.equal(off.page.conditions().districts, '山东/青岛/市南区');
    current = COVERAGE; off.page.retry(); await settle();
    assert.equal(off.calls.at(-1).districts, '山东/青岛/市南区'); assert.equal(off.page.data.error, '');
  }
  // 断网：报错、清空旧结果，不改发不带区县的请求
  const down = searchPage({ search: () => Promise.reject(new Error('网络连接失败，请检查网络后重试')) });
  down.page.onLoad({ districts: '山东/青岛/市南区' }); await settle();
  assert.match(down.page.data.error, /网络连接失败/); assert.equal(down.page.data.items.length, 0);
  assert.ok(down.calls.length >= 1 && down.calls.every((p) => p.districts === '山东/青岛/市南区'));
});

test('关注摘要和暂不可查询的区县关注：条件卡写出区县、newCount 为 null 不当 0，点卡片回搜索带区县', async () => {
  const c = { kind: '申报通知', regions: '', districts: '山东/青岛/市南区,北京/北京/朝阳区', time: '不限' };
  const message = '关注范围暂不可查询，请重新选择地区或等待来源恢复';
  const { page, targets } = followPage([{ id: 3, name: '区县关注', conditions: c, newCount: null, queryUnavailable: true, queryMessage: message }], { newTotal: null, availableNewTotal: 0 });
  page.loadLive(); await settle();
  assert.equal(page.data.newTotal, null); assert.equal(page.data.follows[0].newCount, null);
  const card = textOf(findAll(render('pages/follow/index.wxml', page.data), hasClass('sub'))[0]);
  assert.match(card, /山东·青岛·市南区、北京·朝阳区/); assert.match(card, new RegExp(message)); assert.doesNotMatch(card, /新 \d/);
  page.openFollow(tap({ id: 3, name: '区县关注' }));
  // 空的 regions 不写进网址（searchUrl 一直如此），其余原样
  assert.deepEqual(Object.fromEntries(new URL(targets.at(-1), URL_BASE).searchParams), { kind: '申报通知', districts: c.districts, time: '不限' });
  assert.match(conditions.describe({ kind: 'all', types: '名单与公示', status: '公示', depts: '科技部门', cities: '江苏/苏州', districts: '北京/北京/朝阳区' }),
    /江苏·苏州、北京·朝阳区 · 名单与公示 · 名单状态：公示 · 发文部门：科技部门/);
});

function localWx(initial) {
  const stored = initial ? plain(initial) : {};
  const calls = [];
  return { stored, calls, wx: {
    getStorageSync: (k) => (stored[k] ? plain(stored[k]) : ''),
    setStorageSync: (k, v) => { stored[k] = plain(v); },
    showToast: (o) => calls.push(o.title), showModal: (o) => o.success({ confirm: true }),
    navigateTo: (o) => calls.push(o.url), pageScrollTo() {},
  } };
}
function clientUtils(wx) {
  const module = { exports: {} };
  const deps = { './format.js': format, './regions.js': regions, './conditions.js': conditions };
  vm.runInNewContext(read('utils/clients.js'), { module, wx, require: (name) => { assert.ok(Object.hasOwn(deps, name), name); return deps[name]; } });
  return module.exports;
}
function clientsPage(local, { search, coverage = COVERAGE, exported = [] } = {}) {
  const calls = [];
  const page = load('pages/clients/index.js', {
    '../../utils/regions.js': regions,
    '../../utils/api.js': { search: (p) => { calls.push(plain(p)); return search ? search(p) : Promise.resolve({ items: [], total: 0 }); } },
    '../../utils/clients.js': clientUtils(local.wx),
    '../../utils/coverage.js': { loadCoverage: typeof coverage === 'function' ? coverage : () => Promise.resolve(coverage) },
    '../../utils/exporter.js': { exportDocs: (ids, opts) => { exported.push({ ids, opts }); return Promise.resolve(true); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { wx: local.wx });
  return { page, calls, exported };
}
const doc = (id) => ({ id, title: `申报通知${id}`, kind: '申报通知', date: '2026-10-01' });

test('本机客户：三级选取→保存→重开删除→请求→结果导出都带区县；签名含区县，选省清掉该省城市区县', async () => {
  const local = localWx(); const exported = [];
  const { page, calls } = clientsPage(local, { exported, search: (p) => Promise.resolve({ items: p.districts ? [doc('1'), doc('2')] : [doc('2')], total: 2 }) });
  page.onLoad({}); page.onShow(); await settle();
  page.edit(tap({}));
  page.pick(tap({ group: 'regions', value: '江苏' })); page.pick(tap({ group: 'regions', value: '山东' }));
  page.chooseCity(pick('山东省', '青岛市', '市南区'));
  page.chooseCity(pick('北京市', '北京市', '朝阳区'));
  page.pick(tap({ group: 'industries', value: '数字经济' }));
  assert.deepEqual(plain(page.data.form.regions), ['江苏']);
  assert.deepEqual(plain(page.data.form.districts), ['山东/青岛/市南区', '北京/北京/朝阳区']);
  const editor = render('pages/clients/index.wxml', page.data);
  assert.deepEqual(findAll(editor, (n) => n.attrs.bindtap === 'removeDistrict').map(textOf), ['山东·青岛·市南区', '北京·朝阳区']);
  page.nameInput({ detail: { value: '合成甲公司' } }); page.save(); await settle();
  const [saved] = local.stored.youjuLocalClientsV1;
  assert.deepEqual(saved.districts, ['山东/青岛/市南区', '北京/北京/朝阳区']); assert.deepEqual(saved.regions, ['江苏']);
  assert.equal(page.data.clients[0].summary, '江苏、山东·青岛·市南区、北京·朝阳区（含国家部委） · 数字经济');
  assert.deepEqual(calls.at(-1), { kind: '申报通知', exclude_closed: true, time: '近 90 天', regions: '国家部委,江苏', districts: '山东/青岛/市南区,北京/北京/朝阳区', industries: '数字经济', limit: 50, offset: 0 });
  assert.ok(!JSON.stringify(calls).includes('合成甲公司'));
  // 返回页面、条件没变：不重查；重开删掉一个区县再保存：重查且只带剩下的区县
  const count = calls.length; page.onShow(); await settle(); assert.equal(calls.length, count);
  page.edit(tap({ id: saved.id }));
  assert.deepEqual(plain(page.data.districtLabels.map((d) => d.label)), ['山东·青岛·市南区', '北京·朝阳区']);
  page.removeDistrict(tap({ value: '北京/北京/朝阳区' })); page.save(); await settle();
  assert.deepEqual(local.stored.youjuLocalClientsV1[0].districts, ['山东/青岛/市南区']);
  assert.equal(calls.length, count + 1); assert.equal(calls.at(-1).districts, '山东/青岛/市南区');
  assert.deepEqual(plain(page.data.shown.map((r) => r.id)), ['1', '2']);
  page.exportShown(); assert.deepEqual(plain(exported.at(-1)), { ids: ['1', '2'], opts: { label: '我的客户 · 全部' } });
  // 选全市替换该市区县；选全省替换该省城市和区县
  page.edit(tap({ id: saved.id }));
  page.chooseCity(pick('山东省', '青岛市', '全部'));
  assert.deepEqual(plain(page.data.form.cities), ['山东/青岛']); assert.deepEqual(plain(page.data.form.districts), []);
  page.chooseCity(pick('山东省', '青岛市', '市南区')); page.pick(tap({ group: 'regions', value: '山东' }));
  assert.deepEqual(plain(page.data.form.cities), []); assert.deepEqual(plain(page.data.form.districts), []); assert.deepEqual(plain(page.data.form.regions), ['江苏', '山东']);
  page.cancel();
  assert.deepEqual(local.stored.youjuLocalClientsV1[0].districts, ['山东/青岛/市南区'], '取消编辑不改本机记录');
});

test('本机客户：已收录城市直接加入时也替换该市已选区县和全省', async () => {
  const local = localWx();
  const { page } = clientsPage(local, { coverage: { cities: [{ province: '山东', city: '青岛' }], districts: COVERAGE.districts } });
  page.onLoad({}); page.onShow(); await settle(); page.edit(tap({}));
  page.pick(tap({ group: 'regions', value: '山东' })); page.chooseCity(pick('山东省', '青岛市', '市南区')); page.chooseCity(pick('北京市', '北京市', '朝阳区'));
  page.pick(tap({ group: 'cities', value: '山东/青岛' }));
  assert.deepEqual(plain(page.data.form.cities), ['山东/青岛']); assert.deepEqual(plain(page.data.form.districts), ['北京/北京/朝阳区']); assert.deepEqual(plain(page.data.form.regions), []);
  page.pick(tap({ group: 'cities', value: '山东/青岛' }));
  assert.deepEqual(plain(page.data.form.cities), []); assert.deepEqual(plain(page.data.form.districts), ['北京/北京/朝阳区']);
});

test('旧本机客户迁移：没有区县的记录原样读写、请求不带 districts、键名不变；坏区县不读入，具体区县替换所属城市', () => {
  const OLD = [{ id: 'c1', name: '旧客户', regions: ['山东'], cities: ['江苏/苏州'], industries: ['数字经济'], keywords: ['高新'] }];
  const local = localWx({ youjuLocalClientsV1: OLD });
  const utils = clientUtils(local.wx);
  const [c] = utils.loadClients();
  assert.deepEqual(plain(c), { id: 'c1', name: '旧客户', regions: ['山东'], cities: ['江苏/苏州'], districts: [], industries: ['数字经济'] });
  const params = plain(utils.clientParams(OLD[0]));
  assert.deepEqual(params, { kind: '申报通知', exclude_closed: true, time: '近 90 天', regions: '国家部委,山东', cities: '江苏/苏州', industries: '数字经济' });
  utils.saveClients([c]);
  assert.deepEqual(Object.keys(local.stored), ['youjuLocalClientsV1']);
  assert.deepEqual(local.stored.youjuLocalClientsV1, [{ id: 'c1', name: '旧客户', regions: ['山东'], cities: ['江苏/苏州'], districts: [], industries: ['数字经济'] }]);
  const bad = utils.normalize({ id: 'x', regions: ['山东'], cities: ['山东/青岛', '江苏/苏州'], districts: ['山东/市南区', '山东/青岛/市南区,崂山区', '北京/朝阳/朝阳区', '香港/香港/中西区', '山东/青岛/市南区', '山东/青岛/市南区'] });
  assert.deepEqual(plain(bad.districts), ['山东/青岛/市南区']);
  assert.deepEqual(plain(bad.cities), ['江苏/苏州']); assert.deepEqual(plain(bad.regions), []);
  // 分组：同一个区县的客户只查一次；同名区县不同省市分开查
  const A = { id: 'a', districts: ['江苏/南京/鼓楼区'] }; const B = { id: 'b', districts: ['福建/福州/鼓楼区'] }; const C = { id: 'c', name: '丙', districts: ['江苏/南京/鼓楼区'] };
  const groups = utils.queryGroups([A, B, C]);
  assert.deepEqual(plain(groups.map((g) => [g.ids, g.params.districts, g.params.regions])), [[['a', 'c'], '江苏/南京/鼓楼区', '国家部委'], [['b'], '福建/福州/鼓楼区', '国家部委']]);
  const rows = utils.mergeRelated([A, B, C], [{ ids: ['a', 'c'], items: [doc('5')] }, { ids: ['b'], items: [doc('5'), doc('6')] }]);
  assert.deepEqual(plain(rows.map((r) => [r.id, r.names])), [['5', '客户 1、客户 2、丙'], ['6', '客户 2']]);
});

test('本机客户：区县未收录或收录范围没取到时这一组不查，其他组照查，写明不改为全市或全省', async () => {
  const stored = { youjuLocalClientsV1: [
    { id: 'a', name: '', regions: ['江苏'], cities: [], districts: [], industries: [] },
    { id: 'b', name: '', regions: [], cities: [], districts: ['山东/烟台/芝罘区'], industries: [] },
  ] };
  for (const [coverage, message] of [[null, /区县收录范围没有取到/], [{ cities: [] }, /区县收录范围没有取到/], [COVERAGE, /山东·烟台·芝罘区暂未收录区县来源/]]) {
    const { page, calls } = clientsPage(localWx(stored), { coverage, search: () => Promise.resolve({ items: [doc('9')], total: 1 }) });
    page.onLoad({}); page.onShow(); await settle();
    assert.equal(calls.length, 1); assert.ok(calls.every((p) => p.districts === undefined && p.regions === '国家部委,江苏'));
    assert.match(page.data.loadError, message); assert.match(page.data.loadError, /不会改为全市或全省/);
    assert.deepEqual(plain(page.data.shown.map((r) => r.id)), ['9']);
    assert.equal(page.data.clientErrors.a, undefined); assert.match(page.data.clientErrors.b, message);
    page.pickTab(tap({ tab: 'cards' }));
    const cards = findAll(render('pages/clients/index.wxml', page.data), hasClass('client-card'));
    assert.match(textOf(cards[0]), /近 90 天相关申报通知 1 份/);
    assert.match(textOf(cards[1]), /暂未查询/); assert.doesNotMatch(textOf(cards[1]), /相关申报通知 0 份/);
    page.edit(tap({ id: 'b' })); assert.match(page.data.cityError, coverage && coverage.districts ? /暂未收录区县来源/ : /查询前会再核对/);
  }
});

function reportPage({ coverage = COVERAGE, items } = {}) {
  const requests = []; const targets = [];
  const page = load('pages/report/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': {
      deadlineReport: (p) => { requests.push(['report', plain(p)]); return Promise.resolve({ items: items || [], total: (items || []).length, summary: {}, verified_total: 1 }); },
      calendar: (p) => { requests.push(['calendar', plain(p)]); return Promise.resolve({ items: [], past: [], verifiedTotal: 1 }); },
    },
    '../../utils/regions.js': regions, '../../utils/exporter.js': {}, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/coverage.js': { loadCoverage: typeof coverage === 'function' ? coverage : () => Promise.resolve(coverage) },
  }, { getApp: () => ({ globalData: {} }), wx: { navigateTo: ({ url }) => targets.push(url) } });
  return { page, requests, targets };
}

test('截止列表和按月看：三级选到区县带 province/city/district，未选区县不带 district；直辖市、“全部”、回搜索都保留区县', async () => {
  const items = [{ id: 1, title: '合成区级通知', issuer: '合成机关', province: '山东', city: '青岛', district: '市南区', date: '2026-12-01' }];
  const { page, requests, targets } = reportPage({ items });
  page.onLoad(); await settle();
  assert.ok(requests.every(([, p]) => !('district' in p)), '没选区县时请求和以前一样');
  page.chooseCity(pick('山东省', '青岛市', '市南区')); await settle();
  assert.deepEqual(requests.at(-1)[1], { days: 7, province: '山东', city: '青岛', district: '市南区', dept: '', page: 1, size: 20 });
  assert.equal(page.data.areaText, '青岛 · 市南区'); assert.equal(page.data.rows[0].meta, '合成机关 · 山东 · 青岛 · 市南区');
  assert.match(textOf(render('pages/report/index.wxml', page.data)), /青岛 · 市南区.*改看全省/);
  page.goSearch(); assert.equal(new URL(targets.at(-1), URL_BASE).searchParams.get('districts'), '山东/青岛/市南区');
  page.pickTab(tap({ key: 0 })); await settle();
  assert.deepEqual(requests.at(-1), ['calendar', { province: '山东', city: '青岛', district: '市南区' }]);
  page.chooseCity(pick('北京市', '北京市', '朝阳区')); await settle();
  assert.deepEqual(requests.at(-1), ['calendar', { province: '北京', city: '北京', district: '朝阳区' }]); assert.equal(page.data.areaText, '朝阳区');
  page.chooseCity(pick('北京市', '北京市', '全部')); await settle();
  assert.deepEqual(requests.at(-1), ['calendar', { province: '北京', city: '' }]); assert.match(page.data.cityError, /直辖市/);
  page.pickTab(tap({ key: 7 })); await settle();
  page.chooseCity(pick('山东省', '青岛市', '市南区')); await settle();
  page.clearCity(); await settle();
  assert.deepEqual(requests.at(-1)[1], { days: 7, province: '山东', city: '', dept: '', page: 1, size: 20 });
  assert.deepEqual(plain(page.data.cityRegion), ['山东省', '全部', '全部']);
  page.pickProvince({ detail: { value: page.data.provinces.indexOf('江苏') } }); await settle();
  assert.equal(page.data.district, ''); assert.deepEqual(plain(page.data.cityRegion), ['江苏省', '全部', '全部']);
  page.chooseCity(pick('全部', '全部', '全部')); await settle();
  assert.equal(page.data.province, ''); assert.deepEqual(requests.at(-1)[1], { days: 7, province: '', city: '', dept: '', page: 1, size: 20 });
});

test('截止：区县未收录、收录范围没取到或旧服务没给区县范围时不发请求，写明不改为全市或全省', async () => {
  for (const [coverage, message] of [[COVERAGE, /山东·烟台·芝罘区暂未收录区县来源/], [null, /区县收录范围没有取到/], [{ cities: [] }, /区县收录范围没有取到/], [() => Promise.reject(new Error('断网')), /区县收录范围没有取到/]]) {
    const { page, requests } = reportPage({ coverage });
    page.onLoad(); await settle(); const sent = requests.length;
    page.chooseCity(pick('山东省', '烟台市', '芝罘区')); await settle();
    assert.equal(requests.length, sent); assert.match(page.data.error, message); assert.match(page.data.error, /不会改为全市或全省/);
    assert.equal(page.data.rows.length, 0);
    page.pickTab(tap({ key: 0 })); await settle();
    assert.equal(requests.length, sent); assert.match(page.data.error, message); assert.doesNotMatch(page.data.error, /加载失败/);
  }
});

function dataLogic(api = {}) {
  return load('utils/data-tools.js', { './regions.js': regions, './api.js': api, './advisor.js': advisorHelpers, '../content/advisor-terms.js': terms });
}
const caps = () => ({ data_tools: { enabled: true, features: ['compare', 'track'], remaining: { compare: 3, track: 3 } }, ai_advisor: { enabled: false, features: [] } });
const status = () => ({ enabled: true, pass: { active: false }, quota: { compare: { used: 0, limit: 3, maxRows: 50 }, track: { used: 0, limit: 3, maxRows: 50 } } });
function trackForm({ storage = new Map(), api = {} } = {}) {
  const calls = []; const urls = [];
  const base = {
    advisorCapabilities: () => Promise.resolve(caps()), advisorStatus: () => Promise.resolve(status()),
    me: () => Promise.resolve({ advisorAgreed: { version: terms.version }, taskOwner: OWNER }),
    advisorPreview: (body) => { calls.push(['preview', plain(body)]); return Promise.resolve({ rowCount: body.rows ? body.rows.length : 1 }); },
    createAdvisorTask: (body, key) => { calls.push(['create', plain(body), key]); return Promise.resolve({ id: 8 }); }, ...api,
  };
  const wx = { getStorageSync: (k) => storage.get(k), setStorageSync: (k, v) => storage.set(k, plain(v)), removeStorageSync: (k) => storage.delete(k),
    setNavigationBarTitle() {}, navigateTo: ({ url }) => urls.push(url), redirectTo: ({ url }) => urls.push(url), showToast() {} };
  const page = load('pages/advisor-form/index.js', {
    '../../utils/api.js': base, '../../utils/data-idempotency.js': idempotency, '../../utils/advisor.js': advisorHelpers,
    '../../utils/data-tools.js': dataLogic(base), '../../utils/consent.js': { ensureConsent: () => Promise.resolve(true) },
  }, { wx });
  page.onLoad({ kind: 'track' }); page.onShow();
  return { page, calls, urls, storage };
}

test('客户通知表第 6 列区县：单值校验、直辖市、须有所属城市、国家部委不带；旧四列五列结果与之前完全相同；错误不回显客户内容', () => {
  const logic = dataLogic();
  assert.deepEqual(plain(logic.pasteRows('track', '合成甲,山东省,科技;创新,资金支持,青岛市,市南区\n合成乙,北京,,,北京市,朝阳区\n合成丙,湖南,,,长沙,长沙县')), [
    { name: '合成甲', province: '山东', keywords: '科技;创新', supports: '资金支持', city: '青岛', district: '市南区' },
    { name: '合成乙', province: '北京', keywords: '', supports: '', city: '北京', district: '朝阳区' },
    { name: '合成丙', province: '湖南', keywords: '', supports: '', city: '长沙', district: '长沙县' },
  ]);
  // 旧四列、五列：位置和输出与加区县之前一样（没有 district 键）
  assert.deepEqual(plain(logic.pasteRows('track', '合成甲,山东,科技,资金支持\n合成乙,山东,,,青岛\n合成丙,北京市,,,北京市\n合成丁,山东,,,青岛,')), [
    { name: '合成甲', province: '山东', keywords: '科技', supports: '资金支持', city: '' },
    { name: '合成乙', province: '山东', keywords: '', supports: '', city: '青岛' },
    { name: '合成丙', province: '北京', keywords: '', supports: '', city: '' },
    { name: '合成丁', province: '山东', keywords: '', supports: '', city: '青岛' },
  ]);
  for (const [text, message] of [
    ['合成机密甲,山东,,,,市南区', /^第 1 行填写区县时请同时填写所属城市/], ['合成机密甲,北京,,,,朝阳区', /^第 1 行填写区县时请同时填写所属城市；直辖市的城市填本市/],
    ['合成机密甲,国家部委,,,,朝阳区', /^第 1 行省份为国家部委时城市请留空，区县也请留空/], ['合成机密甲,北京,,,朝阳,朝阳区', /^第 1 行直辖市填写区县时，城市填本市，如北京/],
    ['合成机密甲,山东,,,青岛,市南区、崂山区', /^第 1 行区县只填一个/], ['合成机密甲,山东,,,青岛,市南区|崂山区', /^第 1 行区县只填一个/],
    ['合成机密甲,山东,,,青岛,"市南区,崂山区"', /^第 1 行区县只填一个/], ['合成机密甲,山东,,,青岛,市南区/崂山区', /^第 1 行区县只填一个/],
    [`合成机密甲,山东,,,青岛,${'区'.repeat(33)}`, /^第 1 行区县只填一个/], ['合成机密甲,山东,,,青\t岛,市南区', /^第 1 行城市只填一个/],
    ['合成机密甲,山东,,,青岛,市南\t区', /^第 1 行区县只填一个/],
    ['合成机密甲,山东\n\n合成机密乙,山东,,,青岛,市南区,多', /^第 3 行最多六列/],
  ]) {
    assert.throws(() => logic.pasteRows('track', text), (err) => message.test(err.message) && !/合成机密/.test(err.message), text);
  }
  assert.equal(logic.pasteRows('track', `合成甲,山东,,,青岛,${'区'.repeat(32)}`)[0].district.length, 32);
});

test('粘贴预览和提交带区县；区县进入安全重试摘要，没填区县的内容摘要与升级前相同，未知请求仍用原键', async () => {
  const h = trackForm(); await settle();
  h.page.input({ detail: { value: '合成甲,山东,,,青岛,市南区' } }); assert.equal(h.page.data.rowCount, 1);
  const shown = textOf(render('pages/advisor-form/index.wxml', h.page.data));
  assert.match(shown, /客户名称,省份,关键词,支持方式,城市,区县/); assert.match(shown, /旧四列、五列格式仍有效/);
  h.page.preview(); await settle(); h.page.submit(); await settle();
  assert.deepEqual(h.calls.map((c) => c[0]), ['preview', 'create']);
  assert.deepEqual(h.calls[1][1], { kind: 'track', rows: [{ name: '合成甲', province: '山东', keywords: '', supports: '', city: '青岛', district: '市南区' }] });
  assert.deepEqual(h.calls[0][1], h.calls[1][1]);
  h.page.onUnload();
  const file = trackForm(); await settle(); file.page.mode(tap({ mode: 'file' }));
  assert.match(textOf(render('pages/advisor-form/index.wxml', file.page.data)), /区县（第 6 列，表头写“区县”）/);
  file.page.onUnload();
  // 摘要：区县不同则摘要不同；同一内容同一键
  const logic = dataLogic();
  const store = new Map();
  const s = { getStorageSync: (k) => (store.has(k) ? plain(store.get(k)) : ''), setStorageSync: (k, v) => store.set(k, plain(v)) };
  idempotency.syncOwner(s, OWNER);
  const body = (text) => ({ kind: 'track', rows: logic.pasteRows('track', text) });
  const a = idempotency.claim(s, OWNER, body('合成甲,山东,,,青岛,市南区'), 1000);
  const b = idempotency.claim(s, OWNER, body('合成甲,山东,,,青岛'), 1000);
  const c = idempotency.claim(s, OWNER, body('合成甲,山东,,,青岛,崂山区'), 1000);
  assert.equal(new Set([a.digest, b.digest, c.digest]).size, 3);
  assert.equal(idempotency.claim(s, OWNER, body('合成甲,山东,,,青岛,市南区'), 2000).key, a.key);
  // 升级前的同一请求体（没有 district 键）：升级后同一粘贴内容复用原键，不当作新提交
  const before = { kind: 'track', rows: [{ name: '合成甲', province: '山东', keywords: '', supports: '', city: '青岛' }] };
  assert.deepEqual(plain(body('合成甲,山东,,,青岛')), before);
  const again = idempotency.claim(s, OWNER, before, 3000);
  assert.equal(again.existing, true); assert.equal(again.key, b.key);
  assert.doesNotMatch(JSON.stringify([...store]), /合成甲|市南区|崂山区/);
});

test('双账号未知重试：A 带区县提交结果不明→切到 B→回 A，同一内容沿用 A 的键只建一次，B 另起键', async () => {
  const B = 'b'.repeat(64); let owner = OWNER;
  const storage = new Map(); const server = new Map(); const keys = [];
  const createAdvisorTask = (body, key) => {
    keys.push(key);
    if (!server.has(key)) { server.set(key, { id: server.size + 1, body: plain(body) }); return Promise.reject(Object.assign(new Error('响应丢失'), { status: 504 })); }
    return Promise.resolve({ id: server.get(key).id });
  };
  const h = trackForm({ storage, api: { me: () => Promise.resolve({ advisorAgreed: { version: terms.version }, taskOwner: owner }), createAdvisorTask } }); await settle();
  const send = async (text) => { h.page.input({ detail: { value: text } }); h.page.preview(); await settle(); h.page.submit(); await settle(); };
  await send('合成跨账号企业,北京,,,北京,朝阳区'); assert.match(h.page.data.inputError, /状态尚未确认/);
  owner = B; h.page.onHide(); h.page.onShow(); await settle();
  assert.equal(h.page.data.text, '');
  await send('合成跨账号企业,北京,,,北京,朝阳区'); assert.notEqual(keys[1], keys[0]);
  owner = OWNER; h.page.onHide(); h.page.onShow(); await settle();
  await send('合成跨账号企业,北京,,,北京,海淀区'); assert.notEqual(keys[2], keys[0], '换了区县是新内容，另起键');
  await send('合成跨账号企业,北京,,,北京,朝阳区');
  assert.equal(keys.at(-1), keys[0]); assert.equal(h.page.data.accepted, true);
  assert.equal([...server.values()].filter((t) => t.body.rows[0].district === '朝阳区').length, 2, 'A、B 各一次，A 没有重复建');
  assert.equal(server.get(keys[0]).body.rows[0].city, '北京');
  const saved = storage.get(idempotency.STORAGE_KEY);
  assert.equal(saved.owner, OWNER); assert.deepEqual(saved.others[B].map((e) => e.key), [keys[1]]);
  assert.doesNotMatch(JSON.stringify([...storage]), /合成跨账号企业|朝阳区/); h.page.onUnload();
});

test('真实接口层：搜索、关注、截止、日历、批量和条款对照原样带区县，没选区县时不带', async () => {
  const sent = [];
  const api = load('utils/api.js', { './config.js': { devBaseUrl: URL_BASE, cloudService: 'api' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': format }, {
    wx: { request(o) { sent.push(o); o.success({ statusCode: 200, data: { items: [], total: 0, past: [], pastTotal: 0, id: 1, rowCount: 1 } }); }, removeStorageSync() {} },
    setTimeout() { return 1; }, clearTimeout() {},
  });
  const query = (o) => Object.fromEntries(new URL(o.url).searchParams);
  await api.search({ kind: 'all', regions: '江苏', districts: '山东/青岛/市南区,北京/北京/朝阳区' });
  assert.equal(query(sent.at(-1)).districts, '山东/青岛/市南区,北京/北京/朝阳区');
  await api.deadlineReport({ days: 7, province: '北京', city: '北京', district: '朝阳区', dept: '', page: 1, size: 20 });
  assert.deepEqual([query(sent.at(-1)).province, query(sent.at(-1)).city, query(sent.at(-1)).district], ['北京', '北京', '朝阳区']);
  await api.deadlineReport({ days: 7, province: '山东', city: '', dept: '', page: 1, size: 20 });
  assert.ok(!('district' in query(sent.at(-1))) && !('city' in query(sent.at(-1))));
  await api.calendar({ province: '山东', city: '青岛', district: '市南区' });
  assert.equal(query(sent.at(-1)).district, '市南区');
  await api.createFollow('区县关注', { kind: 'all', districts: '山东/青岛/市南区', cities: undefined });
  assert.deepEqual(JSON.parse(JSON.stringify(sent.at(-1).data)), { name: '区县关注', conditions: { kind: 'all', districts: '山东/青岛/市南区' } });
  const row = { name: '合成企业', province: '山东', keywords: '', supports: '', city: '青岛', district: '市南区' };
  await api.advisorPreview({ kind: 'track', rows: [row] }); assert.deepEqual(plain(sent.at(-1).data), { kind: 'track', rows: [row] });
  await api.createAdvisorTask({ kind: 'track', rows: [row] }, 'track-key'); assert.deepEqual(plain(sent.at(-1).data), { kind: 'track', rows: [row] });
  const company = { region: '北京', city: '北京', district: '朝阳区', employees: 3 };
  await api.createAdvisorTask({ kind: 'clause', doc_id: '42', company }, 'clause-key');
  assert.deepEqual(plain(sent.at(-1).data), { kind: 'clause', doc_id: 42, company });
});

const clauseCaps = () => ({ data_tools: { enabled: false, features: [], remaining: {} }, ai_advisor: { enabled: true, features: ['interpret', 'clause'], remaining: { interpret: 3, clause: 2 } } });
function clauseStorage() {
  const saved = new Map([[interpret.CONSENT_KEY, true]]);
  return { saved, getStorageSync: (key) => saved.get(key), setStorageSync: (key, value) => saved.set(key, value) };
}
function clausePage(api, local) {
  return load('pages/advisor-clause/index.js', {
    '../../utils/regions.js': regions, '../../utils/api.js': api, '../../utils/ai-clause.js': clause,
    '../../utils/tool-context.js': require('../utils/tool-context.js'), '../../utils/format.js': format, '../../utils/advisor.js': advisorHelpers,
  }, { wx: { setNavigationBarTitle() {}, ...local } });
}

test('条款对照注册区县：校验与 company 字段，未填区县时 company 与以前一样；直辖市城市写本市', () => {
  const today = '2026-10-15';
  assert.deepEqual(plain(clause.toCompany({ region: '山东', city: '青岛', district: '市南区' }, today)), { region: '山东', city: '青岛', district: '市南区' });
  assert.deepEqual(plain(clause.toCompany({ region: '北京', city: '北京', district: '朝阳区' }, today)), { region: '北京', city: '北京', district: '朝阳区' });
  assert.deepEqual(plain(clause.toCompany({ ...clause.emptyForm(), region: '山东', city: '青岛' }, today)), { region: '山东', city: '青岛' });
  for (const form of [{ district: '市南区' }, { region: '山东', district: '市南区' }, { region: '山东', city: '青岛', district: '市南区、崂山区' },
    { region: '山东', city: '青岛', district: '区'.repeat(33) }, { region: '北京', city: '朝阳', district: '朝阳区' }]) {
    assert.ok(clause.validateForm(form, today).district, JSON.stringify(form));
  }
  for (const city of ['青岛|烟台', '青岛、烟台', '青岛\\烟台', '青\t岛']) assert.ok(clause.validateForm({ region: '山东', city }, today).city, city);
});

test('条款对照页：三级选区县→提交带 district→失败回表单仍保留→结果标签照显示；清空城市连同区县，企业自述不存本机', async () => {
  const bodies = []; let fail = true;
  const local = clauseStorage();
  const api = {
    advisorCapabilities: () => Promise.resolve(clauseCaps()),
    createAdvisorTask: (body) => { bodies.push(plain(body)); return fail ? Promise.reject(Object.assign(new Error('注册地字段错误'), { status: 422 })) : Promise.resolve({ id: 7, docId: 42, docTitle: '通知', status: 'done', hasResult: false, result: { rows: [{ type: '条件', text: '注册地在本区', refs: [{ pid: 'P002' }], company: [{ key: 'region', label: '注册地', value: '山东 · 青岛 · 市南区' }] }] } }); },
  };
  const page = clausePage(api, local);
  page.onLoad({ docId: 42 }); page.onShow(); await settle();
  page.chooseCity(pick('山东省', '青岛市', '市南区'));
  assert.deepEqual([page.data.form.region, page.data.form.city, page.data.form.district], ['山东', '青岛', '市南区']);
  assert.equal(page.data.provinces[page.data.provinceIndex], '山东');
  assert.match(textOf(render('pages/advisor-clause/index.wxml', page.data)), /青岛 · 市南区/);
  page.submit(); await settle();
  assert.deepEqual(bodies[0].company, { region: '山东', city: '青岛', district: '市南区' });
  page.retry(); assert.equal(page.data.phase, 'form'); assert.equal(page.data.form.district, '市南区');
  fail = false; page.submit(); await settle();
  assert.deepEqual(bodies[1].company, { region: '山东', city: '青岛', district: '市南区' });
  assert.equal(page.data.phase, 'done');
  assert.match(textOf(render('pages/advisor-clause/index.wxml', page.data)), /注册地：山东 · 青岛 · 市南区/);
  assert.doesNotMatch(JSON.stringify([...local.saved]), /市南区|青岛/);
  page.onUnload();
  const form = clausePage(api, clauseStorage());
  form.onLoad({ docId: 42 }); form.onShow(); await settle();
  form.chooseCity(pick('北京市', '北京市', '朝阳区'));
  assert.deepEqual([form.data.form.region, form.data.form.city, form.data.form.district], ['北京', '北京', '朝阳区']);
  form.clear(tap({ field: 'city' })); assert.deepEqual([form.data.form.city, form.data.form.district], ['', '']);
  form.chooseCity(pick('山东省', '青岛市', '市南区')); form.clear(tap({ field: 'district' }));
  assert.deepEqual([form.data.form.city, form.data.form.district], ['青岛', '']);
  form.chooseCity(pick('山东省', '青岛市', '市南区')); form.region({ detail: { value: form.data.provinces.indexOf('江苏') } });
  assert.deepEqual([form.data.form.region, form.data.form.city, form.data.form.district], ['江苏', '', '']); assert.deepEqual(plain(form.data.cityRegion), ['江苏省', '全部', '全部']);
  form.chooseCity(pick('全部', '全部', '全部'));
  assert.deepEqual([form.data.form.region, form.data.form.city, form.data.form.district, form.data.provinceIndex], ['', '', '', 0]);
  form.chooseCity(pick('河南省', '省直辖县级行政区划', '济源市')); assert.match(form.data.errors.city, /无法确定所属城市/);
  form.onUnload();
});

test('来源带区县：列表行、详情、截止按月看的地区不丢区县；地区文字已写区县或没有区县时照录；名单结果的信用代码、批次、序号、企业地区仍显示', async () => {
  assert.equal(format.regionText(DISTRICT_DOC), '青岛·市南区 · 区县级');
  assert.equal(format.regionText({ ...DISTRICT_DOC, region: '市南区 · 区级' }), '市南区 · 区级');
  assert.equal(format.regionText({ region: '青岛 · 市级', city: '青岛' }), '青岛 · 市级');
  assert.equal(format.regionText({ region: '北京 · 区级', province: '北京', city: '北京', district: '朝阳区', level: '区级' }), '北京·朝阳区 · 区级');
  assert.equal(format.regionText({ region: '北京', district: '朝阳区' }), '北京 · 朝阳区');
  const row = load('components/doc-row/index.js', { '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/repost.js': require('../utils/repost.js') });
  row.observers['doc, q, today'].call(row, DISTRICT_DOC, '', '2026-10-15');
  assert.match(textOf(render('components/doc-row/index.wxml', { ...row.data, doc: DISTRICT_DOC })), /青岛·市南区 · 区县级/);
  const detail = load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => false }, '../../utils/api.js': { detail: () => Promise.resolve(plain({ ...DISTRICT_DOC, tags: ['申报通知'], points: [], deadline: null, deadline_state: 'none' })) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => false }, '../../utils/repost.js': require('../utils/repost.js'),
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { wx: {} });
  detail.onLoad({ id: '31' }); await settle();
  assert.match(textOf(render('pages/detail/index.wxml', detail.data)), /青岛·市南区 · 区县级/);
  // 名单结果：doc-row 拿到的通知带区县，名单里的企业字段照常
  const matched = { name: '合成科技有限公司', batch: '第二批', sequence: '12', region: '青岛市市南区', creditCode: '91370200MA00000000', via: 'name' };
  const lists = render('pages/lists/index.wxml', { queried: '合成科技有限公司', result: { items: [{ ...DISTRICT_DOC, kind: '名单', matched }], similar: [], coverage: { lists: 3 } }, items: [], shown: [], chips: [] });
  assert.equal(findAll(lists, (n) => n.tag === 'doc-row')[0].attrs.doc.district, '市南区');
  assert.match(textOf(findAll(lists, hasClass('hit-line'))[0]), /第二批 · 序号 12 · 青岛市市南区 · 信用代码 91370200MA00000000/);
  // 截止按月看：日历条目地区文字里没有区县时补上
  const report = reportPage();
  report.page.onLoad(); await settle();
  report.page.cal = { items: [{ id: 'd1', docId: '31', due: '2099-01-02', verified: true, source: 'verified', title: '区级', what: '企业申报截止', region: '山东·青岛 · 市南区科技局', district: '市南区' },
    { id: 'd2', docId: '32', due: '2099-01-03', verified: true, source: 'verified', title: '区级二', what: '企业申报截止', region: '山东·青岛 · 科技局', district: '崂山区' }], past: [] };
  report.page.setData({ year: 2099, month: 1, tab: 0 }); report.page.showMonth();
  assert.deepEqual(plain(report.page.data.rows.map((r) => r.meta)), ['山东·青岛 · 市南区科技局', '山东·青岛 · 科技局 · 崂山区']);
});

test('收录范围页和我的：区县另列、同名区县按省市分开，并说明选项与收录分开；原生三级选择器和弹层层级不回退，不采集位置', async () => {
  const about = load('pages/about/index.js', {
    '../../content/legal.js': { operator: {}, disclaimer: '' }, '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({ sources: 9, provinces: 3, national: true, regions: [], notCovered: [], updatedAt: null, ...COVERAGE }) },
    '../../utils/format.js': format, '../../utils/consent.js': { loginOpen: () => false, remindAny: () => false },
  });
  about.loadRange(); await settle();
  const text = textOf(render('pages/about/index.wxml', about.data));
  assert.match(text, /覆盖国家部委和 3 个省级地区、4 个区县/);
  assert.match(text, /已收录区县：山东·青岛·市南区（1 个栏目）、北京·朝阳区（2 个栏目）、江苏·南京·鼓楼区（1 个栏目）、福建·福州市·鼓楼区（1 个栏目）/);
  assert.match(text, /省市区选项来自微信，不代表已收录/);
  const me = textOf(render('pages/me/index.wxml', { stats: { sources: 9, provinces: 3, cities: 0, districts: 4 }, loginOpen: false }));
  assert.match(me, /其中已收录 4 个区县的栏目/); assert.doesNotMatch(me, /个城市的栏目/);
  for (const file of ['pages/search/index.wxml', 'pages/clients/index.wxml', 'pages/report/index.wxml', 'pages/advisor-clause/index.wxml']) {
    const source = read(file);
    assert.match(source, /<picker mode="region" custom-item="全部" value="\{\{cityRegion\}\}" bindchange="chooseCity">/, file);
    assert.doesNotMatch(source, /level="(city|province)"/, file);
  }
  assert.match(read('pages/search/index.wxml'), /<t-popup visible="\{\{filterOpen\}\}" placement="bottom" z-index="\{\{900\}\}" overlay-props="\{\{ \{ zIndex: 899 \} \}\}"/);
  const sources = ['app.json', ...['search', 'clients', 'report', 'advisor-clause'].flatMap((p) => [`pages/${p}/index.js`, `pages/${p}/index.wxml`]), 'utils/regions.js'];
  for (const file of sources) assert.doesNotMatch(read(file), /getLocation|chooseLocation|getFuzzyLocation|<map\b|requiredPrivateInfos/, file);
});

test('未收录吉林朝阳区后再选北京全部：两条提示同时可见，再次选择不累计，保存不请求未知区且卡片暂未查询', async () => {
  const local = localWx();
  const { page, calls } = clientsPage(local);
  page.onLoad({}); page.onShow(); await settle();
  const before = calls.length;
  page.edit(tap({}));
  page.chooseCity(pick('吉林省', '长春市', '朝阳区'));
  page.chooseCity(pick('北京市', '北京市', '全部'));
  assert.deepEqual(plain(page.data.form.districts), ['吉林/长春/朝阳区']);
  assert.ok(plain(page.data.form.regions).includes('北京'));
  const uncovered = '暂未收录区县来源';
  const note = '直辖市未选区县时按省级范围查询';
  assert.ok(page.data.cityError.includes(uncovered));
  assert.ok(page.data.cityError.includes(note));
  const editing = textOf(render('pages/clients/index.wxml', page.data));
  assert.ok(editing.includes(uncovered));
  assert.ok(editing.includes(note));
  page.chooseCity(pick('北京市', '北京市', '全部'));
  assert.equal(page.data.cityError.split(note).length - 1, 1);
  assert.ok(page.data.cityError.includes(uncovered));
  assert.deepEqual(plain(page.data.form.districts), ['吉林/长春/朝阳区']);
  page.nameInput({ detail: { value: '合成乙公司' } });
  page.save(); await settle();
  assert.equal(calls.length, before);
  assert.ok(!JSON.stringify(calls).includes('吉林/长春/朝阳区'));
  assert.deepEqual(local.stored.youjuLocalClientsV1[0].districts, ['吉林/长春/朝阳区']);
  page.pickTab(tap({ tab: 'cards' }));
  const card = textOf(render('pages/clients/index.wxml', page.data));
  assert.ok(card.includes('合成乙公司'));
  assert.ok(card.includes('暂未查询'));
});
