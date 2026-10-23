// 第 0 步第 1–4 处（10/22，AI 板块推进计划第四节第 1 部分）：AI 首页两项能力能点、去选一份通知；
// AI 只留一个家（批量工具页不放 AI）；"我的记录"是同一份，每条标类型、从哪进来先筛好哪一种、空的时候指回去；
// "工具中心"改叫"批量工具"，"我的"那一行写明是买。依赖都用替身，不读取 legal.js 或本机配置。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { render, textOf, findAll, hasClass, tapsTo } = require('./wxml-render.js');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const regions = require('../utils/regions.js');
const context = require('../utils/tool-context.js');
const advisor = require('../utils/advisor.js');
const interpret = require('../utils/ai-interpret.js');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 6; i += 1) await flush(); };
const plain = (value) => JSON.parse(JSON.stringify(value));
const tap = (dataset) => ({ currentTarget: { dataset } });

function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, Math, Promise, Set, Map, setTimeout, clearTimeout, console, Date,
    require(name) {
      if (name === '../../utils/regions.js') return regions;
      if (!Object.hasOwn(deps, name) && /daywatch\.js$/.test(name)) return require('./helpers/day-off.js');
      assert.ok(Object.hasOwn(deps, name), `${file} 缺少依赖 ${name}`);
      return deps[name];
    },
    Page(d) { def = d; }, Component(d) { def = d; },
    getApp: () => ({ globalData: { statusBarHeight: 20, navHeight: 44, menuWidth: 96 } }),
    ...globals,
  }, { filename: file });
  if (!def) return module.exports;
  const instance = { ...def, ...(def.methods || {}), data: plain(def.data || {}), setData(values) { Object.assign(this.data, plain(values)); } };
  if (def.properties) instance.properties = Object.fromEntries(Object.entries(def.properties).map(([key, value]) => [key, value.value]));
  return instance;
}

const DATA_ON = { enabled: true, features: ['compare', 'track'], remaining: { compare: 3, track: 3 } };
const DATA_OFF = { enabled: false, features: [], remaining: {} };
const AI_ON = (extra = {}) => ({ enabled: true, features: ['interpret', 'clause'], remaining: { interpret: 5, clause: 4 }, available: true, siteFull: false, disclosure: null, ...extra });
const AI_OFF = { enabled: false, features: [], remaining: {} };
const caps = ({ data = true, ai = true, aiExtra = {}, reason = '' } = {}) => ({ data_tools: data ? DATA_ON : DATA_OFF, ai_advisor: ai ? AI_ON(aiExtra) : AI_OFF, reason: ai ? '' : (reason || 'off') });

// 页面要的"AI 板块状态"替身：set 改状态并通知订阅的页面
function control(initial) {
  let state = Object.assign({}, initial);
  const listeners = new Set();
  return {
    listeners,
    current: () => Object.assign({}, state),
    subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
    ensure: () => Promise.resolve(Object.assign({}, state)),
    refresh: () => Promise.resolve(Object.assign({}, state)),
    recheck: () => Promise.resolve(Object.assign({}, state)),
    reconcile() {},
    canOpen: (value = state) => value.visible === true && value.confirmed === true && value.available !== false,
    clear() {}, rememberDoc() {}, lastDoc: () => null, forgetDoc() {},
    set(next) { state = Object.assign({}, state, next); listeners.forEach((fn) => fn(Object.assign({}, state))); },
  };
}
const OPEN = { visible: true, confirmed: true, available: true };
const CLOSED = { visible: false, confirmed: true, available: false };
const PAUSED = { visible: true, confirmed: true, available: false };

// ---- 第 1 处：AI 首页两项能力能点 ----

function hubPage({ state = OPEN, capsValue = caps() } = {}) {
  const access = control(state);
  const navigations = [];
  const page = load('pages/ai/index.js', {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(capsValue), search: () => Promise.resolve({ items: [] }), advisorTasks: () => Promise.resolve({ items: [] }), detail: () => Promise.resolve({}) },
    '../../utils/ai-access.js': access, '../../utils/advisor.js': advisor, '../../utils/ai-interpret.js': interpret, '../../utils/tool-context.js': context,
    '../../utils/consent.js': { ensureConsent: () => Promise.resolve(true) }, '../../utils/format.js': format,
  }, { wx: { switchTab() {}, navigateTo: (o) => navigations.push(o.url) } });
  page.getTabBar = () => ({ setData() {} });
  page.onLoad();
  return { page, access, navigations, root: () => render('pages/ai/index.wxml', page.data) };
}
const abilityRows = (root) => findAll(root, hasClass('ability'));
const chevronIn = (node) => findAll(node, (n) => n.tag === 't-icon' && n.attrs.name === 'chevron-right').length;

test('AI 首页：两项能力能用时整行能点，带一个 › ；点了去搜索页选一份能解读的通知，行上的按钮就是这一项', async () => {
  const h = hubPage();
  h.page.onShow(); await settle();
  const rows = abilityRows(h.root());
  assert.equal(rows.length, 2);
  for (const row of rows) {
    assert.equal(row.attrs.bindtap, 'pick'); assert.equal(row.attrs['aria-role'], 'button');
    assert.equal(chevronIn(row), 1, textOf(row));
  }
  assert.match(rows[0].attrs['aria-label'], /^AI 解读：选一份通知$/);
  assert.match(rows[1].attrs['aria-label'], /^条款对照：选一份通知$/);
  h.page.pick(tap({ kind: 'interpret' })); h.page.pick(tap({ kind: 'clause' }));
  assert.deepEqual(h.navigations, ['/pages/search/index?ai=1&for=interpret', '/pages/search/index?ai=1&for=clause']);
  // "去找一份通知"照旧一直在（Codex：本周列表空的时候也能换一份）
  assert.equal(findAll(h.root(), tapsTo('findDoc')).length, 1);
  // 不认的、没有这一项的：不动
  h.page.pick(tap({ kind: 'compare' })); h.page.pick(tap({}));
  assert.equal(h.navigations.length, 2);
});

test('AI 首页：两行什么时候点不了——这一项今天用完了、全站满了、后台处理不了、还在问后台；点不了时不带 › ，点了不动；恢复了马上能点', async () => {
  // 条款对照今天用完了：只有那一行点不了
  let h = hubPage({ capsValue: caps({ aiExtra: { remaining: { interpret: 2, clause: 0 } } }) });
  h.page.onShow(); await settle();
  let rows = abilityRows(h.root());
  assert.deepEqual(rows.map(chevronIn), [1, 0]);
  assert.equal(rows[1].attrs['aria-role'], 'text');
  h.page.pick(tap({ kind: 'clause' })); assert.deepEqual(h.navigations, []);
  h.page.pick(tap({ kind: 'interpret' })); assert.deepEqual(h.navigations, ['/pages/search/index?ai=1&for=interpret']);
  // 全站满了、后台处理不了：两行都点不了
  for (const aiExtra of [{ siteFull: true }, { available: false }]) {
    h = hubPage({ capsValue: caps({ aiExtra }) });
    h.page.onShow(); await settle();
    rows = abilityRows(h.root());
    assert.equal(rows.length, 2, JSON.stringify(aiExtra));
    assert.deepEqual(rows.map(chevronIn), [0, 0], JSON.stringify(aiExtra));
    h.page.pick(tap({ kind: 'interpret' })); h.page.pick(tap({ kind: 'clause' }));
    assert.deepEqual(h.navigations, [], JSON.stringify(aiExtra));
  }
  // 停在页面上后台说处理不了：两行马上收起 › 、点了不动；恢复了照能力接口的结果放出来
  h = hubPage();
  h.page.onShow(); await settle();
  h.access.set(PAUSED);
  assert.deepEqual(abilityRows(h.root()).map(chevronIn), [0, 0]);
  h.page.pick(tap({ kind: 'interpret' })); assert.deepEqual(h.navigations, []);
  h.access.set(OPEN);
  assert.deepEqual(abilityRows(h.root()).map(chevronIn), [1, 1]);
  h.page.pick(tap({ kind: 'clause' })); assert.deepEqual(h.navigations, ['/pages/search/index?ai=1&for=clause']);
  // 还没问到能力接口（加载中）、没同意协议（两行换成同意入口）：没有能点的行
  h = hubPage();
  h.page.onShow();
  h.page.pick(tap({ kind: 'interpret' })); assert.deepEqual(h.navigations, []);
  h = hubPage({ capsValue: caps({ ai: false, reason: 'consent' }) });
  h.page.onShow(); await settle();
  assert.equal(abilityRows(h.root()).length, 0);
  h.page.pick(tap({ kind: 'interpret' })); assert.deepEqual(h.navigations, []);
});

// ---- 第 1 处：搜索页"选一份通知" ----

function searchPage(state) {
  const calls = []; const titles = [];
  const access = control(state);
  const page = load('pages/search/index.js', {
    '../../utils/api.js': { search: (p) => { calls.push(p); return Promise.resolve({ total: 0, total_all: 0, items: [] }); } },
    '../../utils/format.js': format, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} }, '../../utils/exporter.js': {},
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/subscribe.js': { remindOpen: () => true }, '../../utils/conditions.js': conditions,
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({}), notCoveredNames: () => '', allUncovered: () => [] },
    '../../utils/ai-access.js': access,
  }, { wx: { setNavigationBarTitle: (o) => titles.push(o.title) } });
  return { page, calls, access, titles };
}

test('搜索页从两项能力来选通知（for=interpret / clause）：标题写"选一份通知"，"只看能……的"那一条按这一项写、下面写怎么选，行上的按钮按这一项；不进关注和转发', async () => {
  for (const [kind, only, hintText] of [['clause', '只看能做条款对照的', '点“条款对照”去填企业情况，点标题先看全文'], ['interpret', '只看能 AI 解读的', '点“AI 解读”直接开始，点标题先看全文']]) {
    const s = searchPage(OPEN);
    s.page.onLoad({ ai: '1', for: kind }); await settle();
    assert.equal(s.page.data.aiOnly, true); assert.equal(s.calls.at(-1).ai, 1);
    assert.equal(s.page.data.aiAction, kind); assert.equal(s.page.data.pickShown, kind);
    assert.deepEqual(s.titles, ['选一份通知'], kind);
    const root = render('pages/search/index.wxml', s.page.data);
    const bar = findAll(root, (n) => n.tag === 'ai-only' && n.attrs.variant === undefined);
    assert.equal(bar.length, 1); assert.equal(bar[0].attrs.pick, kind);
    // 组件自己写：那一条、怎么选；没有结果时的"去掉"按钮也按这一项写
    const view = load('components/ai-only/index.js');
    view.observers['pick, variant'].call(view, kind, 'bar');
    assert.equal(textOf(render('components/ai-only/index.wxml', { ...view.data, variant: 'bar' })).trim(), `${only}通知去掉 ${hintText}`);
    assert.equal(textOf(render('components/ai-only/index.wxml', { ...view.data, variant: 'exit' })).trim(), `去掉“${only}”`);
    assert.match(read('pages/search/index.wxml'), /<ai-only variant="exit" pick="\{\{pickShown\}\}"/);
    // 关注保存的条件、转发路径里都没有 for
    assert.equal(s.page.conditions().for, undefined);
    assert.doesNotMatch(s.page.onShareAppMessage().path, /for=|ai=/);
    // 列表行把这一项交给 AI 按钮
    assert.match(read('pages/search/index.wxml'), /<doc-row [^>]*ai-action="\{\{aiAction\}\}"/);
  }
  // 普通的"去找一份通知"（只带 ai=1）：标题不改、不写怎么选，按钮照旧是 AI 解读
  const plainSearch = searchPage(OPEN);
  plainSearch.page.onLoad({ ai: '1' }); await settle();
  assert.equal(plainSearch.page.data.aiAction, 'interpret'); assert.equal(plainSearch.page.data.pickShown, ''); assert.deepEqual(plainSearch.titles, []);
  // 不认的 for、不带 ai=1：都当没有
  for (const query of [{ ai: '1', for: 'compare' }, { for: 'clause' }]) {
    const s = searchPage(OPEN);
    s.page.onLoad(query); await settle();
    assert.equal(s.page.data.aiAction, 'interpret', JSON.stringify(query)); assert.equal(s.page.data.pickShown, ''); assert.deepEqual(s.titles, []);
  }
  // ai-only 组件：没有 pick 时照旧"只看能 AI 解读的通知"，不多一行
  const bare = load('components/ai-only/index.js');
  assert.equal(textOf(render('components/ai-only/index.wxml', { ...bare.data, variant: 'bar' })).trim(), '只看能 AI 解读的通知去掉');
  bare.observers['pick, variant'].call(bare, '', 'bar');
  assert.equal(findAll(render('components/ai-only/index.wxml', { ...bare.data, variant: 'bar' }), hasClass('ai-only-hint')).length, 0);
  assert.equal(textOf(render('components/ai-only/index.wxml', { ...bare.data, variant: 'exit' })).trim(), '去掉“只看能 AI 解读的”');
});

test('搜索页选通知：AI 对这个人还没确认开着时不改标题、不写怎么选；确认开着才写，后来关了（注销被擦掉）回到"搜索"', async () => {
  const s = searchPage({ visible: true, confirmed: false, available: true });
  s.page.onLoad({ ai: '1', for: 'clause' }); await settle();
  assert.equal(s.page.data.aiOnly, false); assert.equal(s.page.data.pickShown, ''); assert.deepEqual(s.titles, []);
  s.access.set(OPEN); await settle();
  assert.equal(s.page.data.aiOnly, true); assert.equal(s.page.data.pickShown, 'clause'); assert.deepEqual(s.titles, ['选一份通知']);
  s.access.set(CLOSED); await settle();
  assert.equal(s.page.data.aiOnly, false); assert.equal(s.page.data.pickShown, ''); assert.deepEqual(s.titles, ['选一份通知', '搜索']);
  // 去掉"只看能做条款对照的"（Grok 10/22）：只放宽列表，还在选通知——行上的按钮照旧是条款对照，标题照旧，
  // "怎么选"那一行单独留着；之后 AI 关了照样回到"搜索"
  const t = searchPage(OPEN);
  t.page.onLoad({ ai: '1', for: 'clause' }); await settle();
  t.page.removeAi(); await settle();
  assert.equal(t.page.data.aiOnly, false); assert.equal(t.calls.at(-1).ai, undefined, '列表放宽了');
  assert.equal(t.page.data.aiAction, 'clause'); assert.equal(t.page.data.pickShown, 'clause'); assert.deepEqual(t.titles, ['选一份通知']);
  const left = findAll(render('pages/search/index.wxml', t.page.data), (n) => n.tag === 'ai-only');
  assert.deepEqual(left.map((n) => [n.attrs.variant, n.attrs.pick]), [['hint', 'clause']]);
  // 列表放宽了，写明没有按钮的那份做不了（Grok 10/22）
  const hintView = load('components/ai-only/index.js');
  hintView.observers['pick, variant'].call(hintView, 'clause', 'hint');
  assert.equal(textOf(render('components/ai-only/index.wxml', { ...hintView.data, variant: 'hint' })).trim(), '有“条款对照”按钮的才能做对照，点它去填企业情况；点标题看全文');
  hintView.observers['pick, variant'].call(hintView, 'interpret', 'hint');
  assert.equal(hintView.data.hint, '有“AI 解读”按钮的才能解读，点它直接开始；点标题看全文');
  t.access.set(OPEN); await settle();
  assert.equal(t.page.data.aiOnly, false, '去掉了就不再加回来');
  t.access.set(CLOSED); await settle();
  assert.equal(t.page.data.pickShown, ''); assert.deepEqual(t.titles, ['选一份通知', '搜索']);
  assert.equal(findAll(render('pages/search/index.wxml', t.page.data), (n) => n.tag === 'ai-only').length, 0);
  // 普通进来的（只带 ai=1）去掉以后不再跟着变（原来的规矩）
  const u = searchPage(OPEN);
  u.page.onLoad({ ai: '1' }); await settle();
  u.page.removeAi(); await settle();
  assert.equal(u.access.listeners.size, 0);
});

// ---- 第 1 处：列表行的按钮按这一项写 ----

function aiButton(props, state = OPEN) {
  const access = control(state);
  const navigations = []; const events = [];
  const view = load('components/ai-button/index.js', { '../../utils/ai-access.js': access, '../../utils/tool-context.js': context }, { wx: { navigateTo: (o) => navigations.push(o) } });
  Object.assign(view.properties, props);
  view.triggerEvent = (name, detail) => events.push([name, plain(detail)]);
  view.lifetimes.attached.call(view);
  return { view, access, navigations, events, text: () => textOf(render('components/ai-button/index.wxml', { ...view.data, ...view.properties })).trim() };
}

test('列表行的按钮 action=clause：写"条款对照"、看这一行能不能做条款对照，点了直接进对照页；不认的 action 当 AI 解读', () => {
  const doc = { docId: '42', docTitle: '关于申报的通知', ai: { interpret: true, clause: true, reason: null } };
  const b = aiButton({ ...doc, action: 'clause' });
  assert.equal(b.text(), '条款对照'); assert.deepEqual(b.events, [['toggle', { shown: true }]]);
  b.view.open(); b.view.open();
  assert.equal(b.navigations.length, 1);
  assert.equal(b.navigations[0].url, `/pages/advisor-clause/index?docId=42&title=${encodeURIComponent('关于申报的通知')}`);
  const shownLabel = findAll(render('components/ai-button/index.wxml', { ...b.view.data, ...b.view.properties }), hasClass('ai-chip'))[0];
  assert.equal(shownLabel.attrs['aria-label'], '条款对照：关于申报的通知');
  // 这一行只能解读、不能对照（后台两样一般一起给）：对照的按钮不出现
  assert.equal(aiButton({ ...doc, ai: { interpret: true, clause: false }, action: 'clause' }).text(), '');
  // 不认的 action：当 AI 解读
  const odd = aiButton({ ...doc, action: 'toString' });
  assert.equal(odd.text(), 'AI 解读'); odd.view.open();
  assert.match(odd.navigations[0].url, /^\/pages\/advisor-interpret\/index\?docId=42/);
  // 显示着的时候换了 action：名字跟着换，不再发一次 toggle
  const swap = aiButton(doc);
  assert.equal(swap.text(), 'AI 解读');
  swap.view.properties.action = 'clause'; swap.view.observers['ai, selecting, docId, action'].call(swap.view);
  assert.equal(swap.text(), '条款对照'); assert.equal(swap.events.length, 1);
  // AI 没对这个人开着：两种都不出现
  assert.equal(aiButton({ ...doc, action: 'clause' }, CLOSED).text(), '');
});

// ---- 第 3 处：我的记录 ----

const TASKS = [
  { id: 1, kind: 'interpret', status: 'done', docTitle: '甲通知', hasResult: true, createdAt: '2026-10-21T02:00:00Z' },
  { id: 2, kind: 'compare', status: 'done', rowCount: 12, resultRows: 3, hasFile: true, createdAt: '2026-10-20T02:00:00Z', expiresAt: '2099-11-20T02:00:00Z' },
  { id: 3, kind: 'clause', status: 'failed', docTitle: '乙通知', createdAt: '2026-10-19T02:00:00Z' },
  { id: 4, kind: 'track', status: 'queued', rowCount: 5, createdAt: '2026-10-18T02:00:00Z' },
];

function recordsPage({ capsValue = caps(), items = TASKS, query = {}, pages = null } = {}) {
  const navigations = []; const switched = []; const backs = [];
  const timers = [];
  const page = load('pages/advisor-tasks/index.js', {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(capsValue), advisorTasks: () => Promise.resolve({ items: typeof items === 'function' ? items() : items }) },
    '../../utils/advisor.js': advisor, '../../utils/ai-access.js': control(OPEN),
  }, {
    wx: { setNavigationBarTitle() {}, navigateTo: (o) => navigations.push(o.url), switchTab: (o) => switched.push(o.url), navigateBack: () => backs.push('back') },
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
    ...(pages ? { getCurrentPages: () => pages } : {}),
  });
  page.onLoad(query);
  return { page, navigations, switched, backs, timers, root: () => render('pages/advisor-tasks/index.wxml', page.data), text: () => textOf(render('pages/advisor-tasks/index.wxml', page.data)) };
}
const cards = (root) => findAll(root, (n) => hasClass('advisor-card')(n) && n.attrs.bindtap === 'openTask');
const cardTitles = (root) => cards(root).map((n) => textOf(findAll(n, hasClass('advisor-title'))[0]).trim());
const segItems = (root) => findAll(root, hasClass('seg-item')).map((n) => ({ text: textOf(n).trim(), on: hasClass('seg-item--on')(n) }));

test('我的记录：AI 和批量工具都能用时每条标类型，上面一排"全部 / AI / 批量工具"写各有几条；从哪进来先筛好哪一种，可以切回全部', async () => {
  // 从"我的 → 我的记录"进来（不带 type）：全部
  let h = recordsPage();
  h.page.onShow(); await settle();
  assert.deepEqual(segItems(h.root()), [{ text: '全部 4', on: true }, { text: 'AI 2', on: false }, { text: '批量工具 2', on: false }]);
  // 先是哪一项，类型标签在标题行右边（放在前面会读成"AI AI 解读"，Grok 10/22）
  assert.deepEqual(cardTitles(h.root()), ['AI 解读AI', '批量名单比对批量工具', '条款对照AI', '客户相关通知表批量工具']);
  assert.match(h.text(), /AI 生成：/, '有 AI 记录的视图顶部照样标 AI 生成');
  // 从 AI 首页、解读页、对照页进来（type=ai）：先筛好 AI 的
  h = recordsPage({ query: { type: 'ai' } });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.current, 'ai');
  assert.deepEqual(segItems(h.root()).map((s) => s.on), [false, true, false]);
  assert.deepEqual(cardTitles(h.root()), ['AI 解读AI', '条款对照AI']);
  // 切回全部
  h.page.pick(tap({ key: 'all' }));
  assert.equal(cards(h.root()).length, 4);
  // 从批量工具页、提交名单页进来（type=tools）：先筛好批量工具的；顶部不放 AI 生成那一条
  h = recordsPage({ query: { type: 'tools' } });
  h.page.onShow(); await settle();
  assert.deepEqual(cardTitles(h.root()), ['批量名单比对批量工具', '客户相关通知表批量工具']);
  assert.doesNotMatch(h.text(), /AI 生成/);
  assert.match(h.text(), /提交 12 家 · 结果 3 行/);
  // 不认的 type、不认的筛选：当全部 / 不动
  h = recordsPage({ query: { type: 'compare' } });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.current, 'all');
  h.page.pick(tap({ key: 'toString' })); assert.equal(h.page.data.current, 'all');
  // 处理中的过一会儿再问：筛选不变，条数跟着变
  let list = TASKS;
  h = recordsPage({ query: { type: 'tools' }, items: () => list });
  h.page.onShow(); await settle();
  assert.equal(h.timers.length, 1, '有排队中的，过一会儿再问');
  list = TASKS.filter((task) => task.id !== 4);
  h.timers[0](); await settle();
  assert.equal(h.page.data.current, 'tools'); assert.deepEqual(plain(h.page.data.counts), { all: 3, ai: 2, tools: 1 });
});

test('我的记录：从批量工具页点了某一条、提交名单后跳过来（带 taskId）时把那一条标出来；别的不标', async () => {
  const h = recordsPage({ query: { type: 'tools', taskId: '4' } });
  h.page.onShow(); await settle();
  const focused = cards(h.root()).filter(hasClass('record--focus'));
  assert.equal(focused.length, 1); assert.equal(focused[0].attrs['data-id'], 4);
  assert.match(read('pages/advisor-tasks/index.wxss'), /\.record--focus \{ box-shadow: 0 0 0 4rpx var\(--brand\); \}/);
  const plainOpen = recordsPage({ query: { type: 'tools' } });
  plainOpen.page.onShow(); await settle();
  assert.equal(cards(plainOpen.root()).filter(hasClass('record--focus')).length, 0);
});

test('我的记录：只有一种能用时不放筛选、不标类型，照旧列全部——AI 关着时页面上没有 AI 字样（带了 type=ai 也一样）', async () => {
  let h = recordsPage({ capsValue: caps({ ai: false }), query: { type: 'ai' } });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.mixed, false); assert.equal(h.page.data.current, 'all');
  assert.equal(segItems(h.root()).length, 0);
  assert.deepEqual(cardTitles(h.root()), ['批量名单比对', '客户相关通知表']);
  assert.doesNotMatch(h.text(), /\bAI\b/);
  // 批量工具关着、AI 开着（带了 type=tools）：列全部 AI 的，不标类型
  h = recordsPage({ capsValue: caps({ data: false }), query: { type: 'tools' } });
  h.page.onShow(); await settle();
  assert.equal(h.page.data.current, 'all'); assert.equal(segItems(h.root()).length, 0);
  assert.deepEqual(cardTitles(h.root()), ['AI 解读', '条款对照']);
  h.page.pick(tap({ key: 'tools' })); assert.equal(h.page.data.current, 'all', '没有筛选时点了不动');
});

test('我的记录：空的时候按入口写——AI 的指回 AI 首页，批量工具的指回批量工具页（从那一页来的就返回）；全部空的两样都给', async () => {
  // AI 的一条都没有（批量工具有）：筛选那一排照样在，能切去看批量工具的
  const toolsOnly = TASKS.filter((task) => ['compare', 'track'].includes(task.kind));
  let h = recordsPage({ items: toolsOnly, query: { type: 'ai' } });
  h.page.onShow(); await settle();
  // 用户看到的名字是底栏和页面标题上的"AI"，不写"AI 首页"（agy 10/22）
  assert.match(h.text(), /还没有 AI 记录 在“AI”里选一份通知，做 AI 解读或条款对照，结果会保存在这里。 去 AI/);
  assert.deepEqual(segItems(h.root()).map((s) => s.text), ['全部 2', 'AI 0', '批量工具 2']);
  assert.equal(cards(h.root()).length, 0);
  findAll(h.root(), tapsTo('aiHome')).forEach(() => h.page.aiHome());
  assert.deepEqual(h.switched, ['/pages/ai/index']);
  // 批量工具的一条都没有：从批量工具页进来的，点"去批量工具"返回那一页；别处进来的打开它
  const aiOnly = TASKS.filter((task) => ['interpret', 'clause'].includes(task.kind));
  h = recordsPage({ items: aiOnly, query: { type: 'tools' }, pages: [{ route: 'pages/me/index' }, { route: 'pages/advisor/index' }, { route: 'pages/advisor-tasks/index' }] });
  h.page.onShow(); await settle();
  assert.match(h.text(), /还没有批量工具的记录 在批量工具里做批量名单比对或客户相关通知表，结果会保存在这里。 去批量工具/);
  assert.doesNotMatch(h.text(), /AI 生成/);
  h.page.tools(); assert.deepEqual(h.backs, ['back']); assert.deepEqual(h.navigations, []);
  h = recordsPage({ items: aiOnly, query: { type: 'tools' }, pages: [{ route: 'pages/me/index' }, { route: 'pages/advisor-tasks/index' }] });
  h.page.onShow(); await settle();
  h.page.tools(); assert.deepEqual(h.navigations, ['/pages/advisor/index']); assert.deepEqual(h.backs, []);
  // 全部空的：两样都能用时两个都给；只有一种时只给那一种（AI 关着时不提 AI）
  h = recordsPage({ items: [] });
  h.page.onShow(); await settle();
  assert.match(h.text(), /还没有记录 AI 解读、条款对照和批量工具的结果，都会保存在这里。 去 AI去批量工具/);
  h = recordsPage({ items: [], capsValue: caps({ data: false }) });
  h.page.onShow(); await settle();
  assert.match(h.text(), /还没有记录 在“AI”里选一份通知/); assert.equal(findAll(h.root(), tapsTo('tools')).length, 0);
  assert.doesNotMatch(read('pages/advisor-tasks/index.wxml'), /AI 首页选|去 AI 首页/);
  h = recordsPage({ items: [], capsValue: caps({ ai: false }) });
  h.page.onShow(); await settle();
  assert.match(h.text(), /还没有记录 在批量工具里做批量名单比对或客户相关通知表，结果会保存在这里。 去批量工具/);
  assert.doesNotMatch(h.text(), /\bAI\b/); assert.equal(findAll(h.root(), tapsTo('aiHome')).length, 0);
  h.page.aiHome(); assert.deepEqual(h.switched, [], 'AI 没开着时点了不动');
});

// ---- 第 2 处：批量工具页只列批量工具的记录 ----

function toolsPage({ capsValue = caps(), items = TASKS, tasks = null } = {}) {
  const navigations = [];
  const page = load('pages/advisor/index.js', {
    '../../utils/api.js': {
      advisorCapabilities: () => Promise.resolve(capsValue), advisorTasks: tasks || (() => Promise.resolve({ items })),
      advisorStatus: () => Promise.resolve({ enabled: true, quota: { compare: { limit: 3, used: 1, maxRows: 50 }, track: { limit: 3, used: 0, maxRows: 50 } } }),
    },
    '../../utils/advisor.js': advisor, '../../utils/boost.js': require('../utils/boost.js'), '../../utils/ai-access.js': control(OPEN),
  }, { wx: { setNavigationBarTitle() {}, navigateTo: (o) => navigations.push(o.url) } });
  return { page, navigations, root: () => render('pages/advisor/index.wxml', page.data), text: () => textOf(render('pages/advisor/index.wxml', page.data)) };
}

test('批量工具页：AI 开着也只放批量工具；"我的记录"只列批量工具的（最近 3 条、写提交了几家），点了去我的记录、先筛好批量工具的', async () => {
  const many = TASKS.concat([
    { id: 5, kind: 'compare', status: 'done', rowCount: 7, hasFile: true, createdAt: '2026-10-21T05:00:00Z' },
    { id: 6, kind: 'track', status: 'failed', rowCount: 2, createdAt: '2026-10-17T02:00:00Z' },
  ]);
  const h = toolsPage({ items: many });
  h.page.onShow(); await settle();
  assert.deepEqual(plain(h.page.data.recent.map((task) => task.id)), [5, 2, 4]);
  const text = h.text();
  assert.doesNotMatch(text, /AI|条款对照|今天还能用|说明：适用人群|模型：/);
  // 标题写明只列批量工具的，和 AI 首页的"我的 AI 记录"对称（Grok、agy 10/22）
  assert.match(text, /我的批量工具记录 批量名单比对 · 已完成\s?2026-10-21 13:00 · 提交 7 家/);
  // 顾问靠卡片上那句选工具：通知表写明近 90 天、已确认截止的不列、截止没核对的照列（和上传页、backend track.py 同一口径）
  assert.match(text, /客户相关通知表：按客户的省份和填了的条件，列出近 90 天发布的申报通知（已确认截止的不列，截止没核对的照列，以原文为准），不判断能不能报，可下载 Excel。/);
  // D-087（#341）：两个工具按钮、在售时的"了解批量工具"都用白底次按钮
  const wxml = read('pages/advisor/index.wxml');
  assert.match(wxml, /<button class="advisor-button" data-kind="compare" bindtap="dataTool">批量名单比对<\/button>/);
  assert.match(wxml, /<button class="advisor-button" data-kind="track" bindtap="dataTool">制作客户相关通知表<\/button>/);
  assert.match(wxml, /<button wx:if="\{\{payCard.button\}\}" class="advisor-button" bindtap="buyPass">/);
  // 先是批量工具，再是记录；记录一行占满卡片宽（微信 style v2 的 button 不认 class 里的宽度，要 !important）
  assert.ok(text.indexOf('批量处理客户名单') < text.indexOf('我的批量工具记录'));
  assert.match(read('pages/advisor/index.wxss'), /\.recent-task \{ width: 100% !important; margin: 20rpx 0 0 !important;/);
  assert.equal(findAll(h.root(), hasClass('recent-task')).length, 3);
  // 每一行带 ›，看得出点了去别处（Grok 10/22）
  assert.equal(findAll(h.root(), (n) => hasClass('recent-task')(n) && findAll(n, (c) => c.tag === 't-icon' && c.attrs.name === 'chevron-right').length === 1).length, 3);
  // 点某一条：带上编号，那一页把它标出来；点"查看全部"不带
  h.page.tasks(tap({ id: 5 }));
  assert.equal(findAll(h.root(), (n) => n.tag === 'button' && textOf(n).trim() === '查看全部').length, 1);
  h.page.tasks();
  h.page.tasks(tap({ id: 999 }));
  assert.deepEqual(h.navigations, ['/pages/advisor-tasks/index?type=tools&taskId=5', '/pages/advisor-tasks/index?type=tools', '/pages/advisor-tasks/index?type=tools']);
  // 一条批量工具的记录都没有：写清楚，不放"查看全部"（点进去也是空的）
  const none = toolsPage({ items: TASKS.filter((task) => ['interpret', 'clause'].includes(task.kind)) });
  none.page.onShow(); await settle();
  assert.match(none.text(), /我的批量工具记录 还没有记录，提交名单后结果会保存在这里。/);
  assert.equal(findAll(none.root(), (n) => n.tag === 'button' && textOf(n).trim() === '查看全部').length, 0);
  // 记录没加载出来：给重试，"查看全部"照样能点
  const broken = toolsPage({ tasks: () => Promise.reject(new Error('网络不太好')) });
  broken.page.onShow(); await settle();
  assert.match(broken.text(), /网络不太好/);
  assert.equal(findAll(broken.root(), tapsTo('retryRecent')).length, 1);
  assert.equal(findAll(broken.root(), (n) => n.tag === 'button' && textOf(n).trim() === '查看全部').length, 1);
});

// ---- 入口带上筛选 ----

test('"我的记录"的入口按从哪进来带筛选：AI 首页、解读页、对照页带 ai，批量工具页、提交名单页带 tools，"我的""我的数据"不带（全部）', async () => {
  assert.equal(advisor.recordsUrl('ai'), '/pages/advisor-tasks/index?type=ai');
  assert.equal(advisor.recordsUrl('tools'), '/pages/advisor-tasks/index?type=tools');
  for (const odd of [undefined, '', 'all', 'toString']) assert.equal(advisor.recordsUrl(odd), '/pages/advisor-tasks/index');
  assert.deepEqual(['interpret', 'clause', 'compare', 'track', 'other'].map(advisor.recordType), ['ai', 'ai', 'tools', 'tools', '']);
  // 解读页、对照页"打开我的记录"
  for (const page of ['advisor-interpret', 'advisor-clause']) {
    const urls = [];
    const p = load(`pages/${page}/index.js`, {
      '../../utils/api.js': {}, '../../utils/ai-interpret.js': interpret, '../../utils/ai-clause.js': require('../utils/ai-clause.js'), '../../utils/format.js': format,
      '../../utils/advisor.js': advisor, '../../utils/tool-context.js': context, '../../utils/ai-access.js': control(OPEN),
    }, { wx: { navigateTo: (o) => urls.push(o.url) } });
    p.data.aiEnabled = true; p.tasks();
    assert.deepEqual(urls, ['/pages/advisor-tasks/index?type=ai'], page);
  }
  // 提交名单页"查看已有任务"
  assert.match(read('pages/advisor-form/index.js'), /tasks\(\) \{ wx\.redirectTo\(\{ url: recordsUrl\('tools'\) \}\); \}/);
  // "我的"、"我的数据"：全部
  assert.match(read('pages/me/index.wxml'), /data-url="\/pages\/advisor-tasks\/index" bindtap="go"><t-icon name="file"[^>]*\/><text class="row-text">我的记录<\/text>/);
  assert.match(read('pages/my-data/index.js'), /tasks\(\) \{ wx\.navigateTo\(\{ url: '\/pages\/advisor-tasks\/index' \}\); \}/);
});

// ---- 第 4 处：改名 ----

test('"工具中心"改叫"批量工具"：首页入口、三步上手、申报季那一条、"我的"九宫格、我的客户都是这个名字；"我的"去购买页那一行写明是买', () => {
  const home = read('pages/home/index.wxml');
  assert.match(home, /<text class="entry-name">批量工具<\/text><text class="entry-sub">一次处理多家<\/text>/);
  assert.match(home, /aria-label="批量处理客户名单（机构用）：\{\{seasonText\}\}。去批量工具"/);
  assert.match(read('pages/home/index.js'), /text: '在“批量工具”一次粘贴多家企业/);
  assert.match(read('pages/me/index.wxml'), /data-url="\/pages\/advisor\/index" bindtap="go">.*<text>批量工具<\/text>/);
  assert.match(read('pages/me/index.wxml'), /data-url="\/pages\/pass\/index" bindtap="go">.*<text class="row-text">\{\{passLabel\}\}<\/text>/);
  const clients = read('pages/clients/index.wxml');
  assert.match(clients, /批量比对名单或生成客户相关通知表，请用批量工具（要另外确认上传）/); assert.match(clients, /打开批量工具/); assert.match(clients, /用批量工具里的“客户相关通知表”/);
});
