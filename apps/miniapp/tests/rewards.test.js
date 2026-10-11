const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { render, textOf, findAll, tapsTo } = require('./wxml-render.js');
const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const flush = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve(); };
const plain = (value) => JSON.parse(JSON.stringify(value));
const failure = (status, message = '请先登录') => () => Promise.reject(Object.assign(new Error(message), { status }));
const event = (id) => ({ currentTarget: { dataset: { id } } });
const clue = { id: 1, document_id: 12, kind: '企业申报截止', date: '2026-10-31', time: null, quote: '申报截止日期为十月三十一日', status: '待核对' };
function load(file, deps, globals = {}) {
  let def; const module = { exports: {} };
  vm.runInNewContext(read(file), { require(key) { assert.ok(key in deps, `缺少依赖 ${key}`); return deps[key]; }, module, Page: (d) => { def = d; }, App: (d) => { def = d; }, setTimeout, clearTimeout, ...globals }, { filename: file });
  if (!def) return module.exports;
  return { ...def, data: plain(def.data || {}), setData(d) { Object.assign(this.data, d); } };
}
function page(name, overrides = {}, options = {}) {
  const calls = { deleted: [], submitted: [], modals: [], toasts: [], urls: [], timers: [], hidden: 0, copied: [] };
  const api = { payProducts: () => Promise.resolve({ items: [] }), payOrders: () => Promise.resolve({ items: [] }), isLive: () => true, myRewards: () => Promise.resolve({ monthDays: 0, items: [] }), inviteInfo: () => Promise.resolve({ inviteCode: 'abcdefgh1234', monthCount: 1, monthLimit: 4 }),
    myDeadlineClues: () => Promise.resolve({ items: [] }), detail: () => Promise.resolve({ title: '通知标题' }),
    deleteDeadlineClue: (id) => { calls.deleted.push(id); return Promise.resolve(); }, submitDeadlineClue: (body) => { calls.submitted.push(plain(body)); return Promise.resolve(); },
    me: () => Promise.resolve({ agreed: options.signedIn === false ? null : {}, follows: 0, subscriptions: {} }), ...overrides };
  const wx = { showModal: (o) => { calls.modals.push(o); o.success({ confirm: options.confirm !== false }); }, showToast: (o) => calls.toasts.push(o),
    hideShareMenu: () => { calls.hidden += 1; }, navigateTo: ({ url }) => calls.urls.push(url), navigateBack: () => calls.urls.push('back'), redirectTo: ({ url }) => calls.urls.push(url),
    setClipboardData: ({ data }) => calls.copied.push(JSON.parse(data)) };
  const helper = load('utils/rewards.js', { './api.js': api }, { wx });
  const deps = { '../../utils/api.js': api, '../../utils/rewards.js': helper, '../../content/legal.js': { final: true }, '../../utils/pay.js': require('../utils/pay.js'),
    '../../utils/consent.js': { loginOpen: () => true, applyServer: (value) => !!value, consentEpoch: () => 0, withdraw: () => Promise.resolve(true), syncConsent: () => Promise.resolve(options.signedIn !== false) },
    '../../utils/coverage.js': {}, '../../utils/subscribe.js': { remindOpen: () => false }, '../../utils/advisor.js': { taskView: (v) => v }, '../../utils/conditions.js': { describe: () => '' },
    '../../utils/format.js': require('../utils/format.js'), '../../utils/copy.js': {}, '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) }, '../../utils/exporter.js': {},
    '../../utils/daywatch.js': {}, '../../utils/attachments.js': {}, '../../utils/repost.js': {} };
  const view = load(`pages/${name}/index.js`, deps, { wx, setTimeout: (fn) => { calls.timers.push(fn); return calls.timers.length; }, clearTimeout() {} });
  const show = () => render(`pages/${name}/index.wxml`, view.data);
  return { view, api, calls, show, text: () => textOf(show()) };
}
for (const name of ['rewards', 'invite', 'deadline-clue']) {
  test(`${name}：404 整页未开放，没有操作按钮或规则`, async () => {
    const p = page(name, { myRewards: failure(404), inviteInfo: failure(404) }); await p.view.onShow();
    assert.equal(p.text().trim(), '这个功能暂未开放'); assert.equal(findAll(p.show(), (n) => n.tag === 'button').length, 0);
    if (name === 'deadline-clue') { await p.view.submit(); assert.equal(p.calls.submitted.length, 0); }
    if (name === 'invite') assert.equal(p.view.onShareAppMessage().path, '/pages/home/index');
  });
  test(`${name}：未登录显示接口说明，失败可以重试`, async () => {
    const p = page(name, { myRewards: failure(403), inviteInfo: failure(403) }); await p.view.onShow(); assert.match(p.text(), /请先登录/);
    assert.equal(findAll(p.show(), (n) => n.tag === 'button').length, 0);
    p.api.myRewards = () => Promise.resolve({ monthDays: 0, items: [] }); p.api.inviteInfo = () => Promise.resolve({ inviteCode: 'abcdefgh1234', monthCount: 0, monthLimit: 4 });
    await p.view.load(); assert.equal(p.view.data.error, '');
  });
}
test('奖励空状态完整，R8 仅在引荐接口可用时显示', async () => {
  const p = page('rewards', { inviteInfo: failure(404) }); await p.view.onShow();
  // 顶部写这个月已得几天；每种得法一行，右边写加几天
  assert.match(p.text(), /奖励规则 · 本月已得\s*0\s*天/); assert.match(p.text(), /还没有奖励/); assert.match(p.text(), /还没有提交过线索/); assert.doesNotMatch(p.text(), /请同行来用|\+7 天/);
  assert.deepEqual(findAll(p.show(), (n) => /\brw-plus\b/.test(n.attrs.class || '')).map((n) => textOf(n).trim()), ['+1 天', '+1 天', '+3 天', '+3 天']);
  // 引荐这一行只指向"请同行来用"页，不在规则页写送几天（D-059：别处不出现"转发得天数"一类的话）
  p.api.inviteInfo = () => Promise.resolve({}); await p.view.load(); assert.match(p.text(), /请同行来用\s*规则见"请同行来用"页/); assert.doesNotMatch(p.text(), /\+7 天|7 天/);
  assert.equal(findAll(p.show(), tapsTo('invite')).length, 1);
});
test('奖励有数据：北京时间日期、独立备注、通知标题和三种状态；只删待核对', async () => {
  const p = page('rewards', { myRewards: () => Promise.resolve({ monthDays: 1, items: [{ name: '补截止日期被采纳', days: 1, grantedAt: '2026-10-11T17:00:00Z', note: '备注文字' }] }),
    myDeadlineClues: () => Promise.resolve({ items: [clue, { ...clue, id: 2, status: '已采纳' }, { ...clue, id: 3, status: '未采纳' }] }) });
  await p.view.onShow(); assert.match(p.text(), /补截止日期被采纳 · 1 天 · 2026-10-12/); assert.match(p.text(), /备注文字/);
  for (const state of ['待核对', '已采纳', '未采纳']) assert.match(p.text(), new RegExp(`通知标题 · 企业申报截止 2026-10-31 · ${state}`));
  assert.equal(findAll(p.show(), tapsTo('remove')).length, 1);
  await p.view.remove(event(2)); assert.equal(p.calls.modals.length, 0);
  p.api.myDeadlineClues = () => Promise.resolve({ items: [] }); await p.view.remove(event(1)); assert.deepEqual(p.calls.deleted, [1]); assert.match(p.text(), /还没有提交过线索/);
});
test('标题获取失败保留线索；删除取消和后端失败不会移除', async () => {
  const p = page('rewards', { detail: failure(500), myDeadlineClues: () => Promise.resolve({ items: [clue] }) }, { confirm: false }); await p.view.onShow();
  assert.match(p.text(), /通知标题暂不可用/); await p.view.remove(event(1)); assert.equal(p.calls.deleted.length, 0);
  const q = page('rewards', { deleteDeadlineClue: failure(409, '只能删除待核对的线索'), myDeadlineClues: () => Promise.resolve({ items: [clue] }) }); await q.view.onShow(); await q.view.remove(event(1));
  assert.equal(q.view.data.clues.length, 1); assert.equal(q.calls.toasts[0].title, '只能删除待核对的线索');
});
for (const count of [0, 1, 4]) test(`引荐人数 ${count}：完整文字和分享按钮，路径只含随机码`, async () => {
  const p = page('invite', { inviteInfo: () => Promise.resolve({ inviteCode: 'abcdefgh1234', monthCount: count, monthLimit: 4 }) }); await p.view.onShow();
  assert.match(p.text(), new RegExp(`本月已计 ${count} 位，还可以计 ${4 - count} 位`));
  assert.equal(findAll(p.show(), (n) => n.attrs && n.attrs['open-type'] === 'share').length, 1);
  assert.deepEqual(plain(p.view.onShareAppMessage()), { title: '查申报通知和公示名单，每条附原文链接', path: '/pages/home/index?inv=abcdefgh1234' });
  assert.equal(p.calls.modals.length, 0); assert.equal(p.view.onShareTimeline, undefined); assert.equal(p.calls.hidden, 1);
});
async function form(overrides = {}) { const p = page('deadline-clue', overrides); p.view.onLoad({ id: '12', title: encodeURIComponent('通知标题') }); await p.view.onShow(); return p; }
test('日期和性质选择器、可选时间和原句；标题由链接传入', async () => {
  const p = await form(); assert.match(p.text(), /通知标题/);
  assert.deepEqual(plain(p.view.data.kinds), ['企业申报截止', '推荐或审核截止', '材料报送截止', '其他']);
  assert.equal(findAll(p.show(), (n) => n.tag === 'picker').length, 3);
  p.view.kindChange({ detail: { value: '1' } }); p.view.dateChange({ detail: { value: clue.date } }); p.view.timeChange({ detail: { value: '17:00' } }); p.view.quoteChange({ detail: { value: `  ${clue.quote}  ` } }); await p.view.submit();
  assert.deepEqual(p.calls.submitted[0], { document_id: 12, kind: '推荐或审核截止', date: clue.date, time: '17:00', quote: clue.quote });
  assert.equal(p.text().trim(), '已收到。核对需要几天，结果在"奖励规则 → 我的线索"里看');
  p.calls.timers[0](); assert.deepEqual(p.calls.urls, ['back']); await p.view.submit(); assert.equal(p.calls.submitted.length, 1);
});
for (const length of [0, 5, 201]) test(`原句 ${length} 字不提交`, async () => {
  const p = await form(); p.view.setData({ date: clue.date, quote: '文'.repeat(length) }); await p.view.submit(); assert.equal(p.calls.submitted.length, 0); assert.match(p.text(), /原文句子须为 6–200 字/);
});
for (const length of [6, 200]) test(`原句 ${length} 字通过，时间可以清除为 null`, async () => {
  const p = await form(); p.view.setData({ date: clue.date, quote: '文'.repeat(length), time: '17:00' }); p.view.clearTime(); await p.view.submit(); assert.equal(p.calls.submitted.length, 1); assert.equal(p.calls.submitted[0].time, null);
});
test('缺日期、无效编号和性质本地校验；提交期间防止重复', async () => {
  let done; const p = await form({ submitDeadlineClue: () => new Promise((r) => { done = r; }) }); p.view.setData({ quote: clue.quote }); await p.view.submit(); assert.match(p.text(), /请选择截止日期/);
  p.view.setData({ date: clue.date, docId: 0 }); await p.view.submit(); assert.match(p.text(), /文件编号无效/);
  p.view.setData({ docId: 12, kindIndex: 9 }); await p.view.submit(); assert.match(p.text(), /请选择日期的性质/);
  p.view.setData({ kindIndex: 0 }); const promise = p.view.submit(); assert.equal(p.view.data.sending, true); await p.view.submit(); done(); await promise;
});
for (const [code, from, to] of [
  [409, '线索暂时收满了，过几天再来', '待核对的线索已经比较多，暂时不收新的，过几天再来'],
  [409, '这份通知的截止日期已经核对过了', '这份通知的截止日期已经核对过了'],
  [409, '你已经提交过这项线索，请等核对结果', '你已经提交过这项线索，请等核对结果'],
  [409, '你已有 20 条待核对线索，请等核对结果', '你已有 20 条待核对线索，请等核对结果'],
  [422, '日期须在通知发布日至发布后三年之间', '日期须在通知发布日至发布后三年之间'],
  [503, '内容安全校验暂不可用', '内容安全校验暂不可用'],
]) test(`提交错误 ${from}`, async () => {
  const p = await form({ submitDeadlineClue: failure(code, from) }); p.view.setData({ date: clue.date, quote: clue.quote }); await p.view.submit(); assert.match(p.text(), new RegExp(to)); assert.equal(p.view.data.sending, false); assert.equal(p.calls.urls.length, 0);
});
function storage() {
  const rows = new Map(); let now = Date.parse('2026-10-10T00:00:00Z');
  class Clock extends Date { static now() { return now; } }
  const wx = { getStorageSync: (key) => rows.get(key), setStorageSync: (key, v) => rows.set(key, v), removeStorageSync: (key) => rows.delete(key) };
  const invite = load('utils/invite.js', {}, { wx, Date: Clock });
  return { rows, wx, invite, advance: (ms) => { now += ms; } };
}
test('引荐码：只存最近一个，无码进入不清掉有效码；满 30 天和未来时间过期', () => {
  const s = storage(); s.invite.capture({ inv: 'abcdefgh1234' }); assert.equal(s.invite.read(), 'abcdefgh1234');
  s.invite.capture({}); s.invite.capture({ inv: 'bad' }); assert.equal(s.invite.read(), 'abcdefgh1234');
  s.invite.capture({ inv: 'ijklmnop5678' }); assert.equal(s.rows.size, 1); s.advance(s.invite.TTL - 1); assert.equal(s.invite.read(), 'ijklmnop5678'); s.advance(1); assert.equal(s.invite.read(), ''); assert.equal(s.rows.size, 0);
  s.invite.capture({ inv: 'abcdefgh1234' }); s.advance(-1); assert.equal(s.invite.read(), '');
});
test('启动和热进入卡片都静默保存码；存储失败不影响启动', () => {
  const s = storage(); const app = load('app.js', { './utils/invite.js': s.invite, './utils/tool-overlays.js': { install() {} }, './utils/config.js': {} }, { wx: s.wx });
  app.onLaunch({ query: { inv: 'abcdefgh1234' } }); assert.equal(s.invite.read(), 'abcdefgh1234'); app.onShow({ query: { inv: 'ijklmnop5678' } }); assert.equal(s.invite.read(), 'ijklmnop5678');
  s.wx.getStorageSync = () => { throw Error('storage'); }; s.wx.setStorageSync = () => { throw Error('storage'); }; s.wx.removeStorageSync = () => { throw Error('storage'); }; app.onShow({ query: { inv: 'abcdefgh1234' } });
});
function apiHarness() {
  const s = storage(); const calls = []; let code = 200; let received;
  s.wx.request = (o) => { calls.push(o); if (received) received(); o.success({ statusCode: code, data: { detail: '请重试' } }); };
  const api = load('utils/api.js', { './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, '../mock/advisor.js': {}, './format.js': {}, './invite.js': s.invite }, { wx: s.wx });
  return { ...s, api, calls, status: (c) => { code = c; }, during: (fn) => { received = fn; } };
}
test('普通注册带码并清掉；失败保留；工具协议不带、不清', async () => {
  const s = apiHarness(); s.invite.capture({ inv: 'abcdefgh1234' }); s.status(422); await assert.rejects(s.api.agree('v1')); assert.equal(s.invite.read(), 'abcdefgh1234'); s.status(200);
  await s.api.agree('v1', 'advisor'); assert.deepEqual(plain(s.calls.at(-1).data), { version: 'v1', scope: 'advisor' }); assert.equal(s.invite.read(), 'abcdefgh1234');
  await s.api.agree('v1'); assert.deepEqual(plain(s.calls.at(-1).data), { version: 'v1', invite_code: 'abcdefgh1234' }); assert.equal(s.invite.read(), '');
});
test('过期码不传；请求期间的新码保留到下一次注册', async () => {
  const s = apiHarness(); s.invite.capture({ inv: 'abcdefgh1234' }); s.advance(s.invite.TTL); await s.api.agree('v1', 'general'); assert.deepEqual(plain(s.calls.at(-1).data), { version: 'v1', scope: 'general' });
  s.invite.capture({ inv: 'abcdefgh1234' }); s.during(() => s.invite.capture({ inv: 'ijklmnop5678' })); await s.api.agree('v1'); assert.equal(s.invite.read(), 'ijklmnop5678');
});
test('五个新 API 方法使用已有接口及请求体', async () => {
  const s = apiHarness(); await s.api.myRewards(); await s.api.inviteInfo(); await s.api.myDeadlineClues(); await s.api.submitDeadlineClue(clue); await s.api.deleteDeadlineClue(1);
  assert.deepEqual(s.calls.map((c) => [new URL(c.url).pathname, c.method]), [['/api/v1/me/rewards', 'GET'], ['/api/v1/me/invite', 'GET'], ['/api/v1/me/deadline-clues', 'GET'], ['/api/v1/me/deadline-clues', 'POST'], ['/api/v1/me/deadline-clues/1', 'DELETE']]);
  assert.deepEqual(plain(s.calls[3].data), clue);
});
for (const [r, i] of [[true, true], [true, false], [false, true], [false, false]]) test(`我的入口奖励 ${r}、引荐 ${i} 独立按 404 判断`, async () => {
  const p = page('me', { myRewards: r ? () => Promise.resolve({}) : failure(404), inviteInfo: i ? () => Promise.resolve({}) : failure(404) }); p.view.refresh(); await flush();
  assert.equal(p.text().includes('奖励规则'), r); assert.equal(p.text().includes('请同行来用'), i);
});
test('我的未登录不请求奖励，注销后在途接口不能恢复入口', async () => {
  let count = 0; const p = page('me', { myRewards: () => { count += 1; return Promise.resolve({}); } }, { signedIn: false }); p.view.refresh(); await flush(); assert.equal(count, 0); assert.doesNotMatch(p.text(), /奖励规则|请同行来用/);
  let finish; const q = page('me', { myRewards: () => new Promise((r) => { finish = r; }) }); q.view.setData({ signedIn: true }); const waiting = q.view.loadRewardEntries(); q.view.deleteAccount(); await flush(); finish({}); await waiting; assert.equal(q.view.data.rewardsOpen, false); assert.equal(q.view.data.inviteOpen, false);
});
for (const flag of [false, true]) for (const signedIn of [false, true]) test(`详情线索入口 flag=${flag} 登录=${signedIn}`, async () => {
  const p = page('detail', {}, { signedIn }); p.view.setData({ doc: { id: 12, title: '通知标题', can_submit_deadline_clue: flag }, loading: false }); await p.view.syncClueEntry();
  assert.equal(p.text().includes('这份通知还没有核对过的截止日期。原文里写了的话，可以补一下 ›'), flag && signedIn);
  p.view.deadlineClue(); assert.equal(p.calls.urls.length, flag && signedIn ? 1 : 0);
  if (flag && signedIn) assert.equal(p.calls.urls[0], '/pages/deadline-clue/index?id=12&title=%E9%80%9A%E7%9F%A5%E6%A0%87%E9%A2%98');
});
test('我的数据列出线索原句和总数，只有待核对可删，能复制完整原始记录', async () => {
  const data = { user: { openid: 'synthetic' }, follows: [], subscriptions: [], feedback: [], retention: {}, deadlineClues: [clue, { ...clue, id: 2, status: '已采纳' }], deadlineCluesTotal: 2 };
  const p = page('my-data', { myData: () => Promise.resolve(data) }); p.view.onShow(); await flush(); assert.match(p.text(), /截止日期线索 2/); assert.match(p.text(), new RegExp(clue.quote)); assert.equal(findAll(p.show(), tapsTo('removeClue')).length, 1);
  p.view.copyOne({ currentTarget: { dataset: { kind: 'clue', id: 1 } } }); assert.deepEqual(p.calls.copied[0], clue);
  await p.view.removeClue(event(2)); assert.equal(p.calls.modals.length, 0); await p.view.removeClue(event(1)); assert.deepEqual(p.calls.deleted, [1]);
});
test('只有 invite 页带码转发，无朋友圈分享；首页不展示给引荐人看的字样', () => {
  const dirs = fs.readdirSync(path.join(ROOT, 'pages'));
  for (const name of dirs) {
    const js = read(`pages/${name}/index.js`); const wxml = read(`pages/${name}/index.wxml`);
    if (name !== 'invite') { assert.doesNotMatch(js, /\?inv=|inviteCode/); assert.doesNotMatch(wxml, /open-type=["']share["']/); }
  }
  assert.doesNotMatch(read('pages/invite/index.js'), /onShareTimeline/);
  const home = textOf(render('pages/home/index.wxml', { inv: 'abcdefgh1234', loading: false, loginOpen: false, items: [], groups: [], folded: 0 }));
  assert.doesNotMatch(home, /奖励|得 7 天|请同行来用/);
});
test('新页面注册、所有固定界面文字符合禁用词要求，既有日期性质例外', async () => {
  const config = JSON.parse(read('app.json'));
  const banned = /试用|订阅|自动续费|充值|余额|积分|返利|佣金|分销|裂变|AI|智能|匹配度|推荐/;
  for (const name of ['rewards', 'invite', 'deadline-clue']) {
    assert.ok(config.pages.includes(`pages/${name}/index`));
    for (const ext of ['wxml', 'json', 'js']) assert.doesNotMatch(read(`pages/${name}/index.${ext}`).replaceAll('推荐或审核截止', ''), banned);
    const p = page(name); await p.view.onShow(); assert.doesNotMatch(p.text().replaceAll('推荐或审核截止', ''), banned);
    assert.doesNotMatch(read(`pages/${name}/index.wxss`), /animation|@keyframes/);
  }
});
