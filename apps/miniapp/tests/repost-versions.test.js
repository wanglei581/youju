const test = require('node:test');
const AI_OFF = require('./helpers/ai-off.js'); // 没给 AI 板块状态的页面当作 AI 关着
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const repost = require('../utils/repost.js');

// 转发版本（D-055）：列表里同一份部里通知的各地版本只列一条，写“另有 N 个转发版本”；详情里截止按版本列出，
// 不同版本的日期不互相套用。依赖都用替身，不读取 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    module, setTimeout, clearTimeout,
    require(name) { if (!Object.hasOwn(deps, name) && /(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages; if (!Object.hasOwn(deps, name) && /ai-access\.js$/.test(name)) return AI_OFF; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, Component(d) { def = d; }, ...globals,
  }, { filename: file });
  return def ? { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, values); } } : module.exports;
}
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const event = (dataset) => ({ currentTarget: { dataset } });
const FORBIDDEN = ['AI', '智能', '大模型'];
const DIGITS = /[0-9０-９]/;

const GROUP = {
  id: 7, label: '陕西版', versions: 2, regions: ['天津', '福建'],
  items: [
    { id: '12', label: '天津版', region: '天津', issuer: '天津市工业和信息化局', date: '2026-09-23', url: 'https://gyxxh.tj.gov.cn/1.html' },
    { id: '13', label: '福建版', region: '福建', issuer: '福建省工业和信息化厅', date: '2026-09-24', url: 'https://gxt.fujian.gov.cn/2.html' },
  ],
};
const REPOST = {
  group_id: 7,
  label: '福建版',
  upstream: { title: '工业和信息化部等七部门办公厅（办公室）关于组织开展2026年度视听系统典型案例征集工作的通知', number: '工信厅联电子函〔2026〕376号' },
  versions: [
    { id: '13', label: '福建版', current: true, region: '福建', issuer: '福建省工业和信息化厅', date: '2026-09-24', url: 'https://gxt.fujian.gov.cn/2.html', deadlines: [], deadline_state: 'unverified' },
    { id: '11', label: '陕西版', current: false, region: '陕西', issuer: '陕西省工业和信息化厅', date: '2026-09-22', url: 'https://gxt.shaanxi.gov.cn/3.html', deadlines: [{ label: '推荐或审核截止', date: '2026-10-10', time: '' }], deadline_state: 'verified' },
    { id: '12', label: '天津版', current: false, region: '天津', issuer: '天津市工业和信息化局', date: '2026-09-23', url: 'https://gyxxh.tj.gov.cn/1.html', deadlines: [{ label: '企业申报截止', date: '2026-10-12', time: '16:00' }], deadline_state: 'verified' },
    { id: '14', label: '北京版', current: false, region: '北京', issuer: '北京市经济和信息化局', date: '2026-09-25', url: 'https://jxj.beijing.gov.cn/4.html', deadlines: [], deadline_state: 'none' },
  ],
};

test('“另有 N 个转发版本”：最多写 3 个地区，多了写“等”；不是组的不写', () => {
  assert.equal(repost.groupLine(GROUP), '另有 2 个转发版本（天津、福建）');
  assert.equal(
    repost.groupLine({ versions: 5, regions: ['天津', '福建', '北京', '河南', '湖北'] }),
    '另有 5 个转发版本（天津、福建、北京等）',
  );
  assert.equal(repost.groupLine(null), '');
  assert.equal(repost.groupLine({ versions: 0, regions: [] }), '');
});

test('各版本的截止只写各自核对过的；没核对的不写日期', () => {
  const rows = repost.versionRows(REPOST);
  assert.deepEqual(rows.map((r) => r.label), ['福建版', '陕西版', '天津版', '北京版']);
  assert.equal(rows[0].current, true);
  assert.equal(rows[0].deadline, '截止时间见原文');
  assert.doesNotMatch(rows[0].deadline, DIGITS); // 不借用陕西版、天津版的日期
  assert.equal(rows[1].deadline, '推荐或审核截止 10月10日');
  assert.equal(rows[2].deadline, '企业申报截止 10月12日 16:00');
  assert.equal(rows[3].deadline, '截止时间见原文');
  assert.equal(rows[2].url, 'https://gyxxh.tj.gov.cn/1.html'); // 每个版本自己的原文
  assert.equal(rows[1].meta, '陕西省工业和信息化厅 · 2026-09-22 发布');
  // 库里有部里原件时叫“部委”，标签照接口给的写，不另拼
  const ministry = repost.versionRows({ versions: [{ id: '1', label: '部委', deadlines: [{ label: '企业申报截止', date: '2026-10-20', time: '' }] }] });
  assert.equal(ministry[0].label, '部委');
  assert.equal(ministry[0].deadline, '企业申报截止 10月20日');
  assert.deepEqual(repost.versionRows(undefined), []);
});

test('部里通知只照录正文引用的标题和文号；没有文号就不写', () => {
  assert.equal(
    repost.upstreamLine(REPOST),
    '这些通知都转发或依据同一份部委通知：《工业和信息化部等七部门办公厅（办公室）关于组织开展2026年度视听系统典型案例征集工作的通知》（工信厅联电子函〔2026〕376号）',
  );
  assert.equal(repost.upstreamLine({ upstream: null }), '');
  assert.equal(repost.upstreamLine(undefined), '');
});

test('首页、搜索、名单页的请求都带 group=1（按组折叠、按组计数）', async () => {
  const urls = [];
  const api = load('utils/api.js', {
    './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': format,
  }, { wx: { request: ({ url, success }) => { urls.push(new URL(url)); success({ statusCode: 200, data: { stats: {}, items: [], total: 0 } }); } } });
  await api.home();
  await api.search({ q: '视听', kind: 'all' });
  await api.lists();
  // 首页（D-061）另取最新的 8 份申报通知，也按组折叠；首页上只显示人工核对过的截止，所以这一请求不带 excerpts
  assert.deepEqual(urls.map((u) => [u.pathname, u.searchParams.get('group'), u.searchParams.get('excerpts')]), [
    ['/api/v1/home', '1', '1'], ['/api/v1/search', '1', null], ['/api/v1/search', '1', '1'], ['/api/v1/lists/recent', '1', null],
  ]);
  assert.equal(urls[1].searchParams.get('kind'), '申报通知'); assert.equal(urls[1].searchParams.get('limit'), '8');
  assert.equal(urls[2].searchParams.get('q'), '视听');
});

test('列表行：组的主条目写“另有 N 个转发版本”，点开逐个列出，各版本打开自己的详情（不复制链接，10/23 审核退回）', () => {
  const opened = [];
  const row = load('components/doc-row/index.js', {
    '../../utils/format.js': format, '../../utils/repost.js': repost,
  }, { wx: { navigateTo: ({ url }) => opened.push(url) } });
  const call = (name, ...args) => row.methods[name].call(row, ...args);
  row.data.doc = { id: '11', kind: '申报通知', title: '关于组织开展2026年度视听系统典型案例征集工作的通知', group: GROUP };
  row.observers['doc, q, today'].call(row, row.data.doc, '');
  assert.equal(row.data.groupText, '另有 2 个转发版本（天津、福建）');
  assert.equal(row.data.groupOpen, false);
  call('toggleGroup');
  assert.equal(row.data.groupOpen, true);
  call('openVersion', event({ id: '12' }));
  assert.deepEqual(opened, ['/pages/detail/index?id=12']);
  assert.equal(row.methods.copyVersion, undefined);
  assert.equal(row.methods.copy, undefined);
  // 勾选导出时点行不打开
  row.data.selecting = true;
  call('openVersion', event({ id: '13' }));
  assert.equal(opened.length, 1);
  // 换一条时收起；没有组的不写
  row.observers['doc.id'].call(row);
  assert.equal(row.data.groupOpen, false);
  row.observers['doc, q, today'].call(row, { id: '20', kind: '申报通知', title: '单条' }, '');
  assert.equal(row.data.groupText, '');

  const template = read('components/doc-row/index.wxml');
  assert.match(template, /wx:if="{{groupText}}" class="group" catchtap="toggleGroup"/);
  assert.match(template, /<t-icon name="{{groupOpen \? 'chevron-up' : 'chevron-down'}}"/);
  assert.match(template, /wx:for="{{doc.group.items}}"[^>]*catchtap="openVersion"/);
  assert.match(template, /data-id="{{item.id}}" catchtap="openVersion"/);
  assert.doesNotMatch(template, /tap="copy(Version)?"|name="copy"|复制/);
  assert.doesNotMatch(read('components/doc-row/index.js'), /copyLink|setClipboardData/);
});

test('详情页：各版本的截止按版本列出，点别的版本打开它的详情，本页不跳转', async () => {
  const opened = [];
  const doc = {
    id: '13', kind: '申报通知', tags: ['申报通知'], title: '福建省工业和信息化厅等六部门关于开展2026年度视听系统典型案例征集工作的通知',
    deadline: null, deadline_state: 'unverified', points: [], attachments: [], repost: REPOST,
  };
  const page = load('pages/detail/index.js', {
    '../../utils/api.js': { detail: () => Promise.resolve(JSON.parse(JSON.stringify(doc))) },
    '../../utils/format.js': format, '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/repost.js': repost, '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {},
    '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/attachments.js': { decorate: () => [], note: () => '' },
  }, { wx: { navigateTo: ({ url }) => opened.push(url) } });
  page.onLoad({ id: '13' });
  await flush();
  assert.equal(page.data.versions.length, 4);
  assert.equal(page.data.versions[0].deadline, '截止时间见原文');
  assert.equal(page.data.due, null); // 本版没有核对过的截止：不借用其他版本的
  assert.match(page.data.upstream, /^这些通知都转发或依据同一份部委通知：《/);
  page.openVersion(event({ id: '13', current: true }));
  assert.deepEqual(opened, []);
  page.openVersion(event({ id: '12', current: false }));
  assert.deepEqual(opened, ['/pages/detail/index?id=12']);
  assert.equal(page.copyVersion, undefined);

  const template = read('pages/detail/index.wxml');
  assert.match(template, /wx:if="{{versions.length}}" class="card ver-card"/);
  assert.match(template, /各版本的截止/);
  assert.match(template, /data-current="{{item.current}}" bindtap="openVersion"/);
  assert.doesNotMatch(template, /copyVersion|复制原文链接/);
});

test('首页、搜索：按组计数，不再另写"另有几个转发版本未重复计"（10/29 删提示：每行已有"各地版本"可展开）', () => {
  assert.doesNotMatch(read('pages/home/index.wxml'), /week_reposts|转发版本/);
  assert.doesNotMatch(read('pages/search/index.wxml'), /reposts|不重复计数/);
});

test('新加的文字不出现禁用字样', () => {
  const texts = [
    read('utils/repost.js'),
    read('components/doc-row/index.wxml'),
    /<!-- 转发版本（D-055）[\s\S]*?<!-- 要点/.exec(read('pages/detail/index.wxml'))[0],
  ];
  for (const text of texts) for (const word of FORBIDDEN) assert.ok(!text.includes(word), `${word} 出现在新文字里`);
});

test('多个关注命中同组不同版本：新文件流只列一条，未确认的单条仍各自保留', async () => {
  const page = load('pages/follow/index.js', {
    '../../utils/api.js': { followNew: (id) => Promise.resolve({ items: id === 1
      ? [{ id: '11', date: '2026-09-22', group: GROUP }, { id: '20', date: '2026-09-20' }]
      : [{ id: '12', date: '2026-09-23', group: GROUP }, { id: '21', date: '2026-09-21' }] }) },
    '../../utils/format.js': format,
    '../../utils/daywatch.js': {},
    '../../utils/subscribe.js': { remindOpen: () => true },
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/conditions.js': {},
  });
  await page.loadChanges([{ id: 1, name: '陕西', newCount: 2 }, { id: 2, name: '天津', newCount: 2 }]);
  assert.deepEqual(Array.from(page.data.changes, (d) => d.id), ['12', '21', '20']);
  assert.equal(page.data.changes[0].group.versions, 2);
  assert.equal(page.data.changes[0].label, '天津');
});
