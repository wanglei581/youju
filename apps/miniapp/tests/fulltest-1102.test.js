// 11/2 完整功能测试（过审版加 AI 包，main d757d617）测出的几处页面问题：
// 第 6 条 查名单没查到时写名单数据更新到哪天（份数和"比对范围"同一个数由服务端保证，见 backend tests）；
// 第 14 条 输入不合要求（422）时不给"重试"；第 9 条 关注卡片名称和条件说明相同时只写一行、引号不套引号。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const AI_OFF = require('./helpers/ai-off.js');
const { render, textOf, findAll, hasClass, tapsTo } = require('./wxml-render.js');

function load(file, deps = {}, globals = {}) {
  let page;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout, Date, Promise,
    require(name) {
      if (Object.hasOwn(deps, name)) return deps[name];
      if (/(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages;
      if (/ai-access\.js$/.test(name)) return AI_OFF;
      throw new Error(`${file} 缺少依赖 ${name}`);
    },
    Page(def) { page = def; }, ...globals,
  }, { filename: file });
  return { ...page, data: JSON.parse(JSON.stringify(page.data)), setData(values) { Object.assign(this.data, JSON.parse(JSON.stringify(values))); } };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const event = (dataset) => ({ currentTarget: { dataset } });
const daywatch = { watchDay() {}, stopDay() {} };

function listsPage(lookup) {
  return load('pages/lists/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': { lists: () => new Promise(() => {}), listBoard: () => new Promise(() => {}), lookup, isLive: () => true },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
  }, { getApp: () => ({ globalData: {} }), wx: { showToast() {} } });
}
const resultCard = (page) => findAll(render('pages/lists/index.wxml', page.data), hasClass('result'))[0];

test('查名单没查到：写"名单数据更新到某天"（和关于页"最近更新"同一个时间，只写日期）；旧服务端没给就不写', async () => {
  // 用今年的日期，免得跨年以后多出年份、测试变红
  const year = format.todayISO().slice(0, 4);
  const coverage = { lists: 230, since: '2026-07-09', unreadLists: 80, partialLists: 0, updatedAt: `${year}-03-05T06:30:00+08:00` };
  const page = listsPage(() => Promise.resolve({ items: [], similar: [], candidates: [], coverage }));
  page.setData({ name: '国家电网有限公司' }); page.query(); await flush();
  assert.equal(page.data.coverageUpdated, '3月5日');
  const text = textOf(resultCard(page));
  assert.match(text, /已收录的 230 份名单中未找到/);
  assert.match(text, /名单数据更新到 3月5日。请核对企业全称再查；2026年7月9日以来的名单份数见下方“比对范围”。/);
  assert.match(text, /另有 80 份名单读不出企业名称/);
  const old = listsPage(() => Promise.resolve({ items: [], similar: [], candidates: [], coverage: { lists: 230, since: '2026-07-09', unreadLists: 0, partialLists: 0 } }));
  old.setData({ name: '国家电网有限公司' }); old.query(); await flush();
  assert.equal(old.data.coverageUpdated, '');
  assert.doesNotMatch(textOf(resultCard(old)), /更新到/);
});

test('查名单输入不合要求（服务端 422，如像人名的）：只写那一句，不给"重试"；网络、限频出错照旧给重试', async () => {
  const refused = Object.assign(new Error('只查企业和机构，请输入企业全称'), { status: 422 });
  let calls = 0;
  const page = listsPage(() => { calls += 1; return Promise.reject(refused); });
  page.setData({ name: '张三' }); page.query(); await flush();
  assert.equal(page.data.lookupError, '只查企业和机构，请输入企业全称');
  assert.equal(page.data.lookupRetry, false);
  let card = resultCard(page);
  assert.match(textOf(card), /只查企业和机构，请输入企业全称/);
  assert.doesNotMatch(textOf(card), /重试/);
  assert.equal(findAll(card, tapsTo('query')).length, 0);
  for (const [err, retry] of [[new Error('网络连接失败，请检查网络后重试'), true], [Object.assign(new Error('查询太频繁，请稍后再试'), { status: 429 }), true],
    [Object.assign(new Error('服务暂时出错（503），请稍后重试'), { status: 503 }), true], [Object.assign(new Error('参数有误'), { status: 400 }), false]]) {
    const p = listsPage(() => Promise.reject(err));
    p.setData({ name: '某某科技有限公司' }); p.query(); await flush();
    assert.equal(p.data.lookupRetry, retry, err.message);
    card = resultCard(p);
    assert.equal(findAll(card, tapsTo('query')).length, retry ? 1 : 0, err.message);
  }
  // 换个输入重新查：上一次的"不给重试"不留着
  const again = listsPage(() => (calls++ ? Promise.reject(new Error('网络连接失败，请检查网络后重试')) : Promise.reject(refused)));
  calls = 0;
  again.setData({ name: '张三' }); again.query(); await flush();
  assert.equal(again.data.lookupRetry, false);
  again.setData({ name: '某某科技有限公司' }); again.query(); await flush();
  assert.equal(again.data.lookupRetry, true);
});

function followPage(items, wx = {}) {
  return load('pages/follow/index.js', {
    '../../utils/api.js': { myFollows: () => Promise.resolve({ items }), mySubscriptions: () => Promise.resolve({ items: [] }), followNew: () => Promise.resolve({ items: [] }) },
    '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/subscribe.js': { remindOpen: () => false }, '../../utils/consent.js': { loginOpen: () => true }, '../../utils/conditions.js': conditions,
  }, { wx });
}

test('关注卡片：没改过的默认名称和条件说明是同一句时只写名称一行；改过名、条件比名称多时两行照写；引号统一用中文引号', async () => {
  const c = { q: '高新技术企业', kind: 'all', regions: '不限地区', time: '近 30 天' };
  // 条件说明的关键词用中文引号，和搜索页默认名称一样
  assert.equal(conditions.describe(c), '“高新技术企业” · 不限地区 · 全部类型 · 近 30 天');
  const items = [
    // 搜索页默认名称（followName）：类型写"全部"
    { id: 1, name: '“高新技术企业” · 不限地区 · 全部 · 近 30 天', newCount: 0, conditions: c },
    { id: 2, name: '我的高企关注', newCount: 0, conditions: c },
    // 发文部门在默认名称里没有：条件说明多出来，两行
    { id: 3, name: '“高新技术企业” · 不限地区 · 全部 · 近 30 天', newCount: 0, conditions: { ...c, depts: '科技部门' } },
  ];
  const page = followPage(items);
  page.loadLive(); await flush(); await flush();
  assert.deepEqual(page.data.follows.map((f) => f.desc), ['', '“高新技术企业” · 不限地区 · 全部类型 · 近 30 天',
    '“高新技术企业” · 不限地区 · 全部类型 · 发文部门：科技部门 · 近 30 天']);
  const cards = findAll(render('pages/follow/index.wxml', page.data), hasClass('sub'));
  assert.equal(findAll(cards[0], hasClass('sub-desc')).length, 0);
  assert.equal(findAll(cards[0], hasClass('sub-name')).length, 1);
  assert.equal(findAll(cards[1], hasClass('sub-desc')).length, 1);
  for (const card of cards) assert.doesNotMatch(textOf(card), /"/, '卡片上不再出现直引号');
  // 旧的直引号写法、多余空格也算同一句
  assert.equal(conditions.sameAsName('"高新技术企业" · 不限地区 · 全部 · 近 30 天', conditions.describe(c)), true);
  assert.equal(conditions.sameAsName('', ''), false);
});

test('取消关注的确认框：名称自己带引号时不再套一层引号；没带的加中文引号', async () => {
  const modals = [];
  const c = { q: '高新技术企业', kind: 'all', time: '近 30 天' };
  const page = followPage([{ id: 1, name: 'x', newCount: 0, conditions: c }], { showModal: (m) => modals.push(m) });
  page.loadLive(); await flush(); await flush();
  page.remove(event({ id: 1, name: '“高新技术企业” · 不限地区 · 全部 · 近 30 天' }));
  assert.equal(modals[0].content, '不再关注“高新技术企业” · 不限地区 · 全部 · 近 30 天？');
  page.remove(event({ id: 1, name: '我的高企关注' }));
  assert.equal(modals[1].content, '不再关注“我的高企关注”？');
  for (const m of modals) assert.doesNotMatch(m.content, /"/);
  assert.equal(conditions.quoted('"旧写法" · 山东'), '"旧写法" · 山东');
});

test('关注条件说明：发布时间"不限"不写进说明，默认名称和说明一样时卡片只写一行（11/2 独立审阅）', () => {
  const { describe: desc, sameAsName } = require('../utils/conditions.js');
  const c = { q: '高新技术企业', regions: '不限地区', kind: 'all', time: '不限' };
  const text = desc(c);
  assert.ok(!/· 不限$/.test(text), text);
  assert.ok(sameAsName('“高新技术企业” · 不限地区 · 全部', text), text);
  assert.match(desc({ ...c, time: '近 30 天' }), /· 近 30 天$/);
});
