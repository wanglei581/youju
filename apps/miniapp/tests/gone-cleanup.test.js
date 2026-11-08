// D-123：原网页打不开的通知 7 天后清理；这 7 天里正文、附件照常能看，可以保存（我的保存）、导出 Word。
// 服务端开关（YOUJU_GONE_CLEANUP_ENABLED）开着、还在清理期里的，接口才给 gone（卡片、详情）和 goneNote（阅读页）；
// 没给的照 D-097 显示（见 link-gone.test.js）
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');

const root = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(root, file), 'utf8');
const settle = () => new Promise((resolve) => setImmediate(resolve));
// D-124 ④：照 agy 合规审阅的改法，哪天停止展示写在第一句里（服务端给的完整提示）
const NOTICE = '原网页已无法访问，本平台将于10月28日停止展示；内容变动以官方最新发布为准';
const GONE = { since: '2026-10-21', cleanup: '2026-10-28', notice: NOTICE, cleanupNotice: '可以先保存或导出', list: false, window: true, canSave: true, canExport: true };
// 名单类（D-123 ④）：不清理，一直标明已撤下；7 天过后不给正文、不给保存
const LIST_LATER = { since: '2026-10-21', cleanup: null, notice: '原网页10月21日起已撤下', cleanupNotice: '正文不再提供，名单请在“查名单”里按企业名称查询', list: true, window: false, canSave: false, canExport: false };

function pageOf(file, deps, globals = {}) {
  let def;
  vm.runInNewContext(read(file), {
    module: { exports: {} }, setTimeout, clearTimeout, Promise, Date, Math,
    require(name) {
      if (Object.hasOwn(deps, name)) return deps[name];
      if (name === '../../utils/format.js') return require('../utils/format.js');
      if (name === '../../utils/repost.js') return require('../utils/repost.js');
      if (name === '../../utils/regions.js') return require('../utils/regions.js');
      if (/(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages;
      if (/ai-access\.js$/.test(name)) return require('./helpers/ai-off.js');
      throw new Error(`${file} 缺少依赖 ${name}`);
    },
    Page(d) { def = d; },
    wx: { navigateTo() {}, setNavigationBarTitle() {}, showToast() {}, showLoading() {}, hideLoading() {}, pageScrollTo() {}, ...(globals.wx || {}) },
    getApp: () => ({ globalData: {} }), getCurrentPages: () => [{}, {}],
  }, { filename: file });
  return { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, JSON.parse(JSON.stringify(values))); } };
}

function rowOf(doc) {
  let def;
  vm.runInNewContext(read('components/doc-row/index.js'), {
    module: { exports: {} }, Date, Math,
    require(name) {
      if (name === '../../utils/format.js') return require('../utils/format.js');
      if (name === '../../utils/repost.js') return require('../utils/repost.js');
      throw new Error(name);
    },
    Component(d) { def = d; }, wx: { navigateTo() {} },
  });
  const c = { ...def, data: { ...(def.data || {}) }, properties: {}, setData(v) { Object.assign(this.data, v); } };
  c.observers['doc, q, today'].call(c, doc, '');
  return c.data;
}

test('卡片：清理期里写服务端给的提示（含哪天停止展示），不再单独标"原文链接已失效"；不在清理期里照旧', () => {
  const doc = { id: '1', kind: '申报通知', title: '关于开展申报的通知', issuer: '某厅', date: '2026-10-01', link_gone: '2026-10-21', gone: GONE };
  const data = rowOf(doc);
  assert.equal(data.goneLine, NOTICE);
  const tree = render('components/doc-row/index.wxml', { ...data, doc });
  assert.equal(findAll(tree, hasClass('gone-line')).map((n) => textOf(n).trim()).join(), NOTICE);
  assert.doesNotMatch(textOf(tree), /清理/);
  assert.equal(findAll(tree, hasClass('gone')).length, 0);
  const old = { ...doc, gone: undefined };
  const plain = render('components/doc-row/index.wxml', { ...rowOf(old), doc: old });
  assert.equal(findAll(plain, hasClass('gone-line')).length, 0);
  assert.equal(findAll(plain, hasClass('gone')).map((n) => textOf(n).trim()).join(), '原文链接已失效');
});

function detailDeps(doc, calls) {
  return {
    '../../utils/api.js': {
      detail: () => Promise.resolve(JSON.parse(JSON.stringify(doc))),
      saveDoc: (id) => { calls.push(['save', id]); return Promise.resolve({ item: { id: '7' } }); },
      exportGone: (id) => { calls.push(['export-api', id]); return Promise.resolve(new ArrayBuffer(4)); },
    },
    '../../utils/subscribe.js': { remindOpen: () => false, requestOnce: () => Promise.resolve(false) },
    '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) },
    '../../utils/consent.js': { loginOpen: () => true, ensureConsent: () => { calls.push(['consent']); return Promise.resolve(true); } },
    '../../utils/exporter.js': { previewDocs() {}, exportWord: (fetch, opts) => { calls.push(['export', opts]); return Promise.resolve(fetch()).then(() => true); } },
    '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/attachments.js': { decorate: (list) => (list || []).map((a) => ({ ...a, canOpen: !!a.readable, meta: '', action: '阅读' })) },
  };
}

test('详情页：清理期里标题下写提示和清理日期，正文、附件照常；给"保存"（先同意协议）和"导出 Word"（不用登录）', async () => {
  const doc = { id: '9', kind: '申报通知', tags: ['申报通知'], title: '关于开展科技计划项目申报的通知', issuer: '某厅', date: '2026-10-20',
    region: '山东 · 省级', points: [], link_gone: '2026-10-21', gone: GONE,
    body: { status: 'ok', text: '申报说明。'.repeat(20), total: 100, truncated: false },
    attachments: [{ id: '3', name: '申报材料.docx', ext: 'docx', readable: true }] };
  const calls = [];
  const page = pageOf('pages/detail/index.js', detailDeps(doc, calls));
  page.onLoad({ id: '9' }); await settle();
  assert.equal(page.data.goneNote, `${NOTICE}。可以先保存或导出。`);
  assert.equal(page.data.goneSave, true);
  assert.equal(page.data.goneExport, true);
  assert.equal(page.data.goneAll, false);
  const tree = render('pages/detail/index.wxml', { ...page.data, loading: false });
  const note = findAll(tree, hasClass('gone-note')).map((n) => textOf(n)).join('');
  assert.ok(note.trim().startsWith(`${NOTICE}。可以先保存或导出。`), note);
  assert.doesNotMatch(note, /清理|撤回或更正/);
  assert.deepEqual(findAll(tree, hasClass('gone-act')).map((n) => textOf(n).trim()), ['保存', '导出 Word']);
  assert.match(textOf(tree), /申报说明。/);
  await page.saveGone(); await settle();
  assert.deepEqual(calls.slice(0, 2), [['consent'], ['save', '9']]);
  assert.equal(page.data.savedHere, true);
  assert.match(textOf(render('pages/detail/index.wxml', { ...page.data, loading: false })), /已保存/);
  await page.exportGone(); await settle();
  const exported = calls.find((c) => c[0] === 'export');
  assert.deepEqual([exported[1].source, String(exported[1].docId)], ['gone', '9']);
  assert.ok(calls.some((c) => c[0] === 'export-api' && c[1] === '9'));
  // 包里没有"我的保存"（过审版）或没开登录：不给"保存"，"导出 Word"照给
  const without = render('pages/detail/index.wxml', { ...page.data, has: { ...page.data.has, saved: false }, loading: false });
  assert.deepEqual(findAll(without, hasClass('gone-act')).map((n) => textOf(n).trim()), ['导出 Word']);
});

test('名单类：卡片和详情页标明"原网页某月某日起已撤下"，不写清理日期；7 天过后不给保存、导出', async () => {
  const card = { id: '2', kind: '名单', title: '拟认定名单的公示', issuer: '某厅', date: '2026-10-01', link_gone: '2026-10-21', gone: LIST_LATER };
  assert.equal(rowOf(card).goneLine, '原网页10月21日起已撤下');
  const doc = { id: '2', kind: '名单', tags: ['名单'], title: '拟认定名单的公示', issuer: '某厅', date: '2026-10-20', region: '山东 · 省级',
    points: [], link_gone: '2026-10-21', gone: LIST_LATER, body: { status: 'none', reason: 'link_gone' }, attachments: [] };
  const page = pageOf('pages/detail/index.js', detailDeps(doc, []));
  page.onLoad({ id: '2' }); await settle();
  assert.equal(page.data.goneNote, '原网页10月21日起已撤下。正文不再提供，名单请在“查名单”里按企业名称查询。');
  assert.equal(page.data.goneSave || page.data.goneExport, false);
  const tree = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.equal(findAll(tree, hasClass('gone-act')).length, 0);
  assert.doesNotMatch(textOf(tree), /清理/);
});

test('详情页：清理期里没有能存的文字（正文就是名单、附件不展示）时不给保存、导出', async () => {
  const listInWindow = { ...GONE, notice: '原网页10月21日起已撤下', cleanup: null, cleanupNotice: '', list: true, canSave: false, canExport: false };
  const doc = { id: '3', kind: '名单', tags: ['名单'], title: '拟认定名单的公示', issuer: '某厅', date: '2026-10-20', region: '山东 · 省级',
    points: [], link_gone: '2026-10-21', gone: listInWindow, body: { status: 'list' }, attachments: [] };
  const page = pageOf('pages/detail/index.js', detailDeps(doc, []));
  page.onLoad({ id: '3' }); await settle();
  assert.equal(page.data.goneNote, '原网页10月21日起已撤下。');
  assert.equal(page.data.goneSave || page.data.goneExport, false);
  assert.equal(findAll(render('pages/detail/index.wxml', { ...page.data, loading: false }), hasClass('gone-act')).length, 0);
});

test('详情页：新版隐私指引还没上线（服务端 canSave 为假）时只给导出', async () => {
  const doc = { id: '9', kind: '申报通知', tags: ['申报通知'], title: '关于开展科技计划项目申报的通知', issuer: '某厅', date: '2026-10-20',
    region: '山东 · 省级', points: [], link_gone: '2026-10-21', gone: { ...GONE, canSave: false, cleanupNotice: '可以先导出' },
    body: { status: 'ok', text: '申报说明。'.repeat(20), total: 100, truncated: false }, attachments: [] };
  const page = pageOf('pages/detail/index.js', detailDeps(doc, []));
  page.onLoad({ id: '9' }); await settle();
  const tree = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.deepEqual(findAll(tree, hasClass('gone-act')).map((n) => textOf(n).trim()), ['导出 Word']);
});

test('详情页：包里没有"我的保存"（过审版）时提示只写"可以先导出"，和按钮对得上', async () => {
  const doc = { id: '9', kind: '申报通知', tags: ['申报通知'], title: '关于开展科技计划项目申报的通知', issuer: '某厅', date: '2026-10-20',
    region: '山东 · 省级', points: [], link_gone: '2026-10-21', gone: GONE,
    body: { status: 'ok', text: '申报说明。'.repeat(20), total: 100, truncated: false }, attachments: [] };
  const page = pageOf('pages/detail/index.js', detailDeps(doc, []));
  page.data.has = { ...page.data.has, saved: false };
  page.onLoad({ id: '9' }); await settle();
  assert.equal(page.data.goneNote, `${NOTICE}。可以先导出。`);
  const tree = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.deepEqual(findAll(tree, hasClass('gone-act')).map((n) => textOf(n).trim()), ['导出 Word']);
});

test('阅读副本："返回我的保存"上一页是我的保存就退回，从别处打开的换成我的保存页', () => {
  const calls = [];
  const make = (prevRoute) => {
    let def;
    vm.runInNewContext(read('pages/reader/index.js'), {
      module: { exports: {} }, setTimeout, clearTimeout, Promise, Date, Math,
      require(name) {
        if (/reading-progress\.js$/.test(name)) return { get: () => null, save() {}, clear() {} };
        if (/api\.js$/.test(name)) return {};
        if (/(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages;
        throw new Error(name);
      },
      Page(d) { def = d; },
      getCurrentPages: () => [{ route: prevRoute }, { route: 'pages/reader/index' }],
      wx: { navigateBack: () => calls.push('back'), redirectTo: ({ url }) => calls.push(url) },
    });
    return def;
  };
  make('pages/saved/index').backToSaved();
  make('pages/files/index').backToSaved();
  assert.deepEqual(calls, ['back', '/pages/saved/index']);
});

test('详情页：服务端没给 gone（开关关着、过了清理日期、不清理的）照 D-097，不给保存、导出', async () => {
  const doc = { id: '9', kind: '申报通知', tags: ['申报通知'], title: '关于开展申报的通知', issuer: '某厅', date: '2026-10-20', region: '山东 · 省级',
    points: [], link_gone: '2026-10-21', body: { status: 'none', reason: 'link_gone' }, attachments: [] };
  const page = pageOf('pages/detail/index.js', detailDeps(doc, []));
  page.onLoad({ id: '9' }); await settle();
  assert.equal(page.data.goneSave || page.data.goneExport, false);
  assert.match(page.data.goneNote, /^原文链接已失效（2026-10-21 检查时打不开）。正文和附件不再提供/);
  const tree = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.equal(findAll(tree, hasClass('gone-act')).length, 0);
});

function savedDeps(list, calls, synced = true) {
  return {
    '../../utils/api.js': {
      isLive: () => true,
      savedList: () => Promise.resolve(JSON.parse(JSON.stringify(list))),
      deleteSaved: (id) => { calls.push(['delete', id]); return Promise.resolve(); },
      exportSaved: (id) => { calls.push(['export-api', id]); return Promise.resolve(new ArrayBuffer(4)); },
    },
    '../../utils/consent.js': { loginOpen: () => true, syncConsent: () => Promise.resolve(synced), ensureConsent: () => Promise.resolve(true) },
    '../../utils/exporter.js': { exportWord: (fetch, opts) => { calls.push(['export', opts]); return Promise.resolve(fetch()).then(() => true); } },
  };
}

const ITEM = { id: '7', docId: '9', title: '关于开展科技计划项目申报的通知', issuer: '山东省科技厅', date: '2026-10-22', goneOn: '2026-10-21',
  savedAt: '2026-10-23T10:00:00+08:00', chars: 1450, truncated: false, cleaned: false, note: '原网页10月21日起无法访问；内容变动以官方最新发布为准。这是你10月23日保存的副本。' };

test('我的保存：列出标题、出处、打不开的日期和保存日期；点开用阅读页读，导出 Word、删除（先确认）', async () => {
  const calls = []; const urls = []; let modal;
  const list = { items: [ITEM, { ...ITEM, id: '8', docId: '10', cleaned: true }], max: 200 };
  const page = pageOf('pages/saved/index.js', savedDeps(list, calls), {
    wx: { navigateTo: ({ url }) => urls.push(url), showModal: (m) => { modal = m; m.success({ confirm: true }); } },
  });
  page.onShow(); await settle(); await settle();
  assert.equal(page.data.mode, 'live');
  assert.deepEqual(page.data.items.map((i) => i.state), ['约 1450 字', '本平台已停止展示原通知，这里是保存的副本']);
  const tree = render('pages/saved/index.wxml', page.data);
  const text = textOf(tree);
  assert.match(text, /我的保存 2/);
  assert.match(text, /山东省科技厅 · 2026-10-22/);
  assert.match(text, /这是你10月23日保存的副本/);
  assert.match(text, /不含附件原件；最多 200 份/);
  const event = (id) => ({ currentTarget: { dataset: { id } } });
  page.read(event('8'));
  assert.deepEqual(urls, ['/pages/reader/index?saved=8']);
  await page.exportOne(event('7'));
  assert.deepEqual(calls.filter((c) => c[0] !== 'export').map((c) => c.join(':')), ['export-api:7']);
  assert.equal(calls.find((c) => c[0] === 'export')[1].source, 'saved');
  page.remove(event('8')); await settle();
  assert.match(modal.content, /本平台已停止展示原通知，删除后就看不到了/);
  assert.ok(calls.some((c) => c[0] === 'delete' && c[1] === '8'));
});

test('我的保存：没同意协议时先请登录；空列表写怎么保存', async () => {
  const gated = pageOf('pages/saved/index.js', savedDeps({ items: [] }, [], false));
  gated.onShow(); await settle(); await settle();
  assert.equal(gated.data.mode, 'gated');
  assert.match(textOf(render('pages/saved/index.wxml', gated.data)), /登录后使用/);
  const empty = pageOf('pages/saved/index.js', savedDeps({ items: [], max: 200 }, []));
  empty.onShow(); await settle(); await settle();
  assert.match(textOf(render('pages/saved/index.wxml', empty.data)), /还没有保存过/);
});

test('阅读页读保存的副本：走我的保存的接口，写打不开和保存的日期，不记读到哪一页，底下是"返回我的保存"', async () => {
  const asked = []; const saved = [];
  const page = pageOf('pages/reader/index.js', {
    '../../utils/api.js': {
      savedReading: (id, part, params) => { asked.push([id, part, params]); return Promise.resolve({
        status: 'ok', title: ITEM.title, source: { id: '9', title: ITEM.title, issuer: '山东省科技厅', date: '2026-10-22' },
        revision: 'a'.repeat(64), segments: [{ n: 1, kind: 'p', text: '申报说明。', loc: null, cells: null }], nextCursor: null, prevCursor: null,
        page: 1, pages: 1, totalSegments: 1, toc: { body: { readable: true, note: '' }, attachments: [] }, goneNote: ITEM.note,
      }); },
      reading: () => { throw new Error('不该读通知本身'); },
    },
    '../../utils/reading-progress.js': { get: () => null, save: (...args) => saved.push(args), clear() {} },
  });
  page.onLoad({ saved: '7' });
  await page.onShow(); await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(asked)), [['7', '', { cursor: '0' }]]);
  assert.equal(page.data.goneNote, ITEM.note);
  assert.equal(saved.length, 0);
  const text = textOf(render('pages/reader/index.wxml', { ...page.data, loading: false }));
  assert.match(text, /这是你10月23日保存的副本/);
  assert.match(text, /返回我的保存/);
  assert.doesNotMatch(text, /返回文件详情|纠错/);
});

test('导出记录：清理期导出的回到文件详情，我的保存里导出的回到保存的副本', () => {
  const urls = [];
  const page = pageOf('pages/files/index.js', {
    '../../utils/api.js': { isLive: () => true },
    '../../utils/exports.js': { list: () => [] },
    '../../utils/exporter.js': { previewDocs: () => { throw new Error('不该看目录'); }, exists: () => true, removeFiles() {} },
  }, { wx: { navigateTo: ({ url }) => urls.push(url) } });
  page.data.demo = false;
  page.records = [{ source: 'gone', docId: '9', format: 'docx' }, { source: 'saved', savedId: '7', format: 'docx' }];
  page.open({ currentTarget: { dataset: { index: 0 } } });
  page.reexport({ currentTarget: { dataset: { index: 1 } } });
  assert.deepEqual(urls, ['/pages/detail/index?id=9&from=gone', '/pages/reader/index?saved=7']);
});

test('从导出记录回到已清理的通知：说清楚本平台已停止展示、保存过的去我的保存看', async () => {
  const deps = detailDeps({}, []);
  // 和 utils/api.js 一样：接口回的 404 带说明（detail）
  const missing = () => Object.assign(new Error('没有找到这份文件'), { status: 404, detail: '没有找到这份文件' });
  let answer = () => Promise.reject(missing());
  deps['../../utils/api.js'] = { detail: () => answer() };
  const page = pageOf('pages/detail/index.js', deps);
  page.onLoad({ id: '9', from: 'gone' }); await settle();
  assert.equal(page.data.error, '这份通知原网页已无法访问，本平台已停止展示；保存过的在“我的 → 我的保存”里看');
  // 找不到的重试也不会有：给"返回"，不给"重试"
  // 错误卡片右边那个按钮的字（错误文字本身可能带"重试"二字，只看按钮）
  const button = (data) => findAll(render('pages/detail/index.wxml', { ...data, loading: false }), hasClass('retry')).map((n) => textOf(n).trim()).join();
  assert.equal(button(page.data), '返回');
  const plain = pageOf('pages/detail/index.js', deps);
  plain.onLoad({ id: '9' }); await settle();
  assert.equal(plain.data.error, '没有找到这份文件');
  assert.equal(button(plain.data), '返回');
  // 再打开时网络出错（同 utils/api.js 的 callFailure：不带状态码）：变回"重试"
  answer = () => Promise.reject(Object.assign(new Error('网络不太好，请稍后再试'), { retryable: true }));
  plain.load(); await settle();
  assert.equal(plain.data.notFound, false);
  assert.equal(button(plain.data), '重试');
  // 网关回的 404 页面（不带说明）：不当成找不到，照旧给"重试"，也不说成已停止展示
  answer = () => Promise.reject(Object.assign(new Error('服务暂时出错（404），请稍后重试'), { status: 404 }));
  const gateway = pageOf('pages/detail/index.js', deps);
  gateway.onLoad({ id: '9', from: 'gone' }); await settle();
  assert.equal(gateway.data.error, '服务暂时出错（404），请稍后重试');
  assert.equal(gateway.data.notFound, false);
  assert.equal(button(gateway.data), '重试');
});

test('我的页：服务端给了 saved、包里有这一页才放"我的保存"，写保存了几份', () => {
  const tree = (data) => textOf(render('pages/me/index.wxml', { loginOpen: true, has: {}, gridCols: 3, stats: null, ...data }));
  assert.match(tree({ savedEntry: true, savedHint: '2 份' }), /我的保存2 份/);
  assert.doesNotMatch(tree({ savedEntry: false }), /我的保存/);
});

test('注销确认写上保存的通知；从"保存"来的同意弹窗写"保存通知前"，不写"关注和纠错"', async () => {
  const consent = read('utils/consent.js');
  assert.match(consent, /const saved = has && has\.saved \? '、保存的通知' : ''/);
  const modals = [];
  const mod = { exports: {} };
  vm.runInNewContext(consent, {
    module: mod, setTimeout, clearTimeout, Promise,
    require(name) {
      if (name === '../content/legal.js') return { final: true, version: '2027-01' };
      if (name === './config.js') return { templates: {} };
      if (name === './api.js') return { isLive: () => true, AGREED_KEY: 'agreed', forgetAgreed() {}, me: () => Promise.resolve({ agreed: null }) };
      if (name === './exports.js' || name === './exporter.js') return {};
      if (/ai-access\.js$/.test(name)) return require('./helpers/ai-off.js');
      if (/(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages;
      throw new Error(name);
    },
    wx: { getStorageSync: () => '', setStorageSync() {}, removeStorageSync() {}, showToast() {}, showModal: (o) => { modals.push(o); o.success({ confirm: false }); }, navigateTo() {} },
  });
  const ok = await mod.exports.ensureConsent({ purpose: 'save' });
  assert.equal(ok, false);
  assert.match(modals[0].content, /^保存通知前，请阅读并同意《用户协议》和《隐私保护指引》/);
  assert.doesNotMatch(modals[0].content, /关注和纠错/);
});

test('接口：保存、列表、按页读、删除、导出的地址和方法', async () => {
  const sent = [];
  let def;
  const api = (() => {
    const mod = { exports: {} };
    vm.runInNewContext(read('utils/api.js'), {
      module: mod, setTimeout, clearTimeout, Promise, Date, Math, console,
      require(name) {
        if (name === './config.js') return { cloudEnv: '', cloudService: 'api', devBaseUrl: 'http://dev' };
        if (name === '../mock/data.js') return {};
        if (name === './mockSearch.js') return { mockSearch() {} };
        if (name === './format.js') return require('../utils/format.js');
        throw new Error(name);
      },
      wx: {
        request: (o) => { sent.push([o.method, o.url, o.data, o.responseType]); o.success({ statusCode: 200, data: o.responseType ? new ArrayBuffer(2) : { ok: true } }); },
        getStorageSync: () => '', removeStorageSync() {}, showLoading() {}, hideLoading() {},
      },
    });
    def = mod.exports;
    return def;
  })();
  await api.saveDoc(9);
  await api.savedList();
  await api.savedReading('7', '2', { cursor: '0' });
  await api.deleteSaved('7');
  await api.exportSaved('7');
  await api.exportGone('9');
  assert.deepEqual(sent.map((s) => `${s[0]} ${s[1]}`), [
    'POST http://dev/api/v1/me/saved',
    'GET http://dev/api/v1/me/saved',
    'GET http://dev/api/v1/me/saved/7/reading?part=2&cursor=0',
    'DELETE http://dev/api/v1/me/saved/7',
    'POST http://dev/api/v1/me/saved/7/export',
    'POST http://dev/api/v1/documents/9/gone-export',
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0][2])), { docId: '9' });
  assert.equal(sent[4][3], 'arraybuffer');
});

// ---- D-124 ②：通知到期清理以后，引用它的 AI 记录、批量结果 ----

test('我的记录、AI 首页、工具中心：通知已经到期清理的（后台 docCleaned）标"原通知已停止展示"，照常能删', () => {
  const { taskView } = require('../utils/advisor.js');
  const task = taskView({ id: 5, kind: 'interpret', status: 'done', hasResult: true, createdAt: '2026-11-01T02:00:00Z', docId: null, docTitle: null, linkGone: null, docCleaned: true });
  const tasks = textOf(render('pages/advisor-tasks/index.wxml', { loading: false, toolsEnabled: true, aiEnabled: true, items: [task], has: {} }));
  assert.match(tasks, /原通知已停止展示/);
  assert.match(tasks, /删除/);
  assert.doesNotMatch(tasks, /原文链接已失效/);
  const recent = textOf(render('pages/advisor/index.wxml', { loading: false, toolsEnabled: true, recentLoading: false, recent: [task] }));
  assert.match(recent, /原通知已停止展示 · /);
  const home = textOf(render('pages/ai/index.wxml', { records: [{ id: '5', kind: 'interpret', kindLabel: 'AI 解读', title: '', time: '2026-11-01', stateLabel: '已完成', linkGone: '', cleaned: true }] }));
  assert.match(home, /已完成 · 原通知已停止展示/);
  const plain = textOf(render('pages/advisor-tasks/index.wxml', { loading: false, toolsEnabled: true, aiEnabled: true, items: [{ ...task, docCleaned: undefined }], has: {} }));
  assert.doesNotMatch(plain, /停止展示/);
});

test('批量结果：引用的通知已经到期清理的那一条只留标题和说明，不给各项内容和"查看文件"；别的条照常', () => {
  const note = '这一条的通知原网页已无法访问，本平台已停止展示，这一条不再提供';
  const items = [
    { n: 0, documentId: '9', title: '合成通知1', fields: [{ label: '截止核对情况', value: '未核对' }] },
    { n: 1, documentId: null, title: '合成通知2', fields: [], cleaned: true, note },
  ];
  const tree = render('pages/results/index.wxml', { loading: false, advisor: true, result: { items, title: '客户相关通知表' }, page: 1, nextCursor: null, has: {} });
  const text = textOf(tree);
  assert.match(text, /2\. 合成通知2/);
  assert.match(text, new RegExp(note));
  assert.equal((text.match(/查看文件和依据/g) || []).length, 1);
});
