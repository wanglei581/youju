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
const { FIRST_PAGES, FIRST_AI_PAGES } = require('../../../tools/miniapp_presubmit_check.js');
// 过审版加 AI（D-098，22 页）：首发 16 页加 AI 首页、解读、对照、我的记录、投诉举报、使用帮助，没有工具中心
const FIRST_AI = FIRST_PAGES.concat(FIRST_AI_PAGES);

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
  return { ...def, data: plain(def.data), setData(values) { Object.assign(this.data, plain(values)); } };
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

function myData({ tasks = [], pages = APP_PAGES, extra = {}, remove, withdrawn = [] } = {}) {
  const raw = { user: { openid: 'synthetic-openid', agreed: null }, follows: [], subscriptions: [], feedback: [], advisorTasks: plain(tasks),
    advisorTasksTotal: tasks.length, retention: RETENTION, ...extra };
  const calls = { load: 0, deleted: [] }; const copied = []; const toasts = []; let modal;
  const api = {
    myData: () => { calls.load += 1; return Promise.resolve(plain(raw)); },
    deleteAdvisorTask: (id) => { calls.deleted.push(id); return remove ? remove(id) : Promise.resolve(); },
  };
  const page = load('pages/my-data/index.js', {
    '../../utils/api.js': api, '../../utils/conditions.js': conditions,
    '../../utils/consent.js': { loginOpen: () => true, remindAny: () => false, withdrawAccount: (has) => { withdrawn.push(has); return Promise.resolve(false); } },
    '../../utils/advisor.js': advisor, '../../utils/pages.js': pagesOf(pages), '../../utils/exports.js': { removeAdvisorTask: (id) => { calls.removedExport = id; } }, '../../utils/exporter.js': { remove: () => true },
  }, { wx: { setClipboardData: ({ data, success }) => { copied.push(JSON.parse(data)); if (success) success(); }, showToast: (o) => toasts.push(o.title),
    showModal: (m) => { modal = m; m.success({ confirm: true }); }, navigateTo() { assert.fail('我的数据里删记录不跳到别的页面'); } } });
  return { page, raw, calls, copied, toasts, modal: () => modal, view: () => render('pages/my-data/index.wxml', page.data) };
}
const recordsCard = (root) => findAll(root, hasClass('card')).find((card) => /(工具(和 AI )?|^AI )记录/.test(textOf(findAll(card, hasClass('section-title'))[0] || { text: '' }).trim()));

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
  assert.deepEqual(main, ['历史批量工具（12 家）', 'AI 解读', '客户相关通知表（3 家）', '历史批量工具（5 家）']);
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
  assert.equal(h.calls.removedExport, 41);
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
  for (const [pages, note] of [[APP_PAGES, '批量工具的结果在“我的”→“我的记录”→“站内查看结果”里另存 Excel'], [FIRST_PAGES, '']]) {
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

test('加量体验、送的批量工具天数：有的时候写一行，说明在"复制全部数据"里；没有就不显示', async () => {
  const h = myData({ pages: FIRST_PAGES, extra: { boostTrial: { startedAt: '2026-10-01T00:00:00+08:00', endsAt: '2026-10-04T00:00:00+08:00' }, passGiftsTotal: 2, passGifts: [] } });
  h.page.load(); await flush();
  const card = findAll(h.view(), hasClass('card')).find((c) => /其他记录/.test(textOf(c)));
  assert.ok(card);
  assert.match(textOf(card), /加量体验的开始和结束时间、送的批量工具天数（2 笔）：在“复制全部数据”里可以看到。/);
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

test('过审版加 AI（11/2 完整测试第 7 条）：只有 AI 记录时栏目叫"AI 记录"，保存期限不写批量工具和加量体验；按天次数照写', async () => {
  const lines = (h) => textOf(findAll(h.view(), hasClass('plain')).at(-1));
  const h = myData({ pages: FIRST_AI, tasks: [TASKS[1]] });
  h.page.load(); await flush();
  assert.equal(h.page.data.tasksTitle, 'AI 记录');
  assert.match(textOf(findAll(recordsCard(h.view()), hasClass('section-title'))[0]), /AI 记录 1/);
  const text = lines(h);
  for (const word of ['批量工具', '加量体验']) assert.ok(!text.includes(word), `过审版加 AI 还写着：${word}`);
  assert.match(text, /AI 解读和条款对照：任务输入 7 天后删除，任务和结果 30 天后删除/);
  assert.match(text, /按天的使用次数：保留本月和上个月的/);
  assert.ok(!textOf(h.view()).includes('工具和 AI 记录'));
  // 没有任何记录：AI、批量工具、按天次数都不写
  const none = myData({ pages: FIRST_AI });
  none.page.load(); await flush();
  const empty = lines(none);
  for (const word of ['批量工具', '加量体验', '按天的使用次数', 'AI 解读和条款对照']) assert.ok(!empty.includes(word), `没有记录还写着：${word}`);
  // 这个账号真有批量工具记录（在完整版里用过）：照实写批量工具，栏目叫"工具和 AI 记录"
  const mixed = myData({ pages: FIRST_AI, tasks: [TASKS[0], TASKS[1]] });
  mixed.page.load(); await flush();
  assert.equal(mixed.page.data.tasksTitle, '工具和 AI 记录');
  assert.match(lines(mixed), /批量工具：任务里解析出的名单 7 天后清空/);
  // 完整版只有 AI 记录：照旧叫"工具和 AI 记录"、写批量工具和加量体验
  const full = myData({ tasks: [TASKS[1]] });
  full.page.load(); await flush();
  assert.equal(full.page.data.tasksTitle, '工具和 AI 记录');
  assert.match(lines(full), /批量工具：/);
  assert.match(lines(full), /加量体验记录：/);
});

test('注销按钮把包里有哪些功能交给注销弹窗（11/2 完整测试第 7 条）', async () => {
  for (const [pages, flags] of [[FIRST_AI, { advisor: false, ai: true }], [APP_PAGES, { advisor: true, ai: true }], [FIRST_PAGES, { advisor: false, ai: false }]]) {
    const withdrawn = [];
    const h = myData({ pages, withdrawn });
    h.page.deleteAll(); await flush();
    assert.equal(withdrawn.length, 1);
    assert.equal(withdrawn[0].advisor, flags.advisor);
    assert.equal(withdrawn[0].ai, flags.ai);
  }
});

test('注销弹窗按包里的功能写删哪些记录：完整版写工具，过审版加 AI 只写 AI，过审版都不写；没传照完整版', async () => {
  const content = async (has) => {
    let modal;
    const deps = {
      '../content/legal.js': { final: true, version: '2026-11.1' }, './config.js': { templates: {} },
      './api.js': { isLive: () => true, AGREED_KEY: 'agreed', forgetAgreed() {}, deleteMe: () => Promise.resolve({}) },
      './exports.js': { clear() {} }, './exporter.js': { removeFiles() {} }, './ai-access.js': { clear() {}, refresh() {}, recheck() {} },
    };
    const consent = load('utils/consent.js', deps, { wx: {
      showModal: (m) => { modal = m; m.success({ confirm: false }); }, showToast() {},
      getStorageSync: () => '', setStorageSync() {}, removeStorageSync() {}, clearStorageSync() {},
    } });
    assert.equal(await consent.withdraw(has), false);
    return modal.content;
  };
  const ai = await content({ advisor: false, ai: true });
  assert.match(ai, /纠错记录、AI 的输入、任务与结果，以及本机的导出记录和文件/);
  assert.ok(!ai.includes('工具'));
  assert.match(await content({ advisor: true, ai: true }), /纠错记录、批量工具和 AI 的输入、任务与结果、加量体验记录，以及本机/);
  const first = await content({ advisor: false, ai: false });
  assert.match(first, /纠错记录，以及本机的导出记录和文件/);
  assert.ok(!/工具|AI/.test(first));
  assert.match(await content(undefined), /纠错记录、批量工具和 AI 的输入、任务与结果、加量体验记录/);
});
