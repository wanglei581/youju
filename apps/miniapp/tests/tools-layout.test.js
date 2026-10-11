const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { render, textOf, findAll, tapsTo } = require('./wxml-render.js');

// 工具首页、填写客户、我的任务三页重排后（10/11）：每种状态下该有的操作都在，不该有的不出现。
const task = (over) => ({ id: 1, kind: 'compare', kindLabel: '批量名单比对', status: 'done', stateLabel: '已完成', rowCount: 5, time: '2026-10-10 14:48', hasFile: true, terminal: true, paid: false, ...over });
const tasksPage = (items, over = {}) => render('pages/advisor-tasks/index.wxml', { loginOpen: true, loading: false, error: '', actionError: '', items, busyId: null, singleTask: false, demo: false, aiEnabled: false, kinds: [], ...over });
const count = (tree, handler) => findAll(tree, tapsTo(handler)).length;

test('我的任务：已完成有打开、查名单、截止雷达、删除；排队中只有取消（已付费的没有）；失败只有删除', () => {
  const done = tasksPage([task()]);
  assert.deepEqual(['open', 'lists', 'radar', 'remove', 'cancel'].map((h) => count(done, h)), [1, 1, 1, 1, 0]);
  assert.match(textOf(done), /批量名单比对\s*已完成\s*5 行 · 2026-10-10 14:48/);
  const expired = tasksPage([task({ hasFile: false })]);
  assert.equal(count(expired, 'open'), 0); assert.match(textOf(expired), /结果文件已过期或已删除/);
  const queued = tasksPage([task({ status: 'queued', stateLabel: '排队中', terminal: false, hasFile: false })]);
  assert.deepEqual(['open', 'lists', 'radar', 'remove', 'cancel'].map((h) => count(queued, h)), [0, 0, 0, 0, 1]);
  assert.equal(count(tasksPage([task({ status: 'queued', terminal: false, hasFile: false, paid: true })]), 'cancel'), 0);
  const failed = tasksPage([task({ status: 'failed', stateLabel: '失败', hasFile: false, error: '名单读取失败' })]);
  assert.deepEqual(['open', 'lists', 'radar', 'remove', 'cancel'].map((h) => count(failed, h)), [0, 0, 0, 1, 0]);
  assert.match(textOf(failed), /名单读取失败/);
  // 操作进行中：按钮都不能再点
  const busy = tasksPage([task()], { busyId: 1 });
  for (const handler of ['open', 'remove']) assert.equal(findAll(busy, tapsTo(handler))[0].attrs.disabled, true);
  // 没有任务、只看一个任务
  assert.equal(count(tasksPage([]), 'form'), 1);
  assert.equal(count(tasksPage([task()], { singleTask: true }), 'allTasks'), 1);
});

test('工具首页：每个工具一个"填写客户"；最近任务每条能点开，另有"全部任务"；没开通时才有购买入口', () => {
  const skills = [{ kind: 'compare', title: '批量名单比对', description: '说明一', statusLine: '本月剩余 2 次' }, { kind: 'track', title: '客户相关通知表', description: '说明二', statusLine: '本月剩余 3 次' }];
  const data = { loginOpen: true, loading: false, error: '', enabled: true, skills, context: null, recent: [task(), task({ id: 2 })], recentLoading: false, recentError: '', showPassLink: true, passNotice: '', aiLoading: false, aiEnabled: false };
  const home = render('pages/advisor/index.wxml', data);
  assert.deepEqual(findAll(home, tapsTo('form')).map((n) => n.attrs['data-kind']), ['compare', 'track']);
  // 两条最近任务各一个，加上"全部任务"
  assert.equal(count(home, 'tasks'), 3); assert.equal(findAll(home, tapsTo('tasks')).filter((n) => n.attrs['data-id'] == null).length, 1);
  assert.equal(count(home, 'buy'), 1); assert.equal(count(render('pages/advisor/index.wxml', { ...data, showPassLink: false }), 'buy'), 0);
  assert.match(textOf(home), /本月剩余 2 次/);
  assert.match(textOf(render('pages/advisor/index.wxml', { ...data, recent: [] })), /还没有任务，填写客户后可查看进度和结果。/);
});

test('填写客户：没有可提交的行时提交按钮不能点；说明入口只有一个；超限提示里才有购买', () => {
  const data = { loginOpen: true, loading: false, error: '', kind: 'compare', title: '批量名单比对', statusLine: '本月剩余 2 次', mode: 'paste', text: '', fileName: '', rowCount: 0, previewing: false, submitting: false, agreed: true, inputError: '', limitHint: null };
  const empty = render('pages/advisor-form/index.wxml', data);
  assert.equal(findAll(empty, tapsTo('submit'))[0].attrs.disabled, true); assert.equal(count(empty, 'terms'), 1); assert.equal(count(empty, 'buy'), 0);
  assert.match(textOf(empty), /将提交 0 行/); assert.match(textOf(empty), /已单独同意《工具使用说明》/);
  const filled = render('pages/advisor-form/index.wxml', { ...data, text: '演示科技有限公司', rowCount: 1, agreed: false });
  assert.equal(findAll(filled, tapsTo('submit'))[0].attrs.disabled, false); assert.match(textOf(filled), /首次使用前请单独阅读并同意《工具使用说明》/);
  const file = render('pages/advisor-form/index.wxml', { ...data, mode: 'file', inputError: '文件读取失败' });
  assert.equal(count(file, 'choose'), 1); assert.equal(count(file, 'preview'), 1); assert.match(textOf(file), /还没有选择文件/);
  const over = render('pages/advisor-form/index.wxml', { ...data, text: '甲公司', rowCount: 80, limitHint: { message: '这份名单有 80 家', offer: '客户工作台 30 天', split: true } });
  assert.equal(count(over, 'buy'), 1); assert.match(textOf(over), /也可以把名单拆成 50 家以内，分次提交。/);
  // 超出免费范围：提交按钮不能点（点了本来也不会提交），原因在下面的提示里
  assert.equal(findAll(over, tapsTo('submit'))[0].attrs.disabled, true);
  // 名单在输入框里滚动，不随行数把页面撑长
  const input = findAll(filled, (n) => n.tag === 'textarea')[0];
  assert.ok(!('auto-height' in input.attrs)); assert.match(input.attrs.class, /\bform-list\b/);
  assert.match(fs.readFileSync(path.join(__dirname, '../pages/advisor-form/index.wxss'), 'utf8'), /\.form-list \{ height: 400rpx; \}/);
});

// 页面逻辑（10/11 Codex 审查后改的两处）
function loadPage(name, deps) {
  let definition;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'pages', name, 'index.js'), 'utf8'), {
    require: (key) => { for (const [suffix, value] of Object.entries(deps)) if (key.endsWith(suffix)) return value; throw new Error(`缺少依赖 ${key}`); },
    Page: (value) => { definition = value; }, wx: { setNavigationBarTitle() {}, navigateTo() {}, redirectTo() {} },
    setTimeout: () => 1, clearTimeout() {}, Date, Math, Promise, JSON,
  });
  definition.data = JSON.parse(JSON.stringify(definition.data));
  definition.setData = function setData(next) { Object.assign(this.data, next); };
  return definition;
}
const settle = async () => { for (let n = 0; n < 12; n += 1) await Promise.resolve(); };

test('填写客户：再点一次已选中的填法不清空名单；换一种填法才清空', () => {
  const advisor = require('../utils/advisor.js');
  const page = loadPage('advisor-form', { 'consent.js': { loginOpen: () => true }, 'api.js': {}, 'advisor.js': advisor, 'pay.js': require('../utils/pay.js') });
  page.input({ detail: { value: '甲公司\n乙公司' } });
  assert.equal(page.data.rowCount, 2);
  page.mode({ currentTarget: { dataset: { mode: 'paste' } } });
  assert.equal(page.data.text, '甲公司\n乙公司'); assert.equal(page.data.rowCount, 2);
  page.mode({ currentTarget: { dataset: { mode: 'file' } } });
  assert.equal(page.data.mode, 'file'); assert.equal(page.data.text, ''); assert.equal(page.data.rowCount, 0);
});

test('我的任务：重新加载失败时不留着上一次的任务和操作，只显示原因和重试', async () => {
  let fail = false;
  const api = { isLive: () => true, advisorCapabilities: () => Promise.resolve({}),
    advisorTasks: () => fail ? Promise.reject(Object.assign(new Error('请先阅读并同意用户协议和隐私保护指引'), { status: 403 })) : Promise.resolve({ items: [{ id: 1, kind: 'compare', status: 'done', rowCount: 5, hasFile: true }] }) };
  const page = loadPage('advisor-tasks', { 'consent.js': { loginOpen: () => true }, 'api.js': api, 'advisor.js': require('../utils/advisor.js') });
  page.onLoad({}); page.onShow(); await settle();
  assert.equal(page.data.items.length, 1);
  fail = true; page.load(); await settle();
  assert.equal(page.data.error, '请先阅读并同意用户协议和隐私保护指引'); assert.equal(page.data.items.length, 0);
  const tree = render('pages/advisor-tasks/index.wxml', page.data);
  assert.deepEqual(['open', 'remove', 'lists', 'radar', 'form'].map((h) => count(tree, h)), [0, 0, 0, 0, 0]); assert.equal(count(tree, 'load'), 1);
});
