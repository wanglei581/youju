const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const plain = (x) => JSON.parse(JSON.stringify(x));
const TOKEN = 'Abcd1234EFghij_-Abcd1234EFghij_-';

function load(name, api, extra = {}) {
  let page;
  const navigations = [];
  const wxApi = {
    title: '',
    failBack: false,
    navigateTo: (o) => navigations.push(o.url),
    navigateBack: (o) => { if (wxApi.failBack && o && typeof o.fail === 'function') o.fail(); },
    setNavigationBarTitle: (o) => { wxApi.title = o && o.title || ''; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(root, `pages/${name}/index.js`), 'utf8'), {
    Page: (def) => { page = def; },
    Promise,
    require: (dep) => {
      if (dep === '../../utils/api.js') return api;
      if (dep === '../../utils/exporter.js') return extra.exporter || {};
      if (dep === '../../utils/data-tools.js') return extra.tools || {};
      if (dep === '../../utils/reading-progress.js') return extra.progress || require('../utils/reading-progress.js');
      if (extra.deps && Object.hasOwn(extra.deps, dep)) return extra.deps[dep];
      if (dep === '../../utils/pages.js') return require('./helpers/package-pages-default.js').pages;
      throw new Error(dep);
    },
    wx: wxApi,
  });
  return {
    ...page,
    data: plain(page.data),
    navigations,
    wx: wxApi,
    setData(values) { Object.assign(this.data, plain(values)); },
  };
}

function record(patch = {}) {
  return {
    entry: { name: '企业名称', category: '科技型中小企业', batch: '第一批', region: '济南市', ...patch.entry },
    document: {
      id: '123', title: '名单文件标题', status: '公示', issuer: '发文机关', date: '2026-09-02',
      region: '山东省', site: '发文机关网站', host: 'example.gov.cn', link_gone: null, ...patch.document,
    },
    evidence: { state: 'found', rows: [], more: false, partial: false, pagesRead: null, reason: null, ...patch.evidence },
  };
}
async function open(entry, api) {
  const p = load('reader', api);
  p.onLoad(typeof entry === 'object' ? entry : { entry });
  await p.onShow();
  return p;
}
function stringsIn(value, out = []) {
  if (typeof value === 'string') out.push(value);
  else if (Array.isArray(value)) value.forEach((item) => stringsIn(item, out));
  else if (value && typeof value === 'object') Object.values(value).forEach((item) => stringsIn(item, out));
  return out;
}

test('token 不合法不请求', async () => {
  assert.equal(/^[A-Za-z0-9_-]{32}$/.test(TOKEN), true);
  // 以前"编号.签名"的写法、长短不对、带别的符号的都不请求
  const invalids = ['', 'bad', '42.Abcd1234EFghij_-', 'A'.repeat(31), 'A'.repeat(33), `${'A'.repeat(31)}*`, `${'A'.repeat(31)}.`];
  for (const entry of invalids) {
    let called = false;
    const p = await open(entry, { listRecord: async () => { called = true; return record(); } });
    assert.equal(called, false, entry);
    assert.equal(p.data.listMode, true);
    assert.equal(p.data.loading, false);
    assert.equal(p.data.record, null);
    assert.equal(p.data.listErrorKind, 'invalid');
    assert.equal(p.data.error, '这条记录的地址无效，请回查名单页重新查询。');
  }
  // 编号无效时也给"回查名单"按钮，不让人卡在这一页
  const wxml = fs.readFileSync(path.join(root, 'pages/reader/index.wxml'), 'utf8');
  assert.match(wxml, /listErrorKind === 'missing' \|\| listErrorKind === 'invalid'\}\}" bindtap="backToLists"/);
});

test('没有 entry 时仍走站内阅读', async () => {
  const seen = [];
  const p = load('reader', {
    reading: async (id) => {
      seen.push(id);
      return {
        status: 'ok', title: '合成政策', revision: 'rev1', nextCursor: null, totalSegments: 1,
        source: { issuer: '合成部门', date: '2026-10-23', title: '合成政策', host: 'example.gov.cn' },
        segments: [{ n: 1, kind: 'p', text: '首段' }],
      };
    },
    listRecord: async () => { throw new Error('不应请求名单这一行'); },
  });
  p.onLoad({ id: '7' });
  await p.onShow();
  assert.deepEqual(seen, ['7']);
  assert.equal(p.data.listMode, false);
  assert.equal(p.data.record, null);
  assert.equal(p.data.segments[0].text, '首段');
  assert.equal(p.data.complete, true);
});

test('found 显示位置、各格、隐藏列和公示说明', async () => {
  const calls = [];
  const rows = [];
  for (let n = 1; n <= 6; n += 1) {
    rows.push({
      where: n === 1 ? '工作表“名单”第 2 行' : `第 ${n} 处`,
      cells: [{ h: '企业名称', t: `企业名称${n}`, html: '<b>no</b>', name: true }, { h: '', t: '仅文字' }, { h: 9, t: false, name: 'yes' }],
      hidden: n === 1 ? 2 : 0,
    });
  }
  const p = await open(TOKEN, {
    listRecord: async (token) => {
      calls.push(token);
      return record({
        document: { link_gone: '2026-10-01' },
        evidence: { state: 'found', rows, more: true, partial: true, pagesRead: 4, reason: null },
      });
    },
  });
  const view = p.data.record;
  assert.deepEqual(calls, [TOKEN]);
  assert.equal(p.wx.title, '原名单里的这一行');
  assert.equal(p.data.loading, false);
  assert.equal(p.data.error, '');
  assert.equal(view.docTitle, '名单文件标题');
  assert.equal(view.issuerLine, '发文机关 2026-09-02');
  assert.equal(view.statusLine, '发布阶段：公示');
  assert.equal(view.publicityNote, '发布阶段按名单标题用词标注，是否还在公示期以原文为准');
  assert.equal(view.regionLine, '地区：山东省');
  assert.equal(view.disclaimer, '这一行摘自政府公开发布的名单，有据只做整理，不作判断。');
  // 只写来源网站的域名，不给原文链接
  assert.equal(view.sourceLine, '来源 example.gov.cn');
  assert.equal('urlLine' in view, false);
  assert.equal(view.linkGoneLine, '原网页 2026-10-01 起已打不开，下面是此前收录的内容');
  assert.equal(view.found, true);
  assert.equal(view.rows.length, 5);
  assert.equal(view.rows.some((row) => row.where === '第 6 处'), false);
  assert.equal(view.rows[0].whereText, '在原文的位置：工作表“名单”第 2 行');
  assert.deepEqual(view.rows[0].cells, [
    { i: 0, h: '企业名称', t: '企业名称1', name: true },
    { i: 1, h: '', t: '仅文字', name: false },
    { i: 2, h: '', t: '', name: false },
  ]);
  assert.equal(view.rows[0].hidden, 2);
  assert.equal(view.rows[0].hiddenText, '另有 2 列没有显示（联系方式、证件等）');
  assert.equal(view.moreLine, '原文里这个名称出现超过 5 处，只列出前 5 处');
  assert.equal(view.partialLine, '这份名单很长，只读了前 4 页');
  assert.equal(view.parsedTitle, '从原文解析出的记录');
  assert.deepEqual(view.parsed.map((item) => [item.label, item.value]), [
    ['名称', '企业名称'], ['类别', '科技型中小企业'], ['批次', '第一批'], ['地区', '济南市'],
  ]);
  p.openDetail();
  p.feedback();
  assert.deepEqual(p.navigations, [
    '/pages/detail/index?id=123',
    `/pages/feedback/index?id=123&title=${encodeURIComponent('名单文件标题')}`,
  ]);
});

test('pending、failed、notfound、in_body 显示说明和解析记录', async () => {
  const cases = [
    {
      patch: {
        document: { status: '公布', date: null },
        entry: { batch: null, region: null },
        evidence: { state: 'pending', rows: [{ where: '第 9 行', cells: [{ h: '列', t: '值' }], hidden: 4 }], more: true, partial: true, pagesRead: 2 },
      },
      note: '这份名单的原文还在整理，暂时显示不了原文那一行。下面是从原文解析出的记录。',
      statusLine: '发布阶段：公布',
      parsed: [['名称', '企业名称'], ['类别', '科技型中小企业']],
      issuerLine: '发文机关',
    },
    {
      patch: { evidence: { state: 'failed', reason: null, rows: [{ where: '第 1 行', cells: [{ h: '列', t: '值' }], hidden: 1 }] } },
      note: '原文附件暂时没读出来，暂时显示不了原文那一行。下面是从原文解析出的记录。',
      statusLine: '发布阶段：公示',
    },
    {
      patch: { evidence: { state: 'failed', reason: '附件已移除' } },
      note: '附件已移除，暂时显示不了原文那一行。下面是从原文解析出的记录。',
      statusLine: '发布阶段：公示',
    },
    {
      patch: { document: { status: null }, evidence: { state: 'notfound' } },
      note: '在原文里没有找到这一行（名称在原文里可能换了行或写法不同）。下面是从原文解析出的记录。',
      statusLine: '',
    },
    {
      patch: { evidence: { state: 'no_column' } },
      note: '没认出原文表格里哪一列是企业名称，暂时对不上原文那一行。下面是从原文解析出的记录。',
      statusLine: '发布阶段：公示',
    },
    {
      patch: { evidence: { state: 'in_body', reason: '附件已移除' } },
      note: '这份名单在通知网页正文的表格里，暂时显示不了原文那一行。下面是从原文解析出的记录。',
      statusLine: '发布阶段：公示',
    },
  ];
  for (const item of cases) {
    const p = await open(TOKEN, { listRecord: async () => record(item.patch) });
    const view = p.data.record;
    assert.equal(view.stateNote, item.note);
    assert.equal(view.parsedTitle, '解析出的记录');
    assert.equal(view.found, false);
    assert.deepEqual(view.rows, []);
    assert.equal(view.moreLine, '');
    assert.equal(view.partialLine, '');
    // 公示的提醒是否还在公示期以原文为准（原文那一行显示不出来时照样提醒），公布的不写
    const note = item.statusLine === '发布阶段：公示' ? '发布阶段按名单标题用词标注，是否还在公示期以原文为准' : '';
    assert.equal(view.publicityNote, note);
    assert.equal(view.statusLine, item.statusLine);
    assert.equal(view.disclaimer, '这一行摘自政府公开发布的名单，有据只做整理，不作判断。');
    assert.equal(view.sourceLine, '来源 example.gov.cn');
    if (item.issuerLine) assert.equal(view.issuerLine, item.issuerLine);
    if (item.parsed) assert.deepEqual(view.parsed.map((field) => [field.label, field.value]), item.parsed);
    else assert.equal(view.parsed.length, 4);
  }
});

test('404 显示回查名单，其他错误显示原因和重试', async () => {
  const missing = await open(TOKEN, {
    listRecord: async () => { throw Object.assign(new Error('内部说明'), { status: 404 }); },
  });
  assert.equal(missing.data.error, '这条记录已更新或不存在，请回查名单页重新查询。');
  assert.equal(missing.data.listErrorKind, 'missing');
  assert.equal(missing.data.record, null);
  assert.equal(missing.data.loading, false);
  missing.wx.failBack = true;
  missing.backToLists();
  assert.deepEqual(missing.navigations, ['/pages/lists/index']);

  const limited = await open(TOKEN, {
    listRecord: async () => { throw Object.assign(new Error('查询太频繁，请稍后再试'), { status: 429 }); },
  });
  assert.equal(limited.data.error, '查询太频繁，请稍后再试');
  assert.equal(limited.data.listErrorKind, 'retry');

  const offline = await open(TOKEN, { listRecord: async () => { throw new Error('网络不可用'); } });
  assert.equal(offline.data.error, '网络不可用');
  assert.equal(offline.data.listErrorKind, 'retry');

  const blank = await open(TOKEN, { listRecord: async () => { throw new Error(''); } });
  assert.equal(blank.data.error, '没有加载出来，请重试');
  assert.equal(blank.data.listErrorKind, 'retry');

  let calls = 0;
  const again = await open(TOKEN, { listRecord: async () => { calls += 1; throw new Error('网络不可用'); } });
  await again.retry();
  assert.equal(calls, 2);
  assert.equal(again.data.listErrorKind, 'retry');
});

test('离页后晚到响应不写入，回到页面重新请求', async () => {
  let resolveFirst;
  let gate = () => new Promise((resolve) => { resolveFirst = resolve; });
  const p = load('reader', { listRecord: () => gate() });
  p.onLoad({ entry: TOKEN });
  const first = p.onShow();
  assert.equal(p.data.loading, true);
  await Promise.resolve(); // 请求已发出，还没回来
  p.onHide();
  resolveFirst(record());
  await first;
  assert.equal(p.data.record, null);
  assert.equal(p.data.loading, false);
  assert.equal(p.data.error, '');
  gate = async () => record({ evidence: { state: 'pending' } });
  await p.onShow();
  assert.equal(p.data.record.state, 'pending');
  assert.equal(p.data.record.stateNote, '这份名单的原文还在整理，暂时显示不了原文那一行。下面是从原文解析出的记录。');
  assert.equal(p.wx.title, '原名单里的这一行');
});

test('页面文字里没有禁用词', async () => {
  const banned = [
    '复制原文链接', '浏览器', '以原网站为准', '复制后可能打不开', '其他应用打开', '用其他应用', '换个应用', '用 WPS',
    '信用', '失信', '征信', '评分', '风险', '命中', '符合条件', '可以申报', '原文网址', '原文链接', 'http', '资格', '结论', '正式认定',
  ];
  const files = ['pages/reader/index.js', 'pages/reader/index.wxml', 'pages/reader/index.wxss']
    .map((name) => fs.readFileSync(path.join(root, name), 'utf8')).join('\n');
  const required = [
    '原名单里的这一行',
    '发布阶段按名单标题用词标注，是否还在公示期以原文为准',
    '这一行摘自政府公开发布的名单，有据只做整理，不作判断。',
    '正在加载…',
    '这条记录的地址无效，请回查名单页重新查询。',
    '这条记录已更新或不存在，请回查名单页重新查询。',
    '没有加载出来，请重试',
    '列没有显示（联系方式、证件等）',
    '原文里这个名称出现超过 5 处，只列出前 5 处',
    '这份名单很长，只读了前 ',
    '这份名单的原文还在整理，暂时显示不了原文那一行。下面是从原文解析出的记录。',
    '原文附件暂时没读出来',
    '在原文里没有找到这一行（名称在原文里可能换了行或写法不同）。下面是从原文解析出的记录。',
    '这份名单在通知网页正文的表格里，暂时显示不了原文那一行。下面是从原文解析出的记录。',
    '从原文解析出的记录',
    '解析出的记录',
    '发布阶段：',
    '来源 ',
    '起已打不开，下面是此前收录的内容',
    '在原文的位置：',
    '看发布这份名单的通知',
    '回查名单',
  ];
  for (const word of banned) assert.equal(files.includes(word), false, word);
  for (const word of required) assert.equal(files.includes(word), true, word);
  for (const word of ['web-view', 'setClipboardData', 'getClipboardData', 'rich-text', '<navigator']) {
    assert.equal(files.includes(word), false, word);
  }
  const p = await open(TOKEN, { listRecord: async () => record({ document: { link_gone: '2026-10-01' } }) });
  const text = stringsIn(p.data).join('\n');
  for (const word of banned) assert.equal(text.includes(word), false, word);
});

test('名单类通知正文挡掉了名单行（D-092）：站内阅读页给在已收录名单中查询企业', async () => {
  const p = load('reader', {
    reading: async () => ({
      status: 'ok', title: '名单通知', revision: 'rev1', nextCursor: null, totalSegments: 1, listHidden: true,
      source: { issuer: '发文机关', date: '2026-10-20', title: '名单通知', host: 'example.gov.cn' },
      segments: [{ n: 1, kind: 'p', text: '公示时间：10月20日至26日。\n〔这里是名单内容（4 行），站内不展开，可以在查名单里查企业〕' }],
    }),
  });
  p.onLoad({ id: '7' });
  await p.onShow();
  assert.equal(p.data.listHidden, true);
  const wxml = fs.readFileSync(path.join(root, 'pages/reader/index.wxml'), 'utf8');
  assert.match(wxml, /wx:if="\{\{status === 'list' \|\| listHidden\}\}"[^>]*bindtap="lookup"/);
  p.lookup();
  assert.deepEqual(p.navigations, ['/pages/lists/index']);
});

test('名单类通知详情页（D-092）：正文里的名单没展开、正文主要是名单时都给去查名单的入口', () => {
  const wxml = fs.readFileSync(path.join(root, 'pages/detail/index.wxml'), 'utf8');
  const js = fs.readFileSync(path.join(root, 'pages/detail/index.js'), 'utf8');
  assert.match(wxml, /wx:if="\{\{doc\.body\.listHidden\}\}"[^>]*bindtap="toLists"[^>]*>名单请在“查名单”里按企业名称查询 ›</);
  assert.match(wxml, /wx:if="\{\{doc\.body\.status === 'list'\}\}" class="link body-lookup" bindtap="toLists"[^>]*>在“查名单”里按企业名称查询 ›</);
  assert.match(js, /toLists\(\) \{\s*wx\.navigateTo\(\{ url: '\/pages\/lists\/index' \}\);/);
});
