// 我的数据·工具记录（D-093 上线门槛第 8 条）：本人的批量工具和 AI 记录直接列在"我的数据"里，能看（复制单条是
// 完整记录）、能逐条删（结果文件一起删），功能关着、协议升了版本也一样；只有正在处理的要等结束。没有记录时整栏不显示。
// 过审版（首发 16 页）没有"我的记录"页，不提去那里下载；没有工具中心时，保存期限里不写批量工具、加量体验和按天次数。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const conditions = require('../utils/conditions.js');
const advisor = require('../utils/advisor.js');
const { render, textOf, findAll, hasClass, tapsTo } = require('./wxml-render.js');
const { FIRST_PAGES } = require('../../../tools/miniapp_presubmit_check.js');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (v) => JSON.parse(JSON.stringify(v));
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
const event = (dataset) => ({ currentTarget: { dataset } });
const APP_PAGES = JSON.parse(read('app.json')).pages;

function load(file, deps = {}, globals = {}) {
  let def;
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, setTimeout, clearTimeout, Date, Promise,
    require(name) { assert.ok(Object.hasOwn(deps, name), `${file} 缺少依赖 ${name}`); return deps[name]; },
    Page(d) { def = d; }, ...globals,
  }, { filename: file });
  if (!def) return module.exports;
  return { ...def, data: plain(def.data), setData(values, callback) { Object.assign(this.data, plain(values)); if (callback) callback(); } };
}
const pagesOf = (list) => load('utils/pages.js', { './package-pages.js': list });

const RETENTION = { feedback: 180, events: 90, usedSubscriptions: 30, unusedSubscriptions: 365, toolInput: 7, toolTasks: 30 };
const TASKS = [
  { id: 41, kind: 'compare', status: 'done', rowCount: 12, createdAt: '2026-10-20T09:05:00+08:00', startedAt: '2026-10-20T09:05:10+08:00', finishedAt: '2026-10-20T09:06:00+08:00' },
  { id: 42, kind: 'interpret', status: 'done', rowCount: 0, createdAt: '2026-10-21T10:00:00+08:00', finishedAt: '2026-10-21T10:01:00+08:00', result: { summary: '合成' } },
  // 正在处理：两小时前开始（按真实时钟算，处理中不满一天的要等结束）
  { id: 43, kind: 'track', status: 'running', rowCount: 3, createdAt: '2026-10-22T11:00:00+08:00', startedAt: new Date(Date.now() - 2 * 3600e3).toISOString() },
  { id: 44, kind: 'compare', status: 'queued', rowCount: 5, createdAt: '2026-10-22T11:30:00+08:00' },
];

function myData({ tasks = [], pages = APP_PAGES, extra = {}, remove, chatApi = {}, confirm = true, loadData } = {}) {
  const raw = { user: { openid: 'synthetic-openid', agreed: null }, follows: [], subscriptions: [], feedback: [], advisorTasks: plain(tasks),
    advisorTasksTotal: tasks.length, retention: RETENTION, ...extra };
  const calls = { load: 0, deleted: [], scrolls: [] }; const copied = []; const toasts = []; let modal;
  const api = {
    ...chatApi,
    myData: () => { calls.load += 1; return loadData ? loadData() : Promise.resolve(plain(raw)); },
    deleteAdvisorTask: (id) => { calls.deleted.push(id); return remove ? remove(id) : Promise.resolve(); },
  };
  const page = load('pages/my-data/index.js', {
    '../../utils/api.js': api, '../../utils/conditions.js': conditions,
    '../../utils/consent.js': { loginOpen: () => true, remindAny: () => false, withdraw: () => Promise.resolve(false) },
    '../../utils/advisor.js': advisor, '../../utils/pages.js': pagesOf(pages),
  }, { wx: { pageScrollTo: (options) => calls.scrolls.push(plain(options)), setClipboardData: ({ data, success }) => { copied.push(JSON.parse(data)); if (success) success(); }, showToast: (o) => toasts.push(o.title),
    showModal: (m) => { modal = m; m.success({ confirm }); }, navigateTo() { assert.fail('我的数据里删记录不跳到别的页面'); } } });
  return { page, raw, calls, copied, toasts, modal: () => modal, view: () => render('pages/my-data/index.wxml', page.data) };
}
const recordsCard = (root) => findAll(root, hasClass('card')).find((card) => /工具(和 AI )?记录/.test(textOf(findAll(card, hasClass('section-title'))[0] || { text: '' })));

test('没有工具记录：不显示这一栏；页面上没有"管理工具任务"、去"我的记录"的按钮', async () => {
  const h = myData();
  h.page.load(); await flush();
  const root = h.view();
  assert.equal(recordsCard(root), undefined);
  assert.equal(findAll(root, tapsTo('tasks')).length, 0);
  const text = textOf(root);
  assert.doesNotMatch(text, /管理工具任务|删除单条记录及其结果，请打开/);
  assert.doesNotMatch(read('pages/my-data/index.js'), /advisor-tasks\/index'\s*\}\)/);
});

test('有记录：逐条写类型、几家、状态和时间；AI 的叫法从 JS 来（模板里不写 AI）；正在处理的不给删除', async () => {
  const h = myData({ tasks: TASKS });
  h.page.load(); await flush();
  assert.equal(h.page.data.tasksTitle, '工具和 AI 记录');
  const card = recordsCard(h.view());
  assert.ok(card, '没有工具记录这一栏');
  assert.match(textOf(findAll(card, hasClass('section-title'))[0]), /工具和 AI 记录 4/);
  const items = findAll(card, hasClass('item'));
  assert.equal(items.length, 4);
  const main = items.map((n) => textOf(findAll(n, hasClass('item-main'))[0]).trim());
  assert.deepEqual(main, ['批量名单比对（12 家）', 'AI 解读', '客户相关通知表（3 家）', '批量名单比对（5 家）']);
  assert.match(textOf(items[0]), /已完成 · 提交于 2026-10-20 09:05 · 结束于 2026-10-20 09:06/);
  assert.match(textOf(items[3]), /排队中 · 提交于 2026-10-22 11:30/);
  // 正在处理的：没有删除，写等结束
  assert.equal(findAll(items[2], tapsTo('remove')).length, 0);
  assert.match(textOf(items[2]), /正在处理，结束后可以删除/);
  for (const i of [0, 1, 3]) assert.equal(findAll(items[i], tapsTo('remove')).length, 1);
  // 只有批量工具记录时叫"工具记录"
  const tools = myData({ tasks: [TASKS[0]] });
  tools.page.load(); await flush();
  assert.equal(tools.page.data.tasksTitle, '工具记录');
  // 模板里显示的字不写 AI（注释除外，同 ai-entry.test.js）
  assert.doesNotMatch(read('pages/my-data/index.wxml').replace(/<!--[\s\S]*?-->/g, ''), /\bAI\b/);
});

test('复制单条是这条的完整记录；删除走本人删除接口，删完重新加载；排队中的写明先取消、次数退回', async () => {
  const h = myData({ tasks: TASKS });
  h.page.load(); await flush();
  h.page.copyOne(event({ kind: 'task', id: 42 }));
  assert.deepEqual(h.copied.at(-1), TASKS[1]);
  h.page.remove(event({ kind: 'task', id: 41 })); await flush();
  assert.deepEqual(h.calls.deleted, [41]);
  assert.equal(h.modal().title, '删除记录');
  assert.match(h.modal().content, /删除这条记录和它的结果？删除后不能恢复/);
  assert.equal(h.calls.load, 2);
  assert.ok(h.toasts.includes('已删除'));
  h.page.remove(event({ kind: 'task', id: 44 })); await flush();
  assert.deepEqual(h.calls.deleted, [41, 44]);
  assert.match(h.modal().content, /排队中的记录（会先取消，用掉的次数退回）/);
});

test('删不成（例如刚开始处理，服务端说暂时不能删）：照实提示，列表不动', async () => {
  const busy = Object.assign(new Error('任务正在处理，暂时不能删除'), { status: 409 });
  const h = myData({ tasks: TASKS, remove: () => Promise.reject(busy) });
  h.page.load(); await flush();
  h.page.remove(event({ kind: 'task', id: 44 })); await flush();
  assert.deepEqual(h.toasts, ['任务正在处理，暂时不能删除']);
  assert.equal(h.calls.load, 1);
  assert.equal(h.page.data.tasks.length, 4);
});

test('结果文件：完整版提示去"我的记录"下载；过审版没有这一页，不提', async () => {
  for (const [pages, note] of [[APP_PAGES, '批量工具的结果文件在“我的”→“我的记录”里下载'], [FIRST_PAGES, '']]) {
    const h = myData({ tasks: TASKS, pages });
    h.page.load(); await flush();
    assert.equal(h.page.data.fileNote, note);
    assert.equal(textOf(h.view()).includes('我的记录'), Boolean(note));
  }
  // 没有完成的批量工具记录（只有 AI 的）时也不提
  const ai = myData({ tasks: [TASKS[1]] });
  ai.page.load(); await flush();
  assert.equal(ai.page.data.fileNote, '');
});

test('保存期限：过审版、没有工具记录时不写批量工具、加量体验和按天次数；完整版或有过记录时照写', async () => {
  const lines = (h) => textOf(findAll(h.view(), hasClass('plain')).at(-1));
  const first = myData({ pages: FIRST_PAGES });
  first.page.load(); await flush();
  const text = lines(first);
  for (const word of ['批量工具：', '加量体验记录', '按天的使用次数']) assert.ok(!text.includes(word), `过审版还写着：${word}`);
  assert.match(text, /纠错：提交 180 天后删除/);
  assert.match(text, /访问日志：保存 190 天/);
  for (const options of [{ pages: APP_PAGES }, { pages: FIRST_PAGES, tasks: [TASKS[0]] }]) {
    const h = myData(options);
    h.page.load(); await flush();
    const all = lines(h);
    assert.match(all, /批量工具：任务里解析出的名单 7 天后清空，任务记录和结果文件（含提交的名单内容）30 天后删除；可以在本页提前删除/);
    assert.match(all, /加量体验记录：开始和结束时间保存到你注销/);
    assert.match(all, /按天的使用次数：保留本月和上个月的/);
  }
});

test('加量体验、送的批量工具天数：有的时候写一行，说明在"复制个人数据概览"里；没有就不显示', async () => {
  const h = myData({ pages: FIRST_PAGES, extra: { boostTrial: { startedAt: '2026-10-01T00:00:00+08:00', endsAt: '2026-10-04T00:00:00+08:00' }, passGiftsTotal: 2, passGifts: [] } });
  h.page.load(); await flush();
  const card = findAll(h.view(), hasClass('card')).find((c) => /其他记录/.test(textOf(c)));
  assert.ok(card);
  assert.match(textOf(card), /加量体验的开始和结束时间、送的批量工具天数（2 笔）：在“复制个人数据概览”里可以看到。/);
  // 有加量体验记录时，保存期限里写它（即使是过审版）
  assert.match(textOf(findAll(h.view(), hasClass('plain')).at(-1)), /加量体验记录：开始和结束时间保存到你注销/);
  const none = myData();
  none.page.load(); await flush();
  assert.equal(none.page.data.extras, '');
  assert.ok(!findAll(none.view(), hasClass('card')).some((c) => /其他记录/.test(textOf(c))));
});

test('处理中超过一天的（没人在跑了、功能关掉后也不会有人接着处理）：给删除，确认框写次数退回', async () => {
  const stuckTask = { id: 45, kind: 'compare', status: 'running', rowCount: 4, createdAt: '2026-10-20T09:00:00+08:00', startedAt: new Date(Date.now() - 2 * 86400e3).toISOString() };
  const h = myData({ tasks: [stuckTask, TASKS[2]] });
  h.page.load(); await flush();
  const items = findAll(recordsCard(h.view()), hasClass('item'));
  assert.equal(findAll(items[0], tapsTo('remove')).length, 1, '超过一天的应能删');
  assert.equal(findAll(items[1], tapsTo('remove')).length, 0, '两小时前开始的要等结束');
  h.page.remove(event({ kind: 'task', id: 45 })); await flush();
  assert.deepEqual(h.calls.deleted, [45]);
  assert.match(h.modal().content, /这条没有处理完的记录（用掉的次数退回）/);
});

const CHAT = { id: 71, hasDoc: true, docTitle: '合成通知', turns: 230, lastAt: '2026-10-30T09:00:00+08:00' };
const CHAT_PAGE = { id: 71, turns: [{ seq: 230, skill: 'ask', question: '合成问题', status: 'running', counted: false, createdAt: CHAT.lastAt }], total: 230, range: { from: 230, to: 230 }, hasMore: true };

test('关闭生成入口仍能翻全部对话目录与问题页，复制明确范围，处理中也能整条删除', async () => {
  const calls = [];
  const h = myData({ pages: FIRST_PAGES,
    extra: { aiChats: [CHAT], aiChatsNext: 'cursor-71', aiChatsTotal: 2, aiChatTurnsTotal: 231, retention: { ...RETENTION, aiChats: 7 } },
    chatApi: {
      myDataChats: async (after) => { calls.push(['list', after]); return { items: [{ ...CHAT, id: 72, hasDoc: false, lastQuestion: '找合成通知', turns: 1 }], next: null }; },
      myDataChat: async (id, before) => { calls.push(['view', id, before]); return before ? { ...CHAT_PAGE, turns: [{ ...CHAT_PAGE.turns[0], seq: 1 }], range: { from: 1, to: 1 }, hasMore: false } : CHAT_PAGE; },
      deleteChat: async (id) => { calls.push(['delete', id]); },
    } });
  h.page.load(); await flush();
  assert.match(textOf(h.view()), /AI 对话记录 2 条 · 231 问/);
  assert.match(textOf(h.view()), /每一问保存 7 天/);
  h.page.moreChats(); await flush();
  assert.equal(h.page.data.chats.length, 2);
  assert.equal(h.page.data.chatsNext, null);
  h.page.viewChat(event({ id: 71 })); await flush();
  assert.deepEqual(h.calls.scrolls, [{ selector: '#chat-detail', duration: 200 }]);
  assert.match(read('pages/my-data/index.wxml'), /id="chat-detail"/);
  assert.match(textOf(h.view()), /第 230–230 问，共 230 问/);
  assert.match(textOf(h.view()), /合成问题/);
  h.page.copyChatPage(); assert.deepEqual(h.copied[0], CHAT_PAGE);
  h.page.earlierChat(); await flush();
  assert.deepEqual(calls[2], ['view', 71, 230]);
  assert.equal(h.page.data.chatDetail.hasMore, false);
  assert.equal(h.calls.scrolls.length, 2);
  h.page.remove(event({ kind: 'chat', id: 71 })); await flush();
  assert.deepEqual(calls[3], ['delete', 71]);
  assert.match(h.modal().content, /全部问题和结果.*已完成的问答次数不退回/);
  assert.equal(h.calls.load, 2);
  assert.equal(h.page.data.chatDetail, null);
});

test('对话翻页失败保留可重试位置；取消删除不调用接口；没有对话不出现栏目', async () => {
  const empty = myData(); empty.page.load(); await flush();
  assert.doesNotMatch(textOf(empty.view()), /AI 对话记录/);
  const h = myData({ confirm: false, extra: { aiChats: [CHAT], aiChatsNext: 'cursor' }, chatApi: {
    myDataChats: async () => { throw new Error('网络失败'); },
    deleteChat: async () => assert.fail('取消不应删除'),
  } });
  h.page.load(); await flush(); h.page.moreChats(); await flush();
  assert.equal(h.page.data.chatsNext, 'cursor');
  assert.equal(h.page.data.chatsLoading, false);
  assert.deepEqual(h.toasts, ['网络失败']);
  h.page.remove(event({ kind: 'chat', id: 71 })); await flush();
  assert.equal(h.calls.load, 1);
});

test('切换对话或刷新后迟到的问题响应不覆盖当前数据；查看问题不丢失正在翻的目录页', async () => {
  let finishList; let finishOld;
  const h = myData({ extra: { aiChats: [CHAT, { ...CHAT, id: 72 }], aiChatsNext: 'cursor' }, chatApi: {
    myDataChats: () => new Promise((resolve) => { finishList = resolve; }),
    myDataChat: (id) => id === 71 ? new Promise((resolve) => { finishOld = resolve; }) : Promise.resolve({ ...CHAT_PAGE, id }),
  } });
  h.page.load(); await flush(); h.page.moreChats();
  h.page.viewChat(event({ id: 71 })); h.page.viewChat(event({ id: 72 })); await flush();
  finishOld(CHAT_PAGE); finishList({ items: [{ ...CHAT, id: 73 }], next: null }); await flush();
  assert.equal(h.page.data.chatDetail.id, 72);
  assert.equal(h.calls.scrolls.length, 1);
  assert.equal(h.page.data.chats.length, 3);
  assert.equal(h.page.data.chatsLoading, false);
  h.page.viewChat(event({ id: 71 })); h.page.load(); await flush();
  finishOld(CHAT_PAGE); await flush();
  assert.equal(h.page.data.chatDetail, null);
  assert.equal(h.calls.scrolls.length, 1);
});

test('真实 API 封装使用后端挂载路径：本人数据分页与整条对话删除', async () => {
  const sent = [];
  const api = load('utils/api.js', {
    './config.js': { devBaseUrl: 'http://localhost' }, '../mock/data.js': {},
    './mockSearch.js': {}, './format.js': {},
  }, { wx: { request(o) { sent.push([o.method, o.url]); o.success({ statusCode: o.method === 'DELETE' ? 204 : 200, data: {} }); } } });
  await api.myDataChats('123-7'); await api.myDataChat(7, 21); await api.deleteChat(7);
  assert.deepEqual(sent, [
    ['GET', 'http://localhost/api/v1/me/data/chats?after=123-7'],
    ['GET', 'http://localhost/api/v1/me/data/chats/7?before=21'],
    ['DELETE', 'http://localhost/api/v1/advisor/chats/7'],
  ]);
});


test('重复加载逆序完成或卸载后回包，不复活删除前目录，不覆盖新成功状态', async () => {
  const pending = [];
  const h = myData({ loadData: () => new Promise((resolve, reject) => pending.push({ resolve, reject })) });
  h.page.load(); h.page.load();
  pending[1].resolve({ ...h.raw, aiChats: [], aiChatsTotal: 0 }); await flush();
  pending[0].resolve({ ...h.raw, aiChats: [CHAT], aiChatsTotal: 1 }); await flush();
  assert.equal(h.page.data.chats.length, 0); assert.equal(h.page.raw.aiChats.length, 0);
  h.page.load(); h.page.load(); pending[3].resolve(h.raw); await flush();
  pending[2].reject(new Error('旧请求错误')); await flush(); assert.equal(h.page.data.error, '');
  h.page.load(); h.page.onUnload(); const state = plain(h.page.data);
  pending[4].resolve({ ...h.raw, aiChats: [CHAT] }); await flush();
  assert.deepEqual(h.page.data, state);
});
