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
      if (dep === '../../utils/reading-progress.js') return extra.progress || require('../utils/reading-progress.js');
      if (extra.deps && Object.hasOwn(extra.deps, dep)) return extra.deps[dep];
      throw new Error(dep);
    }, wx: { navigateTo: o => navigations.push(o.url), ...(extra.wx || {}) },
  });
  return { ...page, data: plain(page.data), navigations, setData(values) { Object.assign(this.data, plain(values)); } };
}
const segment = (n, text) => ({ n, kind: 'p', text });
const REV1 = 'a'.repeat(64); const REV2 = 'b'.repeat(64);
const TOC = { body: { readable: true, note: '' }, attachments: [
  { id: '8', name: '申报指南.pdf', ext: 'PDF', readable: true, state: 'ok', stateText: '' },
  { id: '9', name: '扫描件.pdf', ext: 'PDF', readable: false, state: 'scan', stateText: '扫描件，暂不能识别文字' },
] };
const reading = (patch = {}) => ({ status: 'ok', state: 'ok', title: '合成政策', source: { id: '7', title: '合成政策', issuer: '合成部门', host: 'example.gov.cn', date: '2026-10-23' }, revision: REV1, segments: [segment(1, '首段')], nextCursor: null, prevCursor: null, page: 1, pages: 1, totalSegments: 1, toc: TOC, ...patch });
// 续读记录的替身：记在内存里
function memoryProgress(initial = {}) {
  const map = { ...initial };
  return { map, get: (doc, part) => map[`${doc}:${part || 'body'}`] || null,
    save: (doc, part, revision, cursor, page) => { const k = `${doc}:${part || 'body'}`; if (page > 1 && cursor !== '0') map[k] = { revision, cursor, page }; else delete map[k]; },
    clear: (doc, part) => { delete map[`${doc}:${part || 'body'}`]; } };
}
const result = (patch = {}) => ({ kind: 'catalogue', title: '本次所选文件目录', revision: 'r1', notice: '合成样例', generatedAt: '2026-10-23', total: 1, items: [{ number: 1, doc_id: '7', title: '合成政策' }], ...patch });

test('正文多页：写"正文 第 N 页 / 共 M 页"，前后翻页绑定同一版本，不漏段或把两版合并', async () => {
  const calls = [];
  const p = load('reader', { reading: async (id, aid, args) => {
    calls.push([id, aid, plain(args)]);
    return args.cursor === '20' ? reading({ segments: [segment(21, '末段')], page: 2, pages: 2, prevCursor: '0' }) : reading({ nextCursor: '20', pages: 2 });
  } }, { progress: memoryProgress() });
  p.onLoad({ id: '7' }); await p.onShow();
  assert.equal(p.data.segments[0].text, '首段'); assert.equal(p.data.complete, false);
  // 页码只在底下翻页那里写一次（10/29：上下两处页码算重复）
  const shown = textOf(render('pages/reader/index.wxml', p.data));
  assert.match(shown, /第 1 页 \/ 共 2 页/); assert.equal((shown.match(/第 1 页 \/ 共 2 页/g) || []).length, 1);
  await p.next(); assert.equal(p.data.page, 2); assert.equal(p.data.segments[0].text, '末段'); assert.equal(p.data.complete, true);
  assert.deepEqual(calls[1], ['7', '', { cursor: '20', revision: REV1 }]);
  await p.previous(); assert.deepEqual(calls[2], ['7', '', { cursor: '0', revision: REV1 }]);
  assert.equal(p.data.segments.length, 1); assert.equal(p.data.segments[0].text, '首段'); assert.equal(p.data.page, 1);
});

test('内容更新（409）：记着的位置作废，写"正文已更新"，从第 1 页读新版本', async () => {
  let updated = false; const progress = memoryProgress();
  const p = load('reader', { reading: async (_id, _att, args) => {
    if (args.cursor !== '0') throw Object.assign(new Error('changed'), { status: 409 });
    return reading({ revision: updated ? REV2 : REV1, nextCursor: '20', pages: 2 });
  } }, { progress });
  p.onLoad({ id: '7' }); p.visible = true; await p.openPart(''); updated = true; await p.next();
  assert.equal(p.data.changed, false); assert.equal(p.data.revision, REV2); assert.equal(p.data.page, 1);
  assert.equal(p.data.updatedNote, '正文已更新，从第 1 页开始读'); assert.deepEqual(progress.map, {});
});

test('续读：再进来从上次读到的那一页接着读，写明第几页，可以从开头看', async () => {
  const calls = []; const progress = memoryProgress({ '7:body': { revision: REV1, cursor: '20', page: 2 } });
  const p = load('reader', { reading: async (_id, _att, args) => {
    calls.push(plain(args));
    return args.cursor === '20' ? reading({ segments: [segment(21, '第二页')], page: 2, pages: 3, prevCursor: '0', nextCursor: '40' }) : reading({ nextCursor: '20', pages: 3 });
  } }, { progress });
  p.onLoad({ id: '7' }); await p.onShow();
  assert.deepEqual(calls[0], { cursor: '20', revision: REV1 }); assert.equal(p.data.page, 2); assert.equal(p.data.resumePage, 2);
  assert.match(textOf(render('pages/reader/index.wxml', p.data)), /从你上次读到的第 2 页继续/);
  await p.fromStart(); assert.equal(p.data.page, 1); assert.equal(p.data.resumePage, 0); assert.deepEqual(progress.map, {});
  // 翻到第 2 页就记下来；离开再回来（如去反馈页）回到这一页，不再显示"从上次……继续"
  await p.next(); assert.deepEqual(progress.map['7:body'], { revision: REV1, cursor: '20', page: 2 });
  p.onHide(); await p.onShow(); assert.equal(p.data.page, 2); assert.equal(p.data.resumePage, 0);
});

test('目录：正文和每个附件都列出来，读不了的写状态、不能点；正文读完接着读附件 1', async () => {
  const calls = [];
  const p = load('reader', { reading: async (_id, att, args) => {
    calls.push([att, plain(args)]);
    return att === '8' ? reading({ title: '申报指南.pdf', segments: [segment(1, '附件正文')] }) : reading();
  } }, { progress: memoryProgress() });
  p.onLoad({ id: '7' }); await p.onShow();
  const text = textOf(render('pages/reader/index.wxml', p.data));
  assert.match(text, /正文/); assert.match(text, /附件 1\s*申报指南\.pdf/); assert.match(text, /附件 2\s*扫描件\.pdf\s*扫描件，暂不能识别文字/);
  assert.match(text, /正文完/); assert.match(text, /接着读附件 1：申报指南\.pdf/);
  assert.doesNotMatch(text, /example\.gov\.cn|来源 /);
  await p.openToc({ currentTarget: { dataset: { id: '9' } } }); assert.equal(calls.length, 1);
  await p.continueNext(); assert.deepEqual(calls[1], ['8', { cursor: '0' }]);
  assert.equal(p.data.partLabel, '附件 1'); assert.equal(p.data.partName, '申报指南.pdf'); assert.equal(p.data.docTitle, '合成政策');
  assert.match(textOf(render('pages/reader/index.wxml', p.data)), /附件 1 完/);
  await p.openToc({ currentTarget: { dataset: { id: '' } } }); assert.deepEqual(calls[2], ['', { cursor: '0' }]);
});

test('正文读不了（内容在附件里）：写服务端的说明，给"去读附件 1"', async () => {
  const p = load('reader', { reading: async () => reading({ status: 'empty', state: 'attachments', revision: null, segments: [], notice: '这份通知的内容在附件里。',
    toc: { body: { readable: false, note: '这份通知的内容在附件里。' }, attachments: TOC.attachments } }) }, { progress: memoryProgress() });
  p.onLoad({ id: '7' }); await p.onShow();
  const text = textOf(render('pages/reader/index.wxml', p.data));
  assert.match(text, /这份通知的内容在附件里。/); assert.match(text, /去读附件 1：申报指南\.pdf/); assert.doesNotMatch(text, /正文完|第 1 页/);
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

test('部分扫描附件：末尾写还缺几页扫描件、不说读完，真实位置和表头保留，不把序号推断为页码', async () => {
  const p = load('reader', { reading: async () => reading({ status: 'partial', scannedPages: 1, notice: '其中 1 页是扫描件，暂不能识别文字，下面的文字不含这些页。', segments: [{ n: 9, kind: 'row', cells: [{ c: 0, t: '12', h: '序号' }, { c: 1, t: '合成企业', h: '单位名称' }], loc: { sheet: '名单', row: 3 } }] }) }, { progress: memoryProgress() });
  p.onLoad({ id: '7', attachment: '8' }); p.visible = true; await p.openPart('8');
  assert.equal(p.data.complete, true); assert.equal(p.data.segments[0].cells[0].h, '序号');
  assert.equal(p.data.segments[0].loc, '工作表“名单”第 3 行');
  const text = textOf(render('pages/reader/index.wxml', p.data));
  assert.match(text, /单位名称：合成企业/); assert.doesNotMatch(text, /aaaa/); assert.match(text, /能显示的文字到这里。另有 1 页扫描件，暂不能识别文字/);
  assert.doesNotMatch(text, /附件 1 ?完|读完|已到本版本内容末尾|浏览器|复制/);
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


test('附件段落的位置：从大到小、说法和查名单一样，页码写原文第 N 页，同一页、同一个表只写变了的那层，空表名不写', async () => {
  const row = (n, t, loc) => ({ n, kind: 'row', cells: [{ c: 0, t, h: '名称' }], loc });
  const segments = [
    { n: 1, kind: 'p', text: '一、总则', loc: { page: 1 } }, { n: 2, kind: 'p', text: '第一条', loc: { page: 1 } },
    row(3, '甲', { page: 1, table: 1, row: 1 }), row(4, '乙', { page: 1, table: 1, row: 2 }),
    { n: 5, kind: 'p', text: '第二条', loc: { page: 2 } }, { n: 6, kind: 'p', text: '说明', loc: null },
    row(7, '丙', { table: 2, row: 1 }), row(8, '丁', { sheet: '汇总', row: 5 }), { n: 9, kind: 'p', text: '幻灯片文字', loc: { slide: 4 } },
    row(10, '戊', { sheet: '', row: 7 }), row(11, '己', { sheet: '  ', row: 8 }),
  ];
  const p = load('reader', { reading: async () => reading({ segments }) }, { progress: memoryProgress() });
  p.onLoad({ id: '7', attachment: '8' }); p.visible = true; await p.openPart('8');
  assert.deepEqual(p.data.segments.map((s) => s.loc),
    ['原文第 1 页', '', '第 1 个表第 1 行', '第 2 行', '原文第 2 页', '', '第 2 个表第 1 行', '工作表“汇总”第 5 行', '第 4 张幻灯片', '第 7 行', '第 8 行']);
  const text = textOf(render('pages/reader/index.wxml', p.data));
  assert.equal((text.match(/原文第 1 页/g) || []).length, 1);
  assert.doesNotMatch(text, / · |行 \d|表 \d|工作表“”/);
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
    if (args.cursor !== '0' && fail) throw Object.assign(new Error('网络出错'), { status: 503 });
    return args.cursor === '0' ? reading({ nextCursor: '20', pages: 2 }) : reading({ page: 2, pages: 2, prevCursor: '0', segments: [segment(21, '正文')] });
  } }, { progress: memoryProgress() });
  p.onLoad({ id: '7' }); await p.onShow(); await p.next();
  assert.equal(p.data.error, '网络出错'); assert.deepEqual(p.data.segments, []);
  const tree = render('pages/reader/index.wxml', p.data);
  assert.equal(findAll(tree, n => n.attrs && ['next', 'previous'].includes(n.attrs.bindtap)).length, 0);
  assert.equal(findAll(tree, n => n.attrs && n.attrs.bindtap === 'retry').length, 1);
  fail = false; await p.retry();
  assert.deepEqual(calls[1], calls[2]); assert.equal(p.data.page, 2); assert.equal(p.data.segments[0].n, 21);
});

test('换页、从开头看、接着读、目录、重试都回到页面顶上（一页约 4000 字，不回去新页开头在屏幕外面）', async () => {
  const scrolls = []; let fail = false;
  const p = load('reader', { reading: async (_id, att, args) => {
    if (fail) throw new Error('网络不通');
    if (att === '8') return reading({ title: '申报指南.pdf', segments: [segment(1, '附件正文')] });
    return args.cursor === '20' ? reading({ segments: [segment(21, '第二页')], page: 2, pages: 2, prevCursor: '0' }) : reading({ nextCursor: '20', pages: 2 });
  } }, { progress: memoryProgress(), wx: { pageScrollTo: (o) => scrolls.push(o.scrollTop) } });
  p.onLoad({ id: '7' }); await p.onShow();
  assert.deepEqual(scrolls, []);
  await p.next(); assert.equal(p.data.page, 2); assert.deepEqual(scrolls, [0]);
  await p.previous(); assert.equal(p.data.page, 1); assert.equal(scrolls.length, 2);
  await p.fromStart(); assert.equal(scrolls.length, 3);
  await p.next(); assert.equal(p.data.nextPart.id, '8');
  await p.continueNext(); assert.equal(p.data.partLabel, '附件 1'); assert.equal(scrolls.length, 5);
  await p.openToc({ currentTarget: { dataset: { id: '' } } }); assert.equal(scrolls.length, 6);
  fail = true; await p.previous(); assert.equal(p.data.error, '网络不通');
  fail = false; await p.retry(); assert.equal(p.data.page, 1); assert.equal(scrolls.length, 8);
});

test('目录里点、"接着读"读过的部分：接着上次那页，写明"从你上次读到的第 N 页继续"、给"从开头看"', async () => {
  const calls = []; const progress = memoryProgress({ '7:8': { revision: REV1, cursor: '20', page: 2 } });
  const p = load('reader', { reading: async (_id, att, args) => {
    calls.push([att, plain(args)]);
    if (att === '8') return args.cursor === '20' ? reading({ title: '申报指南.pdf', segments: [segment(21, '附件第二页')], page: 2, pages: 2, prevCursor: '0' }) : reading({ title: '申报指南.pdf', nextCursor: '20', pages: 2 });
    return reading();
  } }, { progress });
  p.onLoad({ id: '7' }); await p.onShow();
  await p.continueNext();
  assert.deepEqual(calls[1], ['8', { cursor: '20', revision: REV1 }]);
  assert.equal(p.data.page, 2); assert.equal(p.data.resumePage, 2);
  assert.match(textOf(render('pages/reader/index.wxml', p.data)), /从你上次读到的第 2 页继续\s*从开头看/);
  await p.openToc({ currentTarget: { dataset: { id: '' } } }); assert.equal(p.data.resumePage, 0);
  await p.openToc({ currentTarget: { dataset: { id: '8' } } }); assert.equal(p.data.page, 2); assert.equal(p.data.resumePage, 2);
});

test('有扫描页的附件末页：服务端写 ok 也按扫描页说，不写"完"', async () => {
  const p = load('reader', { reading: async () => reading({ status: 'ok', scannedPages: 2 }) }, { progress: memoryProgress() });
  p.onLoad({ id: '7', attachment: '8' }); await p.onShow();
  assert.equal(p.data.endText, '能显示的文字到这里。另有 2 页扫描件，暂不能识别文字');
});

test('记着的位置已不是一页的开头（422）：从第 1 页读，写明上次的位置失效了', async () => {
  const progress = memoryProgress({ '7:body': { revision: REV1, cursor: '30', page: 2 } });
  const p = load('reader', { reading: async (_id, _att, args) => {
    if (args.cursor === '30') throw Object.assign(new Error('阅读位置超出范围，请重新打开'), { status: 422 });
    return reading({ nextCursor: '20', pages: 2 });
  } }, { progress });
  p.onLoad({ id: '7' }); await p.onShow();
  assert.equal(p.data.page, 1); assert.equal(p.data.resumePage, 0);
  assert.equal(p.data.updatedNote, '上次读到的位置已失效，从第 1 页开始读'); assert.deepEqual(progress.map, {});
});

test('正在翻页时位置已不是一页的开头（422，如部署后分页变了）：回到第 1 页，写阅读位置已失效，不卡在重试上', async () => {
  const progress = memoryProgress();
  const calls = [];
  const p = load('reader', { reading: async (_id, _att, args) => {
    calls.push(args.cursor);
    if (args.cursor === '20') throw Object.assign(new Error('阅读位置超出范围，请重新打开'), { status: 422 });
    return reading({ nextCursor: '20', pages: 3 });
  } }, { progress });
  p.onLoad({ id: '7' }); await p.onShow();
  await p.next();
  assert.deepEqual(calls, ['0', '20', '0']);
  assert.equal(p.data.page, 1); assert.equal(p.data.error, '');
  assert.equal(p.data.updatedNote, '阅读位置已失效，从第 1 页开始读'); assert.deepEqual(progress.map, {});
});

test('目录里切到另一部分：加载期间页头和目录当前项就是要打开的那一部分，不留上一部分的名字（agy 第 1 轮）', async () => {
  let release; const held = new Promise((resolve) => { release = resolve; });
  const p = load('reader', { reading: async (_id, att) => {
    if (att === '8') { await held; return reading({ title: '申报指南.pdf', segments: [segment(1, '附件正文')] }); }
    return reading();
  } }, { progress: memoryProgress() });
  p.onLoad({ id: '7' }); await p.onShow();
  const opening = p.openToc({ currentTarget: { dataset: { id: '8' } } });
  assert.equal(p.data.partLabel, '附件 1'); assert.equal(p.data.partName, '申报指南.pdf');
  assert.deepEqual(p.data.toc.map((t) => t.current), [false, true, false]);
  assert.match(textOf(render('pages/reader/index.wxml', p.data)), /附件 1：申报指南\.pdf/);
  release(); await opening;
  assert.equal(p.data.partLabel, '附件 1');
  // 回正文：页头不再写附件名
  const back = p.openToc({ currentTarget: { dataset: { id: '' } } });
  assert.equal(p.data.partLabel, '正文'); assert.equal(p.data.partName, '');
  await back;
});

test('"接着读"按钮：附件名为空时不留冒号；加载中点"从开头看"不重复请求', async () => {
  const toc = { body: { readable: true, note: '' }, attachments: [{ id: '8', name: '', ext: 'PDF', readable: true, state: 'ok', stateText: '' }] };
  let calls = 0;
  const p = load('reader', { reading: async () => { calls += 1; return reading({ toc }); } }, { progress: memoryProgress() });
  p.onLoad({ id: '7' }); await p.onShow();
  const text = textOf(render('pages/reader/index.wxml', p.data));
  assert.match(text, /接着读附件 1/); assert.doesNotMatch(text, /接着读附件 1：/);
  p.data.loading = true; const before = calls;
  assert.equal(p.fromStart(), undefined); assert.equal(calls, before);
});

test('目录切到另一部分时接口报错：页头和目录当前项仍是要打开的那一部分，点重试接着打开它（Grok 第 3 轮建议）', async () => {
  let fail = false;
  const p = load('reader', { reading: async (_id, att) => {
    if (att === '8' && fail) throw new Error('网络不通');
    return att === '8' ? reading({ title: '申报指南.pdf', segments: [segment(1, '附件正文')] }) : reading();
  } }, { progress: memoryProgress() });
  p.onLoad({ id: '7' }); await p.onShow();
  fail = true;
  await p.openToc({ currentTarget: { dataset: { id: '8' } } });
  assert.equal(p.data.error, '网络不通');
  assert.equal(p.data.partLabel, '附件 1'); assert.equal(p.data.partName, '申报指南.pdf');
  assert.deepEqual(p.data.toc.map((t) => t.current), [false, true, false]);
  fail = false; await p.retry();
  assert.equal(p.data.error, ''); assert.equal(p.data.partLabel, '附件 1'); assert.equal(p.data.segments[0].text, '附件正文');
});
