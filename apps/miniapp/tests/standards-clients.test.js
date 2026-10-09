const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const standards = require('../content/standards.js');
const clients = require('../utils/clients.js');
const format = require('../utils/format.js');
const { mockSearch } = require('../utils/mockSearch.js');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (v) => JSON.parse(JSON.stringify(v));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const tap = (dataset) => ({ currentTarget: { dataset } });
const input = (value) => ({ detail: { value } });
const forbidden = ['AI', '智能', '大模型', '测评', '评分', '匹配度', '推荐', '通过率'];
const daywatch = { watchDay() {}, stopDay() {} };

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
    module, wx, require: () => require('../utils/conditions.js'),
  });
  return module.exports;
}
function standardsPage(local) {
  return load('pages/standards/index.js', {
    '../../content/standards.js': standards, '../../utils/copy.js': { copyLink: (url) => local.calls.push(url) },
  }, { wx: local.wx });
}
function editorPage(local, coverage = { cities: [{ province: '山东', city: '青岛' }] }) {
  return load('pages/clients/index.js', {
    '../../utils/clients.js': clientUtils(local.wx), '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(coverage) },
  }, { wx: local.wx });
}
function resultsPage(local, api) {
  return load('pages/client-results/index.js', {
    '../../utils/api.js': api, '../../utils/clients.js': clientUtils(local.wx),
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/conditions.js': require('../utils/conditions.js'),
  }, { wx: local.wx });
}
const CLIENT = { id: 'c1', name: '本机客户名称', regions: ['山东'], cities: [], industries: ['数字经济'], keywords: ['高新技术企业'] };
const DOC = { id: '7', title: '申报通知', kind: '申报通知' };
const CONFIRMED = { ...DOC, apply_conditions: { checked: '2026-10-08', items: [{ quote: '申报单位须为高新技术企业。', text: '不显示的概括' }] } };

test('四项内容与负责人 JSON 全字段逐字一致，仅增加 verified_on；条件条数与基准一致', () => {
  const source = JSON.parse(fs.readFileSync(path.join(ROOT, '../../docs/数据来源/认定条件原文_2026-10-08.json'), 'utf8'));
  const expected = plain(source);
  expected.items.forEach((v) => { v.verified_on = '2026-10-08'; });
  assert.deepEqual(standards, expected);
  assert.deepEqual(standards.items.map((v) => v.conditions.length), [13, 41, 8, 9]);
});

test('四页签实际渲染全部原文、出处和 notes，依据齐全，固定说明置顶可见', () => {
  const local = localWx(); const page = standardsPage(local); page.onLoad({});
  for (const standard of standards.items) {
    page.pickTab(tap({ key: standard.key }));
    const root = render('pages/standards/index.wxml', page.data);
    assert.deepEqual(findAll(root, hasClass('original')).map(textOf), standard.conditions.map((v) => v.text));
    assert.deepEqual(findAll(root, hasClass('ref')).map(textOf), standard.conditions.map((v) => v.ref));
    assert.deepEqual(findAll(root, hasClass('source-note')).map(textOf), standard.notes);
    const all = textOf(root);
    for (const value of [standard.document.title, standard.document.number, standard.document.issuer, standard.document.effective || '自发布之日起实施（未明示具体日期）', '核对日期 2026-10-08', '复制原文链接']) assert.ok(all.includes(value), `缺少依据 ${value}`);
    assert.equal(findAll(root, hasClass('reading-note')).length, 1);
    page.copyOriginal(); assert.equal(local.calls.at(-1), standard.document.url);
  }
  assert.match(read('pages/standards/index.wxss'), /\.sticky-head[^}]*position: sticky;[^}]*top: 0;/);
});

test('勾选只存本机，跨页签和重进保留；清空所有资质勾选；没有统计分享导出', () => {
  const local = localWx(); let page = standardsPage(local); page.onLoad({ key: 'kejixing' });
  page.toggle(tap({ index: 0 }));
  page.pickTab(tap({ key: 'gaoqi' })); page.toggle(tap({ index: 2 }));
  page = standardsPage(local); page.onLoad({ key: 'kejixing' }); assert.equal(page.data.rows[0].checked, true);
  page.pickTab(tap({ key: 'gaoqi' })); assert.equal(page.data.rows[2].checked, true);
  page.clearChecks(); assert.ok(page.data.rows.every((v) => !v.checked));
  page.pickTab(tap({ key: 'kejixing' })); assert.ok(page.data.rows.every((v) => !v.checked));
  for (const word of ['count', 'percent', 'progress', 'onShare', 'export', '报告', '达标']) {
    assert.ok(!read('pages/standards/index.js').includes(word)); assert.ok(!read('pages/standards/index.wxml').includes(word));
  }
});

test('本机读取或写入失败不伪报保存，不覆盖旧勾选', () => {
  const local = localWx(); const page = standardsPage(local); page.onLoad({});
  local.wx.setStorageSync = () => { throw new Error('quota'); };
  page.toggle(tap({ index: 0 })); assert.equal(page.data.rows[0].checked, false);
  assert.ok(local.calls.at(-1).includes('保存失败'));
  local.wx.getStorageSync = () => { throw new Error('read'); };
  page.restore(); page.toggle(tap({ index: 0 })); assert.equal(page.data.rows[0].checked, false); assert.ok(page.data.error);
});

test('新增、编辑、删除卡片，仅本机保存，空名称显示客户 1；省市产业多选', async () => {
  const local = localWx(); const page = editorPage(local); page.onLoad(); page.onShow(); await flush();
  page.edit(tap({})); assert.equal(page.data.placeholder, '客户 1');
  page.pick(tap({ group: 'regions', value: '山东' })); page.pick(tap({ group: 'regions', value: '江苏' }));
  page.pick(tap({ group: 'cities', value: '山东/青岛' }));
  for (const value of ['数字经济', '先进制造']) page.pick(tap({ group: 'industries', value }));
  page.keywordInput(input('高新技术企业')); page.save();
  assert.equal(page.data.clients[0].displayName, '客户 1'); assert.equal(page.data.clients[0].regions.length, 2);
  assert.deepEqual(plain(page.data.clients[0].keywords), ['高新技术企业']);
  const id = page.data.clients[0].id;
  page.edit(tap({ id })); page.nameInput(input('客户甲')); page.removeKeyword(tap({ value: '高新技术企业' })); page.save();
  assert.equal(page.data.clients[0].displayName, '客户甲');
  page.search(tap({ id })); assert.equal(local.calls.at(-1), `/pages/client-results/index?id=${id}`);
  assert.ok(!local.calls.at(-1).includes('客户甲'));
  page.remove(tap({ id })); assert.equal(page.data.clients.length, 0);
});

test('20 个客户、5 个关键词、12 字边界；重复词去重，保存未添加的输入', () => {
  const local = localWx(); const utils = clientUtils(local.wx);
  utils.saveClients(Array.from({ length: 20 }, (_, i) => ({ ...CLIENT, id: `c${i}` })));
  const page = editorPage(local); page.onShow(); page.edit(tap({})); assert.equal(page.data.editing, false);
  assert.throws(() => utils.saveClients(Array.from({ length: 21 }, (_, i) => ({ ...CLIENT, id: `c${i}` }))), /20/);
  page.edit(tap({ id: 'c0' })); page.setData({ form: { ...page.data.form, keywords: [] } });
  page.keywordInput(input('一二三四五六七八九十甲乙')); assert.equal(page.addKeyword(), true);
  page.keywordInput(input('一二三四五六七八九十甲乙丙')); assert.equal(page.addKeyword(), false);
  for (const word of ['甲', '乙', '丙', '丁']) { page.keywordInput(input(word)); page.addKeyword(); }
  page.keywordInput(input('戊')); assert.equal(page.addKeyword(), false);
  page.keywordInput(input('甲')); assert.equal(page.addKeyword(), true); assert.equal(page.data.form.keywords.length, 5);
  assert.throws(() => utils.saveClients([{ ...CLIENT, keywords: ['一'.repeat(13)] }]), /12/);
});

test('编辑取消和存储失败保持已有客户；城市收录范围失败可重试', async () => {
  const local = localWx(); clientUtils(local.wx).saveClients([CLIENT]);
  const page = editorPage(local, null); page.onLoad(); page.onShow(); await flush();
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

test('请求只包含筛选参数：省份并国家部委，城市可单独选择；空地区为全国；关键词名称从不上传', () => {
  assert.deepEqual(clients.clientParams(CLIENT, false), { kind: '申报通知', open: undefined, exclude_closed: true, time: '近 90 天', regions: '国家部委,山东', levels: undefined, cities: undefined, industries: '数字经济' });
  const city = clients.clientParams({ ...CLIENT, regions: [], cities: ['山东/青岛'] }, true);
  assert.equal(city.regions, '国家部委'); assert.equal(city.cities, '山东/青岛'); assert.equal(city.industries, undefined);
  assert.equal(clients.clientParams({ ...CLIENT, regions: [] }, false).regions, '不限地区');
  assert.equal(clients.INDUSTRIES.length, 8);
  assert.ok(!JSON.stringify(city).includes(CLIENT.name)); assert.ok(!JSON.stringify(city).includes(CLIENT.keywords[0]));
});

test('关键词仅纯文本高亮，重叠与特殊字符保留原句，未核条件和概括不显示', () => {
  const text = '高新技术企业与高新技术企业；<>&.*';
  const segments = clients.highlightWords(text, ['高新技术企业', '技术企业', '<>&.*']);
  assert.equal(segments.map((v) => v.text).join(''), text);
  assert.deepEqual(segments.filter((v) => v.hit).map((v) => v.text), ['高新技术企业', '高新技术企业', '<>&.*']);
  assert.deepEqual(clients.conditionQuotes({ apply_conditions: { items: [{ quote: '未核对原句' }] } }), []);
  assert.deepEqual(clients.conditionQuotes({ apply_conditions: { checked: '2026-10-08', items: [{ text: '概括' }] } }), []);
  assert.deepEqual(clients.conditionQuotes({ ...CONFIRMED, apply_conditions: { ...CONFIRMED.apply_conditions, items: CONFIRMED.apply_conditions.items.concat(CONFIRMED.apply_conditions.items) } }), ['申报单位须为高新技术企业。']);
});

test('结果复用列表行，点开才取详情，重复展开不重复请求；切换产业不限保留其余条件', async () => {
  const local = localWx(); clientUtils(local.wx).saveClients([CLIENT]); const calls = []; const details = [];
  const page = resultsPage(local, { search: (p) => { calls.push(plain(p)); return Promise.resolve({ items: [DOC], total: 1 }); }, detail: (id) => { details.push(id); return Promise.resolve(CONFIRMED); } });
  page.onLoad({ id: 'c1' }); page.onShow(); await flush(); assert.deepEqual(details, []);
  page.toggleConditions(tap({ id: '7' })); await flush();
  assert.deepEqual(details, ['7']); assert.equal(page.data.states['7'].rows[0].segments.find((v) => v.hit).text, '高新技术企业');
  page.toggleConditions(tap({ id: '7' })); page.toggleConditions(tap({ id: '7' })); assert.deepEqual(details, ['7']);
  assert.match(read('pages/client-results/index.wxml'), /<doc-row doc="{{item}}"/);
  page.toggleIndustry(input(true)); await flush(); assert.equal(calls[1].industries, undefined);
  for (const key of ['regions', 'kind', 'time', 'open']) assert.equal(calls[0][key], calls[1][key]);
  assert.ok(page.data.subtitle.includes('不限产业方向'));
  for (const p of calls) { assert.ok(!JSON.stringify(p).includes(CLIENT.name)); assert.ok(!JSON.stringify(p).includes(CLIENT.keywords[0])); }
});

test('列表自带已核条件不另取详情；无原句显示未核对，详情失败可重试', async () => {
  const local = localWx(); clientUtils(local.wx).saveClients([CLIENT]); let count = 0;
  const page = resultsPage(local, { search: () => Promise.resolve({ items: [CONFIRMED, { ...DOC, id: '8' }], total: 2 }), detail: () => { count += 1; return count === 1 ? Promise.reject(new Error('network')) : Promise.resolve(DOC); } });
  page.onLoad({ id: 'c1' }); page.onShow(); await flush(); page.toggleConditions(tap({ id: '7' })); await flush(); assert.equal(count, 0);
  page.toggleConditions(tap({ id: '8' })); await flush(); assert.ok(page.data.states['8'].error);
  page.toggleConditions(tap({ id: '8' })); await flush(); assert.equal(page.data.states['8'].loaded, true); assert.equal(page.data.states['8'].rows.length, 0);
  const root = render('pages/client-results/index.wxml', page.data);
  assert.ok(textOf(root).includes('条件未核对，见原文'));
});

test('切换时丢弃旧搜索与旧详情；分页不丢弃正在加载的条件；收起期间请求也能完成', async () => {
  const local = localWx(); clientUtils(local.wx).saveClients([CLIENT]); const searches = []; let resolveDetail;
  const page = resultsPage(local, { search: () => new Promise((resolve) => searches.push(resolve)), detail: () => new Promise((resolve) => { resolveDetail = resolve; }) });
  page.onLoad({ id: 'c1' }); page.onShow(); page.toggleIndustry(input(true));
  searches[1]({ items: [DOC], total: 2 }); await flush(); searches[0]({ items: [{ ...DOC, id: 'old' }], total: 1 }); await flush(); assert.equal(page.data.items[0].id, '7');
  page.toggleConditions(tap({ id: '7' })); page.toggleConditions(tap({ id: '7' })); page.loadMore();
  resolveDetail(CONFIRMED); await flush(); assert.equal(page.data.states['7'].loaded, true); assert.equal(page.data.states['7'].open, false);
  searches[2]({ items: [{ ...DOC, id: '8' }], total: 2 }); await flush();
  page.toggleConditions(tap({ id: '8' })); page.toggleIndustry(input(false)); resolveDetail(CONFIRMED); await flush(); assert.deepEqual(plain(page.data.states), {});
  searches[3]({ items: [], total: 0 }); await flush(); assert.equal(page.data.items.length, 0);
});

test('请求失败后不残留旧条件的结果；删除或损坏客户记录不发起检索', async () => {
  const local = localWx(); clientUtils(local.wx).saveClients([CLIENT]); let count = 0;
  const page = resultsPage(local, { search: () => { count += 1; return count === 1 ? Promise.resolve({ items: [DOC], total: 1 }) : Promise.reject(new Error('network')); } });
  page.onLoad({ id: 'c1' }); page.onShow(); await flush(); page.toggleIndustry(input(true)); await flush(); assert.equal(page.data.items.length, 0); assert.ok(page.data.error);
  clientUtils(local.wx).saveClients([]); page.onShow(); assert.equal(count, 2); assert.equal(page.data.client, null);
  local.stored[clients.STORAGE_KEY] = 'corrupted'; page.onShow(); assert.equal(count, 2); assert.ok(page.data.error);
});

test('演示数据城市与省份取并集，包含国家部委，不扩大到整个市所在省', () => {
  const base = { kind: '申报通知', date: '2026-10-08' };
  const docs = [
    { ...base, id: '1', region: '国家', province: '国家' },
    { ...base, id: '2', region: '山东 · 青岛', province: '山东', city: '青岛' },
    { ...base, id: '3', region: '山东 · 济南', province: '山东', city: '济南' },
    { ...base, id: '4', region: '山东 · 省级', province: '山东' },
    { ...base, id: '5', region: '江苏 · 省级', province: '江苏' },
  ];
  const params = { kind: '申报通知', time: '不限', regions: '国家部委', cities: '山东/青岛' };
  assert.deepEqual(mockSearch(docs, params).items.map((v) => v.id), ['1', '2']);
  assert.deepEqual(mockSearch(docs, { ...params, regions: '国家部委,江苏' }).items.map((v) => v.id), ['1', '2', '5']);
});

test('入口、页面注册与 notice-bar 完整；无登录依赖，新增界面文案严格禁用指定字样', () => {
  const app = JSON.parse(read('app.json'));
  const pages = ['standards', 'clients', 'client-results'];
  for (const page of pages) {
    assert.ok(app.pages.includes(`pages/${page}/index`));
    assert.match(read(`pages/${page}/index.wxml`), /<notice-bar \/>/);
    for (const ext of ['js', 'wxml', 'wxss', 'json']) {
      const source = read(`pages/${page}/index.${ext}`);
      for (const word of forbidden) assert.ok(!source.includes(word), `${page} 新增界面含 ${word}`);
      assert.doesNotMatch(source, /ensureConsent|loginOpen|requestOnce|onShareAppMessage|onShareTimeline/);
    }
  }
  // 官方原文独立以一致性测试约束；不为禁词测试删改其中“评分”“推荐”等原句。
  const local = localWx(); const page = standardsPage(local); page.onLoad({});
  const root = render('pages/standards/index.wxml', { ...page.data, rows: [], standard: { ...page.data.standard, notes: [] } });
  for (const word of forbidden) assert.ok(!textOf(root).includes(word));
  const search = read('pages/search/index.wxml');
  assert.match(search, /data-url="\/pages\/standards\/index"/); assert.match(search, /data-url="\/pages\/clients\/index"/);
  assert.match(read('pages/home/index.wxml'), /data-url="\/pages\/search\/index" bindtap="goEntry">[\s\S]*?全部/);
  for (const label of ['认定条件对照', '按客户筛通知']) assert.ok(read('pages/about/index.wxml').includes(label));
});

test('详情入口严格限定项目类别和四类标题，打开对应资质，异常或无关文档清空入口', async () => {
  let doc = { ...DOC, category: '资质与梯度培育' }; const local = localWx();
  const page = load('pages/detail/index.js', {
    '../../utils/api.js': { detail: () => Promise.resolve(doc) }, '../../utils/format.js': format,
    '../../utils/consent.js': { loginOpen: () => false }, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {},
    '../../utils/daywatch.js': daywatch, '../../utils/attachments.js': { decorate: () => [], note: () => '' },
    '../../utils/repost.js': require('../utils/repost.js'),
  }, { wx: local.wx });
  for (const [title, key] of [['高新技术企业通知', 'gaoqi'], ['科技型中小企业通知', 'kejixing'], ['专精特新通知', 'zhuanjingtexin'], ['专精特新“小巨人”通知', 'xiaojuren'], ['小巨人通知', 'xiaojuren'], ['其他通知', '']]) {
    doc = { ...doc, title }; page.onLoad({ id: '7' }); await flush(); assert.equal(page.data.standardKey, key);
    if (key) { page.openStandard(); assert.equal(local.calls.at(-1), `/pages/standards/index?key=${key}`); }
  }
  doc = { ...doc, title: '高新技术企业通知', category: '人才' }; page.onLoad({ id: '7' }); await flush(); assert.equal(page.data.standardKey, '');
});


test('客户默认近90天保留未核对；严格开关关闭后恢复，空结果只显示有结果的出路', async () => {
  const local = localWx(); clientUtils(local.wx).saveClients([CLIENT]);
  const calls = [];
  const page = resultsPage(local, { search: (p) => {
    calls.push(p);
    return Promise.resolve({ items: !p.open && !p.industries ? [DOC] : [], total: !p.open && !p.industries ? 6 : 0, verified_open_total: 0 });
  } });
  page.onLoad({ id: 'c1' }); page.onShow(); await flush();
  assert.equal(calls[0].open, undefined); assert.equal(calls[0].time, '近 90 天'); assert.equal(calls[0].exclude_closed, true);
  assert.equal(page.data.verifiedOnly, false);
  assert.match(page.data.subtitle, /近 90 天发布 · 含截止日期未核对的/);
  const content = textOf(render('pages/client-results/index.wxml', page.data));
  assert.match(content, /不限产业方向（6 份）/); assert.match(content, /今年发布/); assert.match(content, /去搜索页/);
  page.relaxIndustry(); await flush(); assert.equal(page.data.items.length, 1);
  page.toggleVerified(input(true)); await flush(); assert.equal(calls.findLast((p) => p.limit === 20).open, 1);
  assert.equal(page.data.relaxedTotal, 6);
  page.relaxVerified(); await flush(); assert.equal(page.data.items.length, 1);
  page.thisYear(); await flush(); assert.equal(calls.at(-1).time, '今年');
  page.goSearch(); assert.match(local.calls.at(-1), /search\/index/);
});

test('客户没有可用的放宽结果时只保留去搜索页，旧探测不回写新条件', async () => {
  const local = localWx(); clientUtils(local.wx).saveClients([CLIENT]);
  const pending = [];
  const page = resultsPage(local, { search: (p) => p.limit === 1 ? new Promise((resolve) => pending.push(resolve)) : Promise.resolve({ items: [], total: 0 }) });
  page.onLoad({ id: 'c1' }); page.onShow(); await flush();
  page.toggleIndustry(input(true)); await flush();
  pending[0]({ total: 8 }); pending[1]({ total: 9 }); pending[2]({ total: 0 }); await flush();
  assert.equal(page.data.industryTotal, 0); assert.equal(page.data.yearTotal, 0);
  const root = render('pages/client-results/index.wxml', page.data);
  assert.deepEqual(findAll(root, (node) => /exit-btn/.test((node.attrs && node.attrs.class) || '')).map(textOf), ['去搜索页自己筛']);
});
