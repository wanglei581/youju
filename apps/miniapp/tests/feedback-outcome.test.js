const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');
const conditions = require('../utils/conditions.js');
const { render } = require('./helpers/wxml.js');

const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));

// 仅加载被测页面；依赖都用替身，不访问 legal.js 或本机配置。
function load(file, deps = {}, globals = {}) {
  let page;
  vm.runInNewContext(read(file), {
    module: { exports: {} }, setTimeout, clearTimeout,
    require(name) { if (!Object.hasOwn(deps, name) && /(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages; if (!Object.hasOwn(deps, name) && /utils\/advisor\.js$/.test(name)) return require('./helpers/package-pages-default.js').advisor; assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page(def) { page = def; }, ...globals,
  }, { filename: file });
  return { ...page, data: JSON.parse(JSON.stringify(page.data)), setData(values) { Object.assign(this.data, values); } };
}

test('纠错处理结果（C1）：我的数据写出处理结果和说明；以前处理的照旧只写已处理', async () => {
  const raw = {
    user: { openid: 'synthetic', agreed: null }, follows: [], subscriptions: [], retention: {},
    feedback: [
      { id: 1, content: '截止日期写错了', checkResult: 'pass', handled: true, handledAt: '2026-10-19T15:00:00+08:00', outcome: 'fixed', outcomeLabel: '已更正', reply: '已改成 11 月 30 日' },
      { id: 2, content: '一般建议', checkResult: 'pass', handled: true, handledAt: '2026-10-03T11:00:00+08:00', outcome: null, outcomeLabel: null, reply: null },
      { id: 3, content: '还没处理的', checkResult: 'pass', handled: false, handledAt: null, outcome: null, outcomeLabel: null, reply: null },
    ],
  };
  const page = load('pages/my-data/index.js', {
    '../../utils/api.js': { myData: () => Promise.resolve(raw) },
    '../../utils/conditions.js': conditions, '../../utils/consent.js': { loginOpen: () => true, remindAny: () => true },
  }, { wx: { showToast() {} } });
  page.load(); await flush();
  const [fixed, old, pending] = page.data.feedback;
  assert.equal(fixed.outcome, '已更正');
  assert.equal(fixed.reply, '已改成 11 月 30 日');
  assert.equal(old.outcome, '');
  assert.equal(old.handling, '已处理');
  assert.equal(pending.outcome, '');
  assert.equal(pending.reply, '');
  const text = render(read('pages/my-data/index.wxml'), page.data);
  assert.match(text, /处理结果：已更正 · 处理于 2026-10-19 15:00 处理说明：已改成 11 月 30 日/);
  assert.match(text, /处理状态：已处理 · 处理于 2026-10-03 11:00/);
  assert.match(text, /处理状态：待处理/);
  assert.equal((text.match(/处理说明：/g) || []).length, 1);
});

test('纠错处理结果（C1）：没处理的不显示结果，哪怕接口带了也不显示', async () => {
  const raw = {
    user: { openid: 'synthetic', agreed: null }, follows: [], subscriptions: [], retention: {},
    feedback: [{ id: 4, content: '撤回过的', checkResult: 'pass', handled: false, handledAt: null, outcome: 'fixed', outcomeLabel: '已更正', reply: '旧说明' }],
  };
  const page = load('pages/my-data/index.js', {
    '../../utils/api.js': { myData: () => Promise.resolve(raw) },
    '../../utils/conditions.js': conditions, '../../utils/consent.js': { loginOpen: () => true, remindAny: () => true },
  }, { wx: { showToast() {} } });
  page.load(); await flush();
  assert.equal(page.data.feedback[0].outcome, '');
  assert.equal(page.data.feedback[0].reply, '');
  const text = render(read('pages/my-data/index.wxml'), page.data);
  assert.match(text, /处理状态：待处理/);
  assert.doesNotMatch(text, /旧说明|处理结果：/);
});

test('纠错页写明到哪里看处理结果', () => {
  assert.match(read('pages/feedback/index.wxml'), /处理结果会写在“我的 → 我的数据”里。/);
});

test('"新文件"页（X3）：没带关注编号不请求接口，提示回"关注"页', async () => {
  const calls = [];
  const switched = [];
  const make = () => load('pages/follow-new/index.js', {
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/api.js': { followNew: (id) => { calls.push(id); return Promise.resolve({ total: 0, folded: 0, items: [] }); } },
    '../../utils/format.js': format, '../../utils/conditions.js': conditions,
  }, { wx: { setNavigationBarTitle() {}, switchTab: ({ url }) => switched.push(url) } });
  for (const query of [{}, { id: 'undefined' }, { id: '' }, { id: '0' }, { id: '7x' }]) {
    const page = make();
    page.onLoad(query); await flush();
    assert.equal(page.data.missing, true, JSON.stringify(query));
    assert.equal(page.data.loading, false);
    const text = render(read('pages/follow-new/index.wxml'), page.data);
    assert.match(text, /没有找到要看的关注，请回到“关注”页点开它 去“关注” ›/);
    assert.doesNotMatch(text, /正在加载/);
    page.goFollow();
  }
  assert.deepEqual(calls, []);
  assert.deepEqual(switched.at(-1), '/pages/follow/index');
  const page = make();
  page.onLoad({ id: '7', name: encodeURIComponent('高企') }); await flush();
  assert.equal(page.data.missing, false);
  assert.deepEqual(calls, ['7']);
});
