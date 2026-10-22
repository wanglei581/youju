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
  assert.equal(flat(findAll(card(root), hasClass('body-note'))[0]), '从原网页或PDF提取，仅供阅读参考，以原文为准。');
  // 政策文件上面没有截止，说明里不再指向"上面的截止时间"
  assert.doesNotMatch(textOf(card(root)), /截止/);
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
  assert.equal(flat(kids[at - 1]), '先点上面的“预览并核对行数”，核对通过后才能提交');
  // 提示里说的按钮确实在上面
  assert.equal(findAll(render('pages/advisor-form/index.wxml', FORM), (n) => n.attrs.bindtap === 'preview' && /预览并核对行数/.test(flat(n))).length, 1);
  card = submitCard({ ...FORM, previewed: true, rowCount: 3 });
  kids = elements(card);
  at = kids.findIndex(tapsTo('submit'));
  assert.equal(kids[at].attrs.disabled, false);
  assert.doesNotMatch(textOf(card), /先点上面的/);
});

test('第 4 条：关注页写的按钮名和搜索页按钮上的字一样（"关注"），不再写"关注这个搜索"', () => {
  const gated = textOf(render('pages/follow/index.wxml', { loginOpen: true, mode: 'gated', loading: false, follows: [], changes: [] }));
  assert.match(gated, /在搜索结果页点"关注"，把当时的关键词、地区、类型等条件存下来；有新文件时在这里标出。/);
  const empty = textOf(render('pages/follow/index.wxml', { loginOpen: true, mode: 'live', loading: false, error: '', follows: [], changes: [] }));
  assert.match(empty, /还没有关注。在搜索结果页点"关注"就能加上。/);
  for (const text of [gated, empty]) assert.doesNotMatch(text, /关注这个搜索/);
  // 搜索页按钮上就是"关注"
  assert.match(read('pages/search/index.wxml'), /class="follow-mini" bindtap="follow"[^>]*><t-icon name="notification"[^>]*\/>关注<\/view>/);
});

const CITY = { key: 'city', title: '城市区县', hint: '省市区联动 · 可多选', multi: true, options: [], note: '省市区选项由微信提供，选项齐全不代表已收录数据；选择具体城市会替换同省的全省条件，选择具体区县会替换所属城市和全省的条件。' };
const sheet = (extra = {}) => render('pages/search/index.wxml', {
  filterOpen: true, activeGroup: 'city', groups: [CITY], districtOptions: [], cityCoverageNote: '当前仅有 3 个城市的部分来源，不代表全市数据完整。', pickedMap: {}, counts: {}, entries: {}, activeChips: [], picked: { open: [] }, ...extra,
});
const notes = (root) => findAll(root, hasClass('group-note')).map(flat);

test('第 5 条：城市区县的规则拆成短句，紧跟在选择器下面；"点一下移除"只在有已选的城市、区县时出现', () => {
  let root = sheet();
  const pane = findAll(root, hasClass('pane-inner'))[0];
  const kids = elements(pane);
  const picker = kids.findIndex((n) => n.tag === 'picker');
  assert.ok(picker > 0);
  assert.deepEqual(kids.slice(picker + 1, picker + 4).map(flat), [
    '城市一栏选“全部”：查全省。',
    '区县一栏选“全部”：只查这个市已收录的市级来源，不含区县；要查区县，请选具体区县。',
    '直辖市不选区县：按省级范围查询。',
  ]);
  assert.match(flat(kids[picker + 4]), /^当前仅有3个城市的部分来源/);
  assert.ok(notes(root).includes('选择器打不开时，请升级微信。'));
  assert.ok(notes(root).every((n) => !/点一下移除/.test(n)));
  // 原来挤在一段里的那句不在了
  assert.doesNotMatch(read('pages/search/index.wxml'), /已选城市、区县点一下移除。城市一栏选/);
  // 有已选的城市或区县时，在它们下面写"点一下移除"
  root = sheet({ groups: [{ ...CITY, options: ['山东/青岛'] }], pickedMap: { 'city:山东/青岛': true } });
  assert.ok(notes(root).includes('已选的城市、区县，点一下移除。'));
  root = sheet({ districtOptions: [{ key: '山东/青岛/市南区', label: '山东·青岛·市南区' }] });
  assert.ok(notes(root).includes('已选的城市、区县，点一下移除。'));
});
