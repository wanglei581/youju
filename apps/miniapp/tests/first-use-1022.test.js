const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const { render, textOf, findAll, hasClass, tapsTo } = require('./wxml-render.js');

// Grok 10/22 以"第一次用的顾问"走查首发核心页后的修正：只改字和顺序，不加功能（D-061）。依赖都用替身。
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (v) => JSON.parse(JSON.stringify(v));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const flat = (node) => textOf(node).replace(/\s+/g, '');
const daywatch = { watchDay() {}, stopDay() {} };
// 北京时间今天往后 n 天
const plus = (n) => new Date(Date.now() + 8 * 3600e3 + n * 86400e3).toISOString().slice(0, 10);
// 北京时间 2026-10-22 上午 10 点
const NOW = new Date('2026-10-22T02:00:00Z');

function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, setTimeout, clearTimeout,
    require(name) { if (!Object.hasOwn(deps, name) && /ai-access\.js$/.test(name)) return AI_OFF; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  return def ? { ...def, data: plain(def.data), setData(values) { Object.assign(this.data, values); } } : module.exports;
}

// 一个节点里能看到的元素子节点（去掉空白文字）
const elements = (node) => node.children.filter((c) => c.tag);

test('第 7 条：当天截止写"今天截止"，不写"剩0天"；其余照旧，当天到点了算已截止', () => {
  assert.equal(format.dueInfo({ verified: true, date: '2026-10-22' }, NOW).text, '今天截止');
  assert.equal(format.dueInfo({ verified: true, date: '2026-10-22', time: '17:00' }, NOW).text, '今天 17:00截止');
  assert.equal(format.dueView({ kind: '企业申报截止', source: 'verified', date: '2026-10-22', time: '17:00' }, NOW).text, '今天 17:00截止');
  assert.equal(format.dueInfo({ verified: true, date: '2026-10-25' }, NOW).text, '10月25日截止 · 剩3天');
  assert.equal(format.dueInfo({ verified: true, date: '2026-10-21' }, NOW).text, '10月21日已截止');
  assert.equal(format.dueInfo({ verified: true, date: '2026-10-22', time: '09:00' }, NOW).text, '10月22日 09:00已截止');
  // 机器摘录只在 8–30 天写日期，不会出现"今天截止"
  assert.equal(format.dueView({ source: 'excerpt', date: '2026-10-22', quote: '请于2026年10月22日前报送。' }, NOW).source, 'unverified');
});

test('第 7 条：列表行（搜索、首页最新通知、关注的新文件）当天截止也写"今天截止"', () => {
  const row = load('components/doc-row/index.js', { '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/repost.js': require('../utils/repost.js') });
  row.observers['doc, q, today'].call(row, { id: '9', title: '关于组织申报的通知', kind: '申报通知', due: { kind: '企业申报截止', source: 'verified', date: plus(0) } }, '');
  assert.equal(row.data.due.text, '今天截止');
  const root = render('components/doc-row/index.wxml', { ...plain(row.data), doc: { kind: '申报通知', title: 'x' } });
  assert.equal(flat(findAll(root, hasClass('due'))[0]), '今天截止');
});

function detailPage(getDoc) {
  return load('pages/detail/index.js', {
    '../../utils/consent.js': { loginOpen: () => false },
    '../../utils/api.js': { detail: () => Promise.resolve(plain(getDoc())) },
    '../../utils/format.js': format, '../../utils/copy.js': {}, '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/repost.js': require('../utils/repost.js'),
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': daywatch,
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { wx: {} });
}
const DOC = { id: '5', kind: '申报通知', title: '关于组织申报的通知', issuer: '某省科学技术厅', date: '2026-10-08', deadline: null, deadline_state: 'unverified', tags: [] };

test('第 7 条：详情页逐条截止和旧接口的截止卡片，当天截止都写"今天截止"，和首页、截止页一致', async () => {
  const page = detailPage(() => ({ ...DOC, deadlines: [{ kind: '企业申报截止', source: 'verified', date: plus(0), time: null, remainingDays: 0, quote: null }] }));
  page.onLoad({ id: '5' }); await flush();
  let root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.equal(flat(findAll(root, hasClass('left-pill'))[0]), '今天截止');
  assert.doesNotMatch(textOf(root), /剩\s*0\s*天/);
  // 旧接口：只有核对过的 deadline
  const old = detailPage(() => ({ ...DOC, deadline: { label: '申报截止', date: plus(0), time: null, verified: true, notes: [] }, deadline_state: 'verified' }));
  old.onLoad({ id: '5' }); await flush();
  root = render('pages/detail/index.wxml', { ...old.data, loading: false });
  assert.equal(flat(findAll(root, hasClass('left-pill'))[0]), '今天截止');
  // 首页、截止页本来就是这样写的
  assert.match(read('pages/home/index.wxml'), /item\.days === 0 \? '今天截止'/);
  assert.match(read('pages/report/index.wxml'), /item\.days === 0 \? '今天截止'/);
});

test('第 8 条：显示提取的正文时，标题里写明"自动提取，未经人工逐字核对"；没提取到正文时只写去哪看原文，不出现提取的说明', async () => {
  let body = { status: 'ok', text: '一、申报对象……', total: 7, truncated: false };
  const page = detailPage(() => ({ ...DOC, kind: '政策', body }));
  page.onLoad({ id: '5' }); await flush();
  let root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  const card = (r) => findAll(r, (n) => elements(n).some((c) => hasClass('section-title')(c) && /^正文/.test(flat(c))))[0];
  assert.equal(flat(elements(card(root))[0]), '正文（自动提取，未经人工逐字核对）');
  // 政策文件上面没有截止卡片：说明里不指向"上面的截止时间"
  assert.equal(flat(findAll(card(root), hasClass('body-note'))[0]), '从原网页或PDF提取，一切以原文为准。');
  assert.doesNotMatch(textOf(card(root)), /截止/);
  // 申报通知上面的"截止时间"卡片里写了日期，才写"截止日期可先看上面的'截止时间'"（#339 的措辞）；
  // 卡片里只有"截止未核对"时不写——上面没有日期可看
  let rows = [{ kind: '企业申报截止', source: 'unverified', date: null, time: null, remainingDays: null, quote: null }];
  const notice = detailPage(() => ({ ...DOC, deadlines: rows, body: { status: 'ok', text: '正文', total: 2, truncated: false } }));
  notice.onLoad({ id: '5' }); await flush();
  let withDue = render('pages/detail/index.wxml', { ...notice.data, loading: false });
  assert.equal(findAll(withDue, hasClass('dls-head')).length, 1);
  assert.equal(flat(findAll(card(withDue), hasClass('body-note'))[0]), '从原网页或PDF提取，一切以原文为准。');
  rows = [{ kind: '企业申报截止', source: 'verified', date: plus(9), time: null, remainingDays: 9, quote: null }];
  notice.refreshing = false; notice.load(); await flush();
  withDue = render('pages/detail/index.wxml', { ...notice.data, loading: false });
  assert.equal(flat(findAll(card(withDue), hasClass('body-note'))[0]), '从原网页或PDF提取；截止日期可先看上面的“截止时间”，一切以原文为准。');
  // 旧接口（没有逐条截止，只有一条核对过的截止）：上面的卡片写了日期，同样写这半句
  const legacy = detailPage(() => ({ ...DOC, deadline: { label: '申报截止', date: plus(9), time: null, verified: true, notes: [] }, deadline_state: 'verified', body: { status: 'ok', text: '正文', total: 2, truncated: false } }));
  legacy.onLoad({ id: '5' }); await flush();
  const old = render('pages/detail/index.wxml', { ...legacy.data, loading: false });
  assert.equal(findAll(old, hasClass('dls-head')).length, 0);
  assert.equal(flat(findAll(card(old), hasClass('body-note'))[0]), '从原网页或PDF提取；截止日期可先看上面的“截止时间”，一切以原文为准。');
  for (const status of ['list', 'original', 'failed']) {
    body = { status, total: 0 };
    page.refreshing = false; page.load(); await flush();
    root = render('pages/detail/index.wxml', { ...page.data, loading: false });
    assert.equal(flat(elements(card(root))[0]), '正文');
    assert.equal(findAll(card(root), hasClass('body-note')).length, 0);
    assert.match(flat(card(root)), /复制原文链接/);
  }
});

const FORM = {
  loading: false, error: '', accepted: false, enabled: true, kind: 'compare', title: '批量名单比对', statusLine: '每月免费 3 次，本月剩余 3 次；每次最多 50 家',
  remaining: 3, maxRows: 50, mode: 'paste', text: '', rowCount: 0, previewed: false, previewing: false, submitting: false, agreed: true, passActive: false,
};

test('第 2 条：没预览时"提交任务"是灰的，紧挨着按钮上方写明先点"预览并核对行数"；预览通过后这句不出现', () => {
  const submitCard = (data) => {
    const root = render('pages/advisor-form/index.wxml', data);
    return findAll(root, (n) => elements(n).some(tapsTo('submit')))[0];
  };
  let card = submitCard(FORM);
  let kids = elements(card);
  let at = kids.findIndex(tapsTo('submit'));
  assert.equal(kids[at].attrs.disabled, true);
  assert.equal(flat(kids[at - 1]), '填好名单后，先点上面的“预览并核对行数”，通过后才能提交');
  // 提示里说的按钮确实在上面
  assert.equal(findAll(render('pages/advisor-form/index.wxml', FORM), (n) => n.attrs.bindtap === 'preview' && /预览并核对行数/.test(flat(n))).length, 1);
  // 正在预览（按钮在转圈）时不催"先点预览"
  assert.doesNotMatch(textOf(submitCard({ ...FORM, previewing: true })), /先点上面的/);
  card = submitCard({ ...FORM, previewed: true, rowCount: 3 });
  kids = elements(card);
  at = kids.findIndex(tapsTo('submit'));
  assert.equal(kids[at].attrs.disabled, false);
  assert.doesNotMatch(textOf(card), /先点上面的/);
});

test('第 4 条：关注页写的按钮名和搜索页按钮上的字一样（"关注"），不再写"关注这个搜索"', () => {
  const gated = textOf(render('pages/follow/index.wxml', { loginOpen: true, mode: 'gated', loading: false, follows: [], changes: [] }));
  assert.match(gated, /在搜索结果页点"关注"，把当前的关键词和筛选条件（地区、类型等）存下来；有新文件时在这里标出。/);
  const empty = textOf(render('pages/follow/index.wxml', { loginOpen: true, mode: 'live', loading: false, error: '', follows: [], changes: [] }));
  assert.match(empty, /还没有关注。在搜索结果页点"关注"，就能把当前的关键词和筛选条件存下来。/);
  // 底部入口不再写"地区 × 主题 × 类型"（像三项都得选），也不点名哪几项
  // 读屏标签和看得见的字对得上：按钮写"关注"，读屏读"关注当前搜索条件"
  assert.match(read('pages/search/index.wxml'), /class="follow-mini" bindtap="follow" aria-role="button" aria-label="关注当前搜索条件">/);
  assert.match(empty, /去搜索，设好条件后点"关注"/); assert.doesNotMatch(empty, /×/);
  for (const text of [gated, empty]) assert.doesNotMatch(text, /关注这个搜索/);
  // 搜索页按钮上就是"关注"
  assert.match(read('pages/search/index.wxml'), /class="follow-mini" bindtap="follow"[^>]*><t-icon name="notification"[^>]*\/>关注<\/view>/);
});

const CITY = { key: 'city', title: '城市区县', hint: '省市区联动 · 可多选', multi: true, options: [], note: '省市区选项由微信提供，选项齐全不代表已收录数据；选择具体城市会替换同省的全省条件，选择具体区县会替换所属城市和全省的条件。' };
const sheet = (extra = {}) => render('pages/search/index.wxml', {
  filterOpen: true, activeGroup: 'city', groups: [CITY], districtOptions: [], cityCoverageNote: '当前仅有 3 个城市的部分来源，不代表全市数据完整。', pickedMap: {}, counts: {}, entries: {}, activeChips: [], picked: { open: [], city: [], district: [] }, ...extra,
});
const notes = (root) => findAll(root, hasClass('group-note')).map(flat);

test('第 5 条：城市区县的规则拆成短句，放在选择器上面（先看规则再选）；"点一下移除"只在有已选的城市、区县时出现', () => {
  let root = sheet();
  const pane = findAll(root, hasClass('pane-inner'))[0];
  const kids = elements(pane);
  const picker = kids.findIndex((n) => n.tag === 'picker');
  assert.equal(flat(kids[0]), '城市区县省市区联动·可多选');
  assert.deepEqual(kids.slice(1, picker).map(flat), [
    '城市一栏选“全部”：查全省各级来源（省、市、区县都在内），和在“地区”里选这个省一样。',
    '区县一栏选“全部”：只查这个市已收录的市级来源，不含区县；要查区县，请选具体区县。',
    '直辖市例外：区县一栏选“全部”时查整个直辖市（各级来源都在内）。',
  ]);
  assert.match(flat(kids[picker + 1]), /^当前仅有3个城市的部分来源/);
  assert.ok(notes(root).every((n) => !/点一下移除/.test(n)));
  // 原来挤在一段里的那句不在了
  assert.doesNotMatch(read('pages/search/index.wxml'), /已选城市、区县点一下移除。城市一栏选/);
  // 有已选的城市或区县时，在它们下面写"点一下移除"
  root = sheet({ groups: [{ ...CITY, options: ['山东/青岛'] }], pickedMap: { 'city:山东/青岛': true }, picked: { open: [], city: ['山东/青岛'], district: [] } });
  assert.ok(notes(root).includes('已选的城市、区县，点一下移除。'));
  root = sheet({ districtOptions: [{ key: '山东/青岛/市南区', label: '山东·青岛·市南区' }], picked: { open: [], city: [], district: ['山东/青岛/市南区'] } });
  assert.ok(notes(root).includes('已选的城市、区县，点一下移除。'));
});

test('第 5 条：直接在"地区"里选省份，和在城市一栏选"全部"一样按全省；直辖市区县一栏选"全部"也是按整个直辖市，请求里不带城市（规则里写的都走得通）', async () => {
  const conditions = require('../utils/conditions.js');
  const regions = require('../utils/regions.js');
  // 城市一栏选"全部"得到的条件：只有省份
  const choice = regions.regionSelection(['山东省', '全部', '全部'], { cities: [], districts: [] });
  assert.deepEqual(plain(choice), { province: '山东', city: '', district: '', note: '' });
  const viaPicker = regions.applyAreaChoice({ regions: ['不限地区'], cities: [], districts: [] }, choice);
  assert.deepEqual(plain(viaPicker), { regions: ['山东'], cities: [], districts: [] });
  // 在"地区"里点山东得到的地区条件也是 ['山东']，请求一样
  assert.deepEqual(plain(conditions.requestScope(['山东'], [], [], [])), plain(conditions.requestScope(viaPicker.regions, [], viaPicker.cities, viaPicker.districts)));
  // 直辖市：区县一栏选"全部"得到的也只是省份（整个直辖市），不是只查市级来源
  const bj = regions.regionSelection(['北京市', '北京市', '全部'], { cities: [], districts: [] });
  assert.equal(bj.province, '北京'); assert.equal(bj.city, ''); assert.equal(bj.district, '');
  const viaBj = regions.applyAreaChoice({ regions: ['不限地区'], cities: [], districts: [] }, bj);
  assert.deepEqual(plain(viaBj), { regions: ['北京'], cities: [], districts: [] });
});

const results = (extra) => render('pages/search/index.wxml', { loading: false, error: '', total: 3, totalAll: 9, chipTime: '近 30 天', items: [], activeChips: [], picked: { open: [] }, fold: { text: '' }, entries: {}, groups: [], openNote: '只列申报通知，含还没截止的（日期已人工核对，或机器摘录·未经人工核对），以及可能 7 天内截止、日期还没核对的；没写明截止日期、或截止日期还没理清的不在里面', ...extra });
const OPEN_LINE = '“只看未截止”：只列申报通知，含还没截止的（日期已人工核对，或机器摘录·未经人工核对），以及可能7天内截止、日期还没核对的；没写明截止日期、或截止日期还没理清的不在里面';

test('第 6 条：选了"只看未截止"、有结果时，结果上面也写明口径（含哪些、不含哪些）；没选、或没有结果时不写这一行', () => {
  const lines = (root) => findAll(root, hasClass('fold-note')).map(flat);
  assert.ok(lines(results({ picked: { open: ['只看未截止'] } })).includes(OPEN_LINE));
  assert.ok(!lines(results({})).includes(OPEN_LINE));
  // 没有结果时由空状态里的"去掉只看未截止"和下面同一句来讲：结果上面不写，空状态里只写一遍
  const empty = results({ total: 0, totalAll: 0, picked: { open: ['只看未截止'] }, activeChips: [{ key: 'open:只看未截止', group: 'open', option: '只看未截止', label: '只看未截止' }] });
  assert.ok(!lines(empty).includes(OPEN_LINE));
  assert.deepEqual(findAll(empty, hasClass('empty-note')).map(flat).filter((t) => /只看未截止/.test(t)), [OPEN_LINE]);
});

test('第 7 条：转发合并的通知，截止卡片里写明上面的日期是本页这一版的，别的地方看"各版本的截止"；不是转发组的不写', async () => {
  const repost = { versions: [
    { id: '5', label: '江西版', current: true, issuer: '江西省科学技术厅', date: '2026-10-08', deadlines: [{ label: '企业申报截止', date: plus(9) }], url: 'https://a.example/5' },
    { id: '6', label: '部委', current: false, issuer: '科学技术部', date: '2026-10-01', deadlines: [], deadline_state: 'unverified', url: 'https://a.example/6' },
  ] };
  const deadlines = [{ kind: '企业申报截止', source: 'verified', date: plus(9), time: null, remainingDays: 9, quote: null }];
  let doc = { ...DOC, deadlines, repost };
  const page = detailPage(() => doc);
  page.onLoad({ id: '5' }); await flush();
  let root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  const due = findAll(root, hasClass('due-card'))[0];
  assert.equal(flat(findAll(due, hasClass('ver-hint'))[0]), '上面这一栏只对应本页这一版（江西版）；同一通知其他地方的版本，截止看下面“各版本的截止”');
  // 提示说的那一块就在截止卡片后面
  const cards = elements(root).filter((n) => /card/.test(n.attrs.class || ''));
  const at = cards.indexOf(due);
  assert.match(flat(cards[at + 1]), /^各版本的截止/);
  doc = { ...DOC, deadlines };
  page.refreshing = false; page.load(); await flush();
  root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.equal(findAll(root, hasClass('ver-hint')).length, 0);
});

test('第 2 条附带：选文件那里写明旧版 .xls 要先另存为 .xlsx（选择器里本来就选不到 .xls）', () => {
  const text = textOf(render('pages/advisor-form/index.wxml', { ...FORM, mode: 'file' }));
  assert.match(text, /只支持 XLSX \/ CSV（旧版 \.xls 请先另存为 \.xlsx），文件不超过 60 KB。/);
  assert.match(read('pages/advisor-form/index.js'), /extension: \['xlsx', 'csv'\]/);
});

test('第 5 条附带：截止页、我的客户页和搜索页同一说法——直辖市区县一栏选"全部"时请求里只带省份、不带城市（后端按省份查、不限层级，区县的通知也在内），不再写"直辖市不选区县时也是（只查市级）"', async () => {
  const calls = [];
  const page = load('pages/report/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': { deadlineReport: (params) => { calls.push(plain(params)); return Promise.resolve({ summary: {}, by_province: {}, items: [], total: 0, verified_total: 0 }); }, calendar: () => Promise.resolve({ items: [], past: [] }) },
    '../../utils/exporter.js': { exportDocs() {} }, '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/regions.js': require('../utils/regions.js'), '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null) },
  }, { getApp: () => ({ globalData: {} }), getCurrentPages: () => [{}, {}], wx: { navigateTo() {}, setNavigationBarTitle() {} } });
  page.onLoad({}); await flush();
  page.chooseCity({ detail: { value: ['北京市', '北京市', '全部'] } }); await flush();
  const last = calls.at(-1);
  assert.equal(last.province, '北京'); assert.equal(last.city, ''); assert.equal(last.district, undefined);
  const clients = require('../utils/clients.js');
  const params = clients.clientParams({ id: 'c1', name: '甲', regions: ['北京'], cities: [], districts: [] });
  assert.match(params.regions, /(^|,)北京(,|$)/); assert.equal(params.cities, undefined); assert.equal(params.districts, undefined);
  // 搜索页：选"北京市/北京市/全部"后发出的请求只带省份，选完的提示和三页的规则同一句
  const searches = [];
  const search = load('pages/search/index.js', {
    '../../utils/api.js': { search: (p) => { searches.push(plain(p)); return Promise.resolve({ total: 0, total_all: 0, items: [] }); } },
    '../../utils/regions.js': require('../utils/regions.js'), '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/exporter.js': { exportDocs() {}, MAX: 200 }, '../../utils/consent.js': { loginOpen: () => true, ensureConsent: () => Promise.resolve(true) },
    '../../utils/subscribe.js': { remindOpen: () => false, requestOnce: () => Promise.resolve(false) }, '../../utils/conditions.js': require('../utils/conditions.js'),
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({ cities: [], districts: [] }), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: {} });
  search.onLoad({}); await flush();
  search.chooseCity({ detail: { value: ['北京市', '北京市', '全部'] } }); await flush();
  assert.equal(searches.at(-1).regions, '北京'); assert.equal(searches.at(-1).cities, undefined); assert.equal(searches.at(-1).districts, undefined);
  assert.equal(search.data.cityError, '直辖市没选具体区县：查整个直辖市（各级来源都在内）');
  for (const pageName of ['report', 'clients', 'search']) {
    const text = read(`pages/${pageName}/index.wxml`).replace(/<!--[\s\S]*?-->/g, '');
    assert.doesNotMatch(text, /直辖市不选区县时也是|按省级范围查询/, pageName);
    assert.match(text, /只查这个市已收录的市级来源，不含区县/, pageName);
    assert.match(text, /直辖市例外：区县一栏选“全部”时查整个直辖市（各级来源都在内）/, pageName);
    // 城市一栏选"全部"＝全省各级：搜索页在规则第一句，截止页、我的客户页在同一行里
    assert.match(text, /城市一栏选“全部”(：)?查全省各级来源/, pageName);
  }
});

test('Grok 第 3 次走查：关注确认框写明下面只是名称、改名不改条件；通知表的上传页写明按省份和条件列通知、客户名称只作标识、不判断能不能报；正文的按钮写"展开正文"（截断的正文展开后也不是全部）', async () => {
  let confirm;
  const page = load('pages/search/index.js', {
    '../../utils/api.js': { search: () => Promise.resolve({ total: 0, total_all: 0, items: [] }) },
    '../../utils/regions.js': require('../utils/regions.js'), '../../utils/format.js': format, '../../utils/daywatch.js': daywatch,
    '../../utils/exporter.js': { exportDocs() {}, MAX: 200 }, '../../utils/consent.js': { loginOpen: () => true, ensureConsent: () => Promise.resolve(true) },
    '../../utils/subscribe.js': { remindOpen: () => false, requestOnce: () => Promise.resolve(false) }, '../../utils/conditions.js': require('../utils/conditions.js'),
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({ cities: [], districts: [] }), notCoveredNames: () => '', allUncovered: () => [] },
  }, { wx: { showModal: (o) => { confirm = o; } } });
  page.onLoad({ q: '专精特新', regions: '山东', open: '1' }); await flush();
  page.follow(); await flush();
  assert.equal(confirm.title, '按当前搜索条件关注（下面是名称，改名不改条件）');
  assert.equal(confirm.editable, true);
  assert.match(confirm.content, /“专精特新” · 山东 · 全部 · 近 30 天 · 只看未截止/);
  const line = '按每行的省份和填了的条件，列出近 90 天发布的申报通知（已确认截止的不列，截止没核对的照列，以原文为准）；关键词不填就不限主题；客户名称只用来标出是哪家，不判断能不能报。';
  assert.ok(textOf(render('pages/advisor-form/index.wxml', { ...FORM, kind: 'track', title: '客户相关通知表' })).includes(line));
  assert.ok(!textOf(render('pages/advisor-form/index.wxml', FORM)).includes(line));
  const body = { status: 'ok', text: '一'.repeat(800), total: 800, truncated: false };
  const detail = detailPage(() => ({ ...DOC, kind: '政策', body }));
  detail.onLoad({ id: '5' }); await flush();
  const more = findAll(render('pages/detail/index.wxml', { ...detail.data, loading: false }), hasClass('body-more'))[0];
  assert.equal(flat(more), '展开正文');
});

test('D-087（创始人 10/23 按 #341 推荐定）：工具中心两个工具按钮、在售时购买卡片的按钮都是白底次按钮；底栏"文件"改名"导出"，页面标题"导出记录"', () => {
  const base = { loading: false, toolsEnabled: true, dataEnabled: true, compareEnabled: true, trackEnabled: true, aiEnabled: false, aiGate: '', recent: [], recentLoading: false, dataStatus: '本月比对剩余 3/3 次',
    payCard: { usedUp: false, quotaLine: '本月免费：比对还剩 3 次', offerLine: '要一次处理更多家', button: '了解批量工具' } };
  const root = render('pages/advisor/index.wxml', base);
  for (const node of [...findAll(root, (n) => n.attrs['data-kind'] === 'compare' || n.attrs['data-kind'] === 'track'), ...findAll(root, tapsTo('buyPass'))]) {
    assert.doesNotMatch(node.attrs.class, /btn-primary/, flat(node));
  }
  assert.equal(findAll(root, tapsTo('buyPass')).length, 1);
  assert.equal(flat(findAll(root, hasClass('advisor-title'))[0]), '批量工具', '购买卡片仍在最上面（D-076）');
  const app = JSON.parse(read('app.json'));
  assert.deepEqual(app.tabBar.list.map((t) => t.text), ['首页', '关注', '导出', '我的', 'AI']);
  assert.equal(JSON.parse(read('pages/files/index.json')).navigationBarTitleText, '导出记录');
  assert.match(read('custom-tab-bar/index.js'), /pagePath: '\/pages\/files\/index', text: '导出'/);
  assert.doesNotMatch(read('utils/data-tools.js'), /文件页/);
});
