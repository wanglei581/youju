const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { render, textOf, findAll } = require('./wxml-render.js');
const root = path.join(__dirname, '..');
const plain = x => JSON.parse(JSON.stringify(x));
function load(name, api, extra = {}) {
  let page; const navigations = [];
  vm.runInNewContext(fs.readFileSync(path.join(root, `pages/${name}/index.js`), 'utf8'), {
    Page: def => { page = def; }, Promise,
    require: dep => {
      if (dep === '../../utils/api.js') return api;
      if (dep === '../../utils/exporter.js') return extra.exporter || {};
      if (dep === '../../utils/data-tools.js') return extra.tools || {};
      if (extra.deps && Object.hasOwn(extra.deps, dep)) return extra.deps[dep];
      throw new Error(dep);
    }, wx: { navigateTo: o => navigations.push(o.url) },
  });
  return { ...page, data: plain(page.data), navigations, setData(values) { Object.assign(this.data, plain(values)); } };
}
const segment = (n, text) => ({ n, kind: 'p', text });
const reading = (patch = {}) => ({ status: 'ok', title: '合成政策', source: { id: '7', title: '合成政策', issuer: '合成部门', host: 'example.gov.cn', date: '2026-10-23' }, revision: 'rev1', segments: [segment(1, '首段')], nextCursor: null, totalSegments: 1, ...patch });
const result = (patch = {}) => ({ kind: 'catalogue', title: '本次所选文件目录', revision: 'r1', notice: '合成样例', generatedAt: '2026-10-23', total: 1, items: [{ number: 1, doc_id: '7', title: '合成政策' }], ...patch });

test('正文多页绑定同一版本，前后翻页不漏段或把两版合并', async () => {
  const calls = [];
  const p = load('reader', { reading: async (id, aid, args) => {
    calls.push([id, aid, plain(args)]);
    return args.cursor === 'c2' ? reading({ segments: [segment(2, '末段')] }) : reading({ nextCursor: 'c2' });
  } });
  p.onLoad({ id: '7' }); await p.onShow(); // onShow starts asynchronously
  await new Promise(r => setImmediate(r));
  assert.equal(p.data.segments[0].text, '首段'); assert.equal(p.data.complete, false);
  await p.next(); assert.equal(p.data.page, 2); assert.equal(p.data.segments[0].text, '末段'); assert.equal(p.data.complete, true);
  assert.deepEqual(calls[1], ['7', '', { cursor: 'c2', revision: 'rev1' }]);
  await p.previous(); assert.equal(p.data.segments.length, 1); assert.equal(p.data.segments[0].text, '首段');
});

test('内容更新409清空旧版本，重新读不会沿用旧游标', async () => {
  let updated = false;
  const p = load('reader', { reading: async (_id, _att, args) => {
    if (args.cursor) throw Object.assign(new Error('changed'), { status: 409 });
    return reading({ revision: updated ? 'rev2' : 'rev1', nextCursor: 'c2' });
  } });
  p.onLoad({ id: '7' }); p.visible = true; await p.restart(); await p.next();
  assert.equal(p.data.changed, true); assert.deepEqual(p.data.segments, []); assert.equal(p.data.revision, '');
  updated = true; await p.retry(); assert.equal(p.data.revision, 'rev2'); assert.equal(p.data.page, 1);
});

test('离页后晚到响应不能显示，返回重新验证，撤回/不可读200清旧正文', async () => {
  let resolve; let response = () => new Promise(r => { resolve = r; });
  const p = load('reader', { reading: (...args) => response(...args) });
  p.onLoad({ id: '7' }); p.visible = true; const pending = p.restart(); p.onHide(); resolve(reading()); await pending;
  assert.deepEqual(p.data.segments, []);
  response = async () => reading(); p.visible = true; await p.restart(); assert.equal(p.data.segments.length, 1);
  p.onHide(); response = async () => reading({ status: 'unavailable', revision: null, segments: [] }); p.onShow(); await new Promise(r => setImmediate(r));
  assert.equal(p.data.status, 'unavailable'); assert.deepEqual(p.data.segments, []); assert.equal(p.data.complete, false);
});

test('部分扫描附件在站内标明缺失，真实位置和表头保留，不把序号推断为页码', async () => {
  const p = load('reader', { reading: async () => reading({ status: 'partial', scannedPages: 1, segments: [{ n: 9, kind: 'row', cells: [{ c: 0, t: '12', h: '序号' }, { c: 1, t: '合成企业', h: '单位名称' }], loc: { sheet: '名单', row: 3 } }] }) });
  p.onLoad({ id: '7', attachment: '8' }); p.visible = true; await p.restart();
  assert.equal(p.data.complete, false); assert.equal(p.data.segments[0].cells[0].h, '序号');
  assert.equal(p.data.segments[0].loc, '工作表 名单 · 行 3');
  const text = textOf(render('pages/reader/index.wxml', p.data));
  assert.match(text, /单位名称：合成企业/); assert.doesNotMatch(text, /rev1/); assert.match(text, /扫描页/); assert.doesNotMatch(text, /已到本版本内容末尾|浏览器|复制/);
});

test('目录在站内显示，保存绑定预览revision；409后不能保存旧结果', async () => {
  let saves = [];
  const p = load('results', { exportPreview: async ids => { assert.deepEqual(plain(ids), ['7']); return result(); } }, { exporter: { exportDocs: async (ids, opts) => { saves.push([plain(ids), opts.revision]); opts.onStale(); return false; } } });
  p.onLoad({ ids: '7', current: '1' }); await p.onShow();
  assert.match(textOf(render('pages/results/index.wxml', p.data)), /按当前数据重新查看/);
  await p.save(); assert.deepEqual(saves, [[['7'], 'r1']]); assert.equal(p.data.result, null); assert.equal(p.data.changed, true);
  p.save(); assert.equal(saves.length, 1);
});

test('advisor原生结果分页保留权限错误，旧任务不能伪造为有可读结果', async () => {
  let calls = []; let expired = false;
  const p = load('results', { advisorReading: async (id, args) => {
    calls.push([id, plain(args)]);
    if (expired) throw Object.assign(new Error('结果已过期'), { status: 410 });
    return result({ kind: 'compare', items: [{ n: 1, title: '合成企业', documentId: '7', fields: [{ label: '名单序号', value: '12' }] }], nextCursor: args.cursor ? null : '20' });
  } });
  p.onLoad({ mode: 'advisor', taskId: '8' }); await p.onShow();
  assert.match(textOf(render('pages/results/index.wxml', p.data)), /名单序号：12/);
  await p.next(); assert.deepEqual(calls[1], ['8', { cursor: '20', revision: 'r1' }]);
  p.onHide(); expired = true; await p.onShow(); assert.equal(p.data.result, null); assert.equal(p.data.error, '结果已过期');
  const old = load('results', { advisorReading: async () => { throw Object.assign(new Error('旧结果未生成站内阅读内容，请重新生成'), { status: 409 }); } });
  old.onLoad({ mode: 'advisor', taskId: '1' }); await old.onShow(); assert.equal(old.data.result, null); assert.match(old.data.error, /重新生成/);
});

test('结果离页后的迟回不能泄露旧任务数据，新查询不会被慢请求覆盖', async () => {
  const resolves = [];
  const p = load('results', { exportPreview: () => new Promise(r => resolves.push(r)) });
  p.onLoad({ ids: '7' }); const first = p.onShow(); const second = p.restart();
  resolves[1](result({ title: '新请求' })); await second; resolves[0](result({ title: '旧请求' })); await first;
  assert.equal(p.data.result.title, '新请求');
  const pending = p.load(); p.onHide(); resolves[2](result()); await pending; assert.equal(p.data.result, null);
});

test('真实API接线：正文/附件版本游标、advisor身份通道、目录预览及保存revision', async () => {
  const calls = []; let exportStatus = 200; const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'utils/api.js'), 'utf8'), {
    module, Promise, setTimeout, clearTimeout, Date,
    require(name) {
      if (name === './config.js') return { cloudEnv: 'test', cloudService: 'api', templates: {} };
      if (name === '../mock/data.js') return {};
      if (name === './mockSearch.js') return {};
      if (name === './format.js') return require('../utils/format.js');
      throw new Error(name);
    },
    wx: { cloud: { callContainer(o) {
      calls.push(o);
      o.success({ statusCode: o.path === '/api/v1/exports' ? exportStatus : 200, data: o.responseType === 'arraybuffer' ? new ArrayBuffer(1) : {} });
    } } },
  });
  const api = module.exports;
  await api.reading('7', '', { cursor: '1', revision: 'r1' });
  await api.reading('7', '8', { cursor: '2', revision: 'r2' });
  await api.advisorReading('9', { cursor: '20', revision: 'r3' });
  await api.exportPreview(['7']); await api.exportFile(['7'], 'xlsx', 'r4');
  assert.ok(calls.some(c => c.path === '/api/v1/documents/7/reading?cursor=1&revision=r1'));
  assert.ok(calls.some(c => c.path === '/api/v1/documents/7/attachments/8/reading?cursor=2&revision=r2'));
  assert.equal(calls.find(c => c.path.startsWith('/api/v1/advisor/tasks/9/reading')).header['X-WX-SERVICE'], 'api');
  const preview = calls.find(c => c.path === '/api/v1/exports/preview');
  assert.equal(preview.method, 'POST'); assert.deepEqual(plain(preview.data), { ids: ['7'], excerpts: 1 });
  assert.equal(calls.find(c => c.path === '/api/v1/exports').data.revision, 'r4');
  exportStatus = 409; await assert.rejects(api.exportFile(['7'], 'xlsx', 'r4'), e => e.status === 409);
});


test('名单查询离页丢弃迟回，返回保留输入并重新比对，不沿用旧命中', async () => {
  const resolves = []; const names = [];
  const p = load('lists', { isLive: () => true, lookup: name => { names.push(name); return new Promise(r => resolves.push(r)); } }, {
    deps: { '../../utils/format.js': { todayISO: () => '2026-10-23', cnDate: x => x }, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} }, '../../utils/cache.js': {} },
  });
  p.setData({ name: '合成科技有限公司' }); p.onShow(); p.query(); p.onHide();
  resolves[0]({ items: [{ id: '7' }] }); await new Promise(r => setImmediate(r));
  assert.equal(p.data.result, null); p.onShow(); assert.equal(names.length, 2);
  resolves[1]({ items: [], coverage: { lists: 20 } }); await new Promise(r => setImmediate(r));
  assert.equal(p.data.name, '合成科技有限公司'); assert.equal(p.data.result.items.length, 0); p.onUnload();
});


test('101列附件完整显示且保留字段名，不按客户端假上限丢弃', async () => {
  const cells = Array.from({ length: 101 }, (_, c) => ({ c, h: `列${c + 1}`, t: c === 100 ? '2026-10-30' : `值${c}` }));
  const p = load('reader', { reading: async () => reading({ segments: [{ n: 0, kind: 'row', cells, loc: { row: 2 } }] }) });
  p.onLoad({ id: '7', attachment: '8' }); await p.onShow();
  assert.equal(p.data.error, ''); assert.equal(p.data.segments[0].cells.length, 101);
  assert.match(textOf(render('pages/reader/index.wxml', p.data)), /列101：2026-10-30/);
});

test('批量结果保存使用原提交企业数而非展开命中行数，生成时间按北京时间显示', async () => {
  const calls = [];
  const p = load('results', { advisorReading: async () => result({ kind: 'compare', rowCount: 1, total: 10, generatedAt: '2026-10-23T03:04:05+00:00' }) }, {
    tools: { openResult: async (id, options) => { calls.push([id, plain(options.task)]); } },
  });
  p.onLoad({ mode: 'advisor', taskId: '8' }); await p.onShow(); await p.save();
  assert.deepEqual(calls, [['8', { kind: 'compare', rowCount: 1 }]]);
  const text = textOf(render('pages/results/index.wxml', p.data));
  assert.match(text, /2026年10月23日 11:04（北京时间）/); assert.doesNotMatch(text, /T03:04|\+00:00/);
});

test('保存未完成时不能翻结果页，完成后保存按钮必须恢复', async () => {
  let resolveSave;
  const p = load('results', { advisorReading: async (_id, args) => result({ kind: 'compare', rowCount: 1, nextCursor: args.cursor ? null : '20' }) }, {
    tools: { openResult: () => new Promise(resolve => { resolveSave = resolve; }) },
  });
  p.onLoad({ mode: 'advisor', taskId: '8' }); await p.onShow();
  p.setData({ error: '上一次保存失败' });
  const saving = p.save(); assert.equal(p.data.saving, true); assert.equal(p.data.error, '');
  const seq = p.seq; await p.retry(); assert.equal(p.seq, seq, '旧错误的重试事件也不能使保存回调失效');
  await p.next(); resolveSave(); await saving;
  assert.equal(p.data.saving, false, '翻页不能使保存的结束回调失效，按钮应恢复');
  assert.equal(p.data.page, 1, '保存期间停留在当前结果');
  await p.next(); assert.equal(p.data.page, 2);
});


test('保存期间上一页也被拦截，失败或取消后按钮恢复且可以继续翻页', async () => {
  for (const cancelled of [true, false]) {
    let rejectSave;
    const p = load('results', { advisorReading: async (_id, args) => result({ kind: 'compare', rowCount: 1, nextCursor: args.cursor ? null : '20' }) }, {
      tools: { openResult: () => new Promise((_resolve, reject) => { rejectSave = reject; }) },
    });
    p.onLoad({ mode: 'advisor', taskId: '8' }); await p.onShow(); await p.next();
    const saving = p.save();
    const controls = findAll(render('pages/results/index.wxml', p.data), n => n.attrs && n.attrs.bindtap === 'previous');
    assert.equal(controls.length, 1); assert.equal(controls[0].attrs.disabled, true);
    await p.previous(); assert.equal(p.data.page, 2);
    rejectSave(Object.assign(new Error('保存失败'), { cancelled })); await saving;
    assert.equal(p.data.saving, false); assert.equal(p.data.error, cancelled ? '' : '保存失败');
    await p.previous(); assert.equal(p.data.page, 1);
  }
});

test('离页取消保存，旧保存迟回不能复位新一轮保存状态', async () => {
  const jobs = [];
  const p = load('results', { advisorReading: async () => result({ kind: 'compare', rowCount: 1 }) }, {
    tools: { openResult: (_id, options) => new Promise(resolve => jobs.push({ resolve, current: options.current })) },
  });
  p.onLoad({ mode: 'advisor', taskId: '8' }); await p.onShow();
  const old = p.save(); assert.equal(jobs[0].current(), true); p.onHide();
  assert.equal(jobs[0].current(), false); assert.equal(p.data.saving, false);
  await p.onShow(); const fresh = p.save(); jobs[0].resolve(); await old;
  assert.equal(p.data.saving, true); assert.equal(jobs[1].current(), true);
  jobs[1].resolve(); await fresh; assert.equal(p.data.saving, false);
});

test('正文翻页错误时实际隐藏全部分页按钮，重试仍请求失败页而非跳过', async () => {
  let fail = true; const calls = [];
  const p = load('reader', { reading: async (_id, _att, args) => {
    calls.push(plain(args));
    if (args.cursor && fail) throw Object.assign(new Error('网络出错'), { status: 503 });
    return reading({ nextCursor: args.cursor ? null : '2', segments: [segment(args.cursor ? 2 : 1, '正文')] });
  } });
  p.onLoad({ id: '7' }); await p.onShow(); await p.next();
  assert.equal(p.data.error, '网络出错'); assert.deepEqual(p.data.segments, []);
  const tree = render('pages/reader/index.wxml', p.data);
  assert.equal(findAll(tree, n => n.attrs && ['next', 'previous'].includes(n.attrs.bindtap)).length, 0);
  assert.equal(findAll(tree, n => n.attrs && n.attrs.bindtap === 'retry').length, 1);
  fail = false; await p.retry();
  assert.deepEqual(calls[1], calls[2]); assert.equal(p.data.page, 2); assert.equal(p.data.segments[0].n, 2);
});
