const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const regions = require('../utils/regions.js');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');

// 我的客户（D-061）：客户卡片只存在本机；所有客户的相关通知按通知合并，写明涉及哪几家、什么时候截止。
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (v) => JSON.parse(JSON.stringify(v));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 6; i += 1) await flush(); };
const tap = (dataset) => ({ currentTarget: { dataset } });
const input = (value) => ({ detail: { value } });
const forbidden = ['AI', '智能', '大模型', '测评', '评分', '匹配度', '推荐', '通过率'];
const daywatch = { watchDay() {}, stopDay() {} };
const plus = (n) => new Date(Date.now() + 8 * 3600e3 + n * 86400e3).toISOString().slice(0, 10);

function load(file, deps, globals = {}) {
  let def;
  vm.runInNewContext(read(file), {
    Page(d) { def = d; },
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    ...globals,
  }, { filename: file });
  return { ...def, data: plain(def.data), setData(v) { Object.assign(this.data, v); } };
}
function localWx() {
  const stored = {};
  const calls = [];
  return { stored, calls, wx: {
    getStorageSync: (k) => stored[k] ? plain(stored[k]) : '',
    setStorageSync: (k, v) => { stored[k] = plain(v); },
    showToast: (o) => calls.push(o.title),
    showModal: (o) => o.success({ confirm: true }),
    navigateTo: (o) => calls.push(o.url), pageScrollTo() {},
  } };
}
function clientUtils(wx) {
  // 本机工具模块在隔离上下文里运行，不污染其他并发测试的全局 wx。
  const module = { exports: {} };
  vm.runInNewContext(read('utils/clients.js'), {
    module, wx, require: (name) => (name === './format.js' ? format : name === './regions.js' ? regions : conditions),
  });
  return module.exports;
}
const clients = clientUtils({});
function clientsPage(local, { search, coverage = { cities: [{ province: '山东', city: '青岛' }] }, exported = [] } = {}) {
  return load('pages/clients/index.js', {
    '../../utils/regions.js': regions,
    '../../utils/api.js': { search: search || (() => Promise.resolve({ items: [], total: 0 })) },
    '../../utils/clients.js': clientUtils(local.wx),
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(coverage) },
    '../../utils/exporter.js': { exportDocs: (ids, opts) => { exported.push({ ids, opts }); return Promise.resolve(true); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { wx: local.wx });
}
const CLIENT = { id: 'c1', name: '本机客户名称', regions: ['山东'], cities: [], industries: ['数字经济'] };
const doc = (id, extra) => ({ id, title: `申报通知${id}`, kind: '申报通知', date: '2026-10-01', ...extra });

test('新增、编辑、删除卡片，仅本机保存，空名称显示客户 1；省市产业多选；看相关通知切到这家客户', async () => {
  const local = localWx(); const page = clientsPage(local); page.onLoad({}); page.onShow(); await settle();
  page.edit(tap({})); assert.equal(page.data.placeholder, '客户 1');
  page.pick(tap({ group: 'regions', value: '山东' })); page.pick(tap({ group: 'regions', value: '江苏' }));
  page.pick(tap({ group: 'cities', value: '山东/青岛' }));
  for (const value of ['数字经济', '先进制造']) page.pick(tap({ group: 'industries', value }));
  page.save(); await settle();
  assert.equal(page.data.clients[0].displayName, '客户 1'); assert.equal(page.data.clients[0].regions.length, 1);
  assert.equal(page.data.clients[0].summary, '江苏、山东·青岛（含国家部委） · 数字经济、先进制造');
  const id = page.data.clients[0].id;
  page.edit(tap({ id })); page.nameInput(input('客户甲')); page.save(); await settle();
  assert.equal(page.data.clients[0].displayName, '客户甲');
  page.setData({ tab: 'cards' }); page.pickOnly(tap({ id }));
  assert.equal(page.data.tab, 'docs'); assert.equal(page.data.only, id);
  page.remove(tap({ id })); await settle(); assert.equal(page.data.clients.length, 0);
  assert.deepEqual(Object.keys(local.stored), ['youjuLocalClientsV1']);
});

test('最多 20 个客户；旧版本存的关键词不再保存', () => {
  const local = localWx(); const utils = clientUtils(local.wx);
  utils.saveClients(Array.from({ length: 20 }, (_, i) => ({ ...CLIENT, id: `c${i}`, keywords: ['高新技术企业'] })));
  assert.ok(local.stored.youjuLocalClientsV1.every((c) => !('keywords' in c)));
  const page = clientsPage(local); page.onShow(); page.edit(tap({})); assert.equal(page.data.editing, false);
  assert.equal(local.calls.at(-1), '最多保存 20 个客户');
  assert.throws(() => utils.saveClients(Array.from({ length: 21 }, (_, i) => ({ ...CLIENT, id: `c${i}` }))), /20/);
});

test('编辑取消和存储失败保持已有客户；城市收录范围失败可重试', async () => {
  const local = localWx(); clientUtils(local.wx).saveClients([CLIENT]);
  const page = clientsPage(local, { coverage: null }); page.onLoad({}); page.onShow(); await settle();
  assert.ok(page.data.coverageError); assert.equal(page.data.cityOptions.length, 0);
  page.edit(tap({ id: 'c1' })); page.nameInput(input('新名字')); page.cancel();
  assert.equal(clientUtils(local.wx).loadClients()[0].name, CLIENT.name);
  page.edit(tap({ id: 'c1' })); page.nameInput(input('新名字'));
  page.setData({ form: { ...page.data.form, cities: ['山东/旧城市'] } });
  page.refreshPicked(); page.removeCity(tap({ value: '山东/旧城市' }));
  assert.equal(page.data.form.cities.length, 0, '收录范围变更后，仍能移除已保存城市');
  local.wx.setStorageSync = () => { throw new Error('quota'); }; page.save();
  assert.equal(page.data.editing, true); assert.ok(page.data.error); assert.equal(clientUtils(local.wx).loadClients()[0].name, CLIENT.name);
});

test('请求只包含筛选参数：省份并国家部委，城市可单独选择；空地区为全国；名称从不上传', () => {
  assert.deepEqual(plain(clients.clientParams(CLIENT)), { kind: '申报通知', exclude_closed: true, time: '近 90 天', regions: '国家部委,山东', industries: '数字经济' });
  const city = clients.clientParams({ ...CLIENT, regions: [], cities: ['山东/青岛'], industries: [] });
  assert.equal(city.regions, '国家部委'); assert.equal(city.cities, '山东/青岛'); assert.equal(city.industries, undefined);
  assert.equal(clients.clientParams({ ...CLIENT, regions: [] }).regions, '不限地区');
  assert.equal(clients.INDUSTRIES.length, 8);
  assert.ok(!JSON.stringify(city).includes(CLIENT.name));
});

test('条件相同的客户只查一次；合并按通知去重，写明涉及哪几家（按卡片顺序），快截止的排前面', () => {
  const list = [CLIENT, { ...CLIENT, id: 'c2', name: '' }, { ...CLIENT, id: 'c3', name: '丙公司', industries: [] }];
  const groups = clients.queryGroups(list);
  assert.equal(groups.length, 2); assert.deepEqual(plain(groups[0].ids), ['c1', 'c2']); assert.deepEqual(plain(groups[1].ids), ['c3']);
  const rows = clients.mergeRelated(list, [
    { ids: ['c3'], items: [doc('1', { date: '2026-10-09' }), doc('2', { due: { source: 'excerpt', date: plus(20), quote: '原句' } })] },
    { ids: ['c1', 'c2'], items: [
      doc('2', { due: { source: 'excerpt', date: plus(20), quote: '原句' } }),
      doc('3', { due: { source: 'verified', date: plus(3) } }),
      doc('4', { due: null, dueSoonUnverified: true }),
      doc('5', { date: '2026-09-01' }),
    ] },
  ]);
  assert.deepEqual(plain(rows.map((r) => r.id)), ['3', '4', '2', '1', '5']);
  assert.deepEqual(plain(rows.map((r) => r.names)), ['本机客户名称、客户 2', '本机客户名称、客户 2', '本机客户名称、客户 2、丙公司', '丙公司', '本机客户名称、客户 2']);
});

test('相关通知：每组条件取 50 份，合并后一条一行；分客户计数，能只看一家，导出只发编号', async () => {
  const local = localWx();
  clientUtils(local.wx).saveClients([CLIENT, { ...CLIENT, id: 'c2', name: '乙公司' }, { ...CLIENT, id: 'c3', name: '丙公司', regions: ['江苏'] }]);
  const calls = []; const exported = [];
  const search = (params) => {
    calls.push(params);
    return Promise.resolve(params.regions.includes('江苏')
      ? { items: [doc('1', { date: '2026-10-09' }), doc('2')], total: 2 }
      : { items: [doc('2', { due: { source: 'verified', date: plus(5) } }), doc('3')], total: 80 });
  };
  const page = clientsPage(local, { search, exported }); page.onLoad({}); page.onShow(); await settle();
  assert.equal(calls.length, 2); assert.ok(calls.every((p) => p.limit === 50 && p.offset === 0));
  assert.ok(!JSON.stringify(calls).includes('乙公司'));
  assert.deepEqual(plain(page.data.shown.map((r) => [r.id, r.names])), [['2', '本机客户名称、乙公司、丙公司'], ['1', '丙公司'], ['3', '本机客户名称、乙公司']]);
  assert.deepEqual(plain(page.data.counts), { c1: 2, c2: 2, c3: 2 });
  assert.equal(page.data.truncated, true);
  const root = render('pages/clients/index.wxml', page.data);
  assert.deepEqual(findAll(root, hasClass('involved')).map(textOf), ['相关客户：本机客户名称、乙公司、丙公司', '相关客户：丙公司', '相关客户：本机客户名称、乙公司']);
  assert.match(textOf(findAll(root, hasClass('export-btn'))[0]), /导出这 3 份/);
  page.pickOnly(tap({ id: 'c3' })); assert.deepEqual(plain(page.data.shown.map((r) => r.id)), ['2', '1']);
  // 卡片上的"看相关通知"总是只看这一家；上面的筛选条再点一次才回到全部
  page.setData({ tab: 'cards' }); page.showClient(tap({ id: 'c3' }));
  assert.equal(page.data.only, 'c3'); assert.equal(page.data.tab, 'docs');
  page.pickOnly(tap({ id: 'c3' })); assert.equal(page.data.only, ''); page.pickOnly(tap({ id: 'c3' }));
  page.exportShown();
  assert.deepEqual(plain(exported[0]), { ids: ['2', '1'], opts: { label: '我的客户 · 丙公司' } });
  // 从详情返回：客户和日期没变，不重新检索
  page.onShow(); await settle(); assert.equal(calls.length, 2);
});

test('相关通知：部分条件没加载出来时提示并能重试；全部失败只写没加载出来；没有客户不发请求', async () => {
  const local = localWx();
  clientUtils(local.wx).saveClients([CLIENT, { ...CLIENT, id: 'c3', regions: ['江苏'] }]);
  let fail = (p) => p.regions.includes('江苏');
  const search = (p) => (fail(p) ? Promise.reject(new Error('网络')) : Promise.resolve({ items: [doc('9')], total: 1 }));
  const page = clientsPage(local, { search }); page.onLoad({}); page.onShow(); await settle();
  assert.equal(page.data.loadError, '有 1 组客户的通知没有加载出来'); assert.equal(page.data.shown.length, 1);
  fail = () => true; page.reload(); await settle();
  assert.equal(page.data.loadError, '相关通知没有加载出来'); assert.equal(page.data.shown.length, 0);
  const empty = localWx(); let called = 0;
  const none = clientsPage(empty, { search: () => { called += 1; return Promise.resolve({ items: [], total: 0 }); } });
  none.onLoad({}); none.onShow(); await settle();
  assert.equal(called, 0);
  assert.match(textOf(render('pages/clients/index.wxml', none.data)), /还没有客户/);
});

test('入口、页面注册与 notice-bar 完整；无登录依赖，界面文案不出现禁用字样；旧的单个客户结果页已删', () => {
  const app = JSON.parse(read('app.json'));
  // D-061：认定条件对照整页删掉（页面、数据文件和入口都不在代码包里）；单个客户的结果页并进"我的客户"
  for (const gone of ['standards', 'client-results', 'calendar', 'list-board']) {
    assert.ok(!app.pages.includes(`pages/${gone}/index`)); assert.ok(!fs.existsSync(path.join(ROOT, `pages/${gone}`)));
  }
  assert.ok(!fs.existsSync(path.join(ROOT, 'content/standards.js')));
  assert.ok(app.pages.includes('pages/clients/index'));
  assert.match(read('pages/clients/index.wxml'), /<notice-bar \/>/);
  for (const ext of ['js', 'wxml', 'wxss', 'json']) {
    const source = read(`pages/clients/index.${ext}`);
    for (const word of forbidden) assert.ok(!source.includes(word), `我的客户含 ${word}`);
    assert.doesNotMatch(source, /ensureConsent|loginOpen|requestOnce|onShareAppMessage|onShareTimeline/);
  }
  assert.equal(JSON.parse(read('pages/clients/index.json')).navigationBarTitleText, '我的客户');
  assert.match(read('pages/home/index.wxml'), /data-url="\/pages\/clients\/index"/);
  const about = read('pages/about/index.wxml');
  assert.ok(about.includes('我的客户')); assert.doesNotMatch(about, /认定条件|按客户筛通知/);
});

test('本机城市保存不受覆盖清单缺失影响，城市替换同省全省、其他省份保留', async () => {
  const local = localWx(); const page = clientsPage(local, { coverage: { cities: [] } });
  page.onLoad({}); page.onShow(); await settle(); page.edit(tap({}));
  page.pick(tap({ group: 'regions', value: '山东' })); page.pick(tap({ group: 'regions', value: '江苏' }));
  page.chooseCity({ detail: { value: ['山东省', '青岛市'] } });
  assert.deepEqual(plain(page.data.form.regions), ['江苏']); assert.deepEqual(plain(page.data.form.cities), ['山东/青岛']);
  assert.match(page.data.cityError, /暂未收录/); page.save(); await settle();
  assert.deepEqual(plain(clientUtils(local.wx).loadClients()[0].cities), ['山东/青岛']);
  assert.match(read('pages/clients/index.wxml'), /picker mode="region" level="city"/);
});
