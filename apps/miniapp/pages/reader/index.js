const api = require('../../utils/api.js');
const progress = require('../../utils/reading-progress.js');

// 服务端没给说明时的兜底（页面说法以服务端 notice 为准，D-097 设计稿第三节）
const STATUS = {
  scan: '扫描件，暂不能识别文字。', empty: '这部分没有能显示的文字。', unavailable: '这部分暂时读不了。',
  list: '名单请在“查名单”里按企业名称查询。', unsupported: '这部分暂不能在小程序内阅读。',
};
const LIST_TITLE = '原名单里的这一行';
// 查名单结果给的加密编号（D-088）：32 个字母、数字、-、_
const ENTRY_TOKEN = /^[A-Za-z0-9_-]{32}$/;
const ENTRY_FIELDS = [['名称', 'name'], ['类别', 'category'], ['批次', 'batch'], ['地区', 'region']];

// 不把服务端字符串解释成 HTML；表格按实际单元格排列，不推测页码或字段。
function segmentsOf(items) {
  if (!Array.isArray(items) || items.length > 200) throw new Error('阅读内容格式有误，请重试');
  return items.map((s) => {
    if (!s || typeof s.text !== 'string' && !Array.isArray(s.cells)) throw new Error('阅读内容格式有误，请重试');
    const cells = Array.isArray(s.cells) ? s.cells : [];
    if (cells.some((cell) => !cell || typeof cell.t !== 'string')) throw new Error('表格内容格式有误');
    const labels = { page: '第', sheet: '工作表', row: '行', table: '表', slide: '幻灯片' };
    const loc = s.loc && typeof s.loc === 'object' ? Object.keys(labels).filter((key) => s.loc[key] != null)
      .map((key) => `${labels[key]} ${s.loc[key]}${key === 'page' ? ' 页' : ''}`).join(' · ') : '';
    return { n: s.n, kind: s.kind, text: s.text || '', loc,
      cells: cells.map((cell) => ({ c: cell.c, t: cell.t, h: typeof cell.h === 'string' ? cell.h : '' })) };
  });
}

function textOf(value) {
  return typeof value === 'string' ? value : '';
}
function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}
function stateNoteOf(evidence) {
  const state = evidence.state;
  if (state === 'pending') return '这份名单的原文还在整理，暂时显示不了原文那一行。下面是从原文解析出的记录。';
  if (state === 'failed') {
    const reason = typeof evidence.reason === 'string' && evidence.reason ? evidence.reason : '原文附件暂时没读出来';
    return `${reason}，暂时显示不了原文那一行。下面是从原文解析出的记录。`;
  }
  if (state === 'notfound') return '在原文里没有找到这一行（名称在原文里可能换了行或写法不同）。下面是从原文解析出的记录。';
  if (state === 'in_body') return '这份名单在通知网页正文的表格里，暂时显示不了原文那一行。下面是从原文解析出的记录。';
  if (state === 'no_column') return '没认出原文表格里哪一列是企业名称，暂时对不上原文那一行。下面是从原文解析出的记录。';
  return '';
}
// 单元格只读取 h、t、name；不是字符串则当空串。其他服务端字段不进入页面数据。
function evidenceRows(evidence) {
  const source = Array.isArray(evidence.rows) ? evidence.rows.slice(0, 5) : [];
  return source.map((row, i) => {
    const item = objectOf(row);
    const cells = Array.isArray(item.cells) ? item.cells.map((cell, ci) => {
      const current = objectOf(cell);
      return { i: ci, h: textOf(current.h), t: textOf(current.t), name: current.name === true };
    }) : [];
    const hidden = typeof item.hidden === 'number' && Number.isFinite(item.hidden) && item.hidden > 0 ? item.hidden : 0;
    const where = textOf(item.where);
    return {
      i, where, whereText: `在原文的位置：${where}`, cells, hidden,
      hiddenText: hidden > 0 ? `另有 ${hidden} 列没有显示（联系方式、证件、备注和认不出列名的列）` : '',
    };
  });
}
function entryItems(entry) {
  return ENTRY_FIELDS.filter(([, key]) => typeof entry[key] === 'string' && entry[key])
    .map(([label, key], i) => ({ i, label, value: entry[key] }));
}
function listRowView(res) {
  const doc = objectOf(res.document);
  const entry = objectOf(res.entry);
  const evidence = objectOf(res.evidence);
  const found = evidence.state === 'found';
  const status = doc.status === '公示' || doc.status === '公布' ? doc.status : '';
  const region = textOf(doc.region);
  const host = textOf(doc.host);
  const gone = textOf(doc.link_gone);
  const pages = typeof evidence.pagesRead === 'number' && Number.isFinite(evidence.pagesRead) ? String(evidence.pagesRead) : '';
  return {
    docId: doc.id == null ? '' : String(doc.id),
    docTitle: textOf(doc.title),
    issuerLine: [textOf(doc.issuer), textOf(doc.date)].filter(Boolean).join(' '),
    issuer: textOf(doc.issuer),
    date: textOf(doc.date),
    statusLine: status ? `发布阶段：${status}` : '',
    // 发布阶段只照录标题用词（D-090）；公示的提醒是否还在公示期由原文说了算，公布的不写
    publicityNote: status === '公示' ? '发布阶段按名单标题用词标注，是否还在公示期以原文为准' : '',
    regionLine: region ? `地区：${region}` : '',
    disclaimer: '这一行摘自政府公开发布的名单，有据只做整理，不作判断。',
    sourceLine: host ? `来源 ${host}` : '',
    linkGoneLine: gone ? `原网页 ${gone} 起已打不开，下面是此前收录的内容` : '',
    state: textOf(evidence.state),
    stateNote: found ? '' : stateNoteOf(evidence),
    found,
    rows: found ? evidenceRows(evidence) : [],
    moreLine: found && evidence.more === true ? '原文里这个名称出现超过 5 处，只列出前 5 处' : '',
    partialLine: found && evidence.partial === true ? `这份名单很长，只读了前 ${pages} 页` : '',
    parsedTitle: found ? '我们从原文解析出的记录（以上面的原文为准）' : '解析出的记录',
    parsed: entryItems(entry),
  };
}

// 阅读页的目录（D-097）："正文 · 附件 1 · 附件 2"，读不了的也列出来、写状态（和详情页同一个判断）
function tocItems(toc, part) {
  const value = objectOf(toc);
  const body = objectOf(value.body);
  const items = [{ id: '', label: '正文', name: '', readable: body.readable === true, note: textOf(body.note), current: !part }];
  const list = Array.isArray(value.attachments) ? value.attachments.slice(0, 50) : [];
  list.forEach((raw, i) => {
    const item = objectOf(raw);
    const id = item.id == null ? '' : String(item.id);
    if (!/^\d+$/.test(id)) return;
    items.push({ id, label: `附件 ${i + 1}`, name: textOf(item.name), readable: item.readable === true,
      note: item.readable === true ? '' : textOf(item.stateText), current: id === part });
  });
  return items;
}

// 这一部分后面第一个能读的附件：正文读完、附件读完（或这一部分读不了）时给"接着读附件 N：××"
function nextPartOf(items, part) {
  const at = items.findIndex((item) => item.current);
  return items.slice(at + 1).find((item) => item.id && item.readable) || null;
}

// 读到这一部分的末尾写什么：有扫描页的写还缺几页，不写"读完"
function endTextOf(label, status, scanned) {
  if (scanned > 0) return `能显示的文字到这里。另有 ${scanned} 页扫描件，暂不能识别文字`;
  // "正文完""附件 1 完"：数字后面空一格
  return `${label}${/\d$/.test(label) ? ' ' : ''}完`;
}

// 换页、换部分时回到页面顶上（一页约 4000 字，不回去新页开头在屏幕外面）
function toTop() {
  if (typeof wx !== 'undefined' && wx && typeof wx.pageScrollTo === 'function') wx.pageScrollTo({ scrollTop: 0, duration: 0 });
}

Page({
  data: { loading: false, error: '', changed: false, source: null, title: '', revision: '', status: '', notice: '',
    segments: [], nextCursor: null, prevCursor: null, page: 1, pages: 0, totalSegments: 0, scannedPages: 0, complete: false,
    docTitle: '', partLabel: '正文', partName: '', toc: [], nextPart: null, endText: '', resumePage: 0, updatedNote: '',
    listMode: false, listTitle: LIST_TITLE, listErrorKind: '', record: null, listHidden: false },
  onLoad(q) {
    this.docId = String(q.id || ''); this.part = q.attachment ? String(q.attachment) : '';
    this.revision = ''; this.firstShow = true;
    this.listMode = q.entry != null;
    this.entryToken = this.listMode ? String(q.entry) : '';
    if (this.listMode) this.setData({ listMode: true });
  },
  onShow() {
    this.visible = true;
    if (api.watchStartup) api.watchStartup(this);
    // 从后台或其他页面回来重新验证，旧内容不能在撤回后继续当作有效内容展示。回到记着的那一页
    if (this.listMode) return this.loadRecord();
    const banner = this.firstShow; this.firstShow = false;
    return this.openPart(this.part, { resume: true, banner });
  },
  onHide() {
    this.visible = false; this.seq = (this.seq || 0) + 1;
    if (api.stopStartup) api.stopStartup(this);
    const clear = { segments: [], source: null, loading: false, complete: false };
    if (this.listMode) {
      clear.record = null; clear.error = ''; clear.listErrorKind = '';
    }
    this.setData(clear);
  },
  onUnload() { this.onHide(); },
  // 打开正文或某个附件：记着读到第几页（同一内容版本）就从那一页接着读，否则从第 1 页
  openPart(part, opts = {}) {
    this.part = part ? String(part) : ''; this.revision = '';
    // 加载期间（或出错时）页头和目录就写要打开的这一部分，不留上一部分的名字
    const toc = (this.data.toc || []).map((t) => Object.assign({}, t, { current: t.id === this.part }));
    const here = toc.find((t) => t.current);
    this.setData({ segments: [], source: null, revision: '', nextCursor: null, prevCursor: null, page: 1, pages: 0,
      complete: false, endText: '', nextPart: null, resumePage: 0, updatedNote: '', toc,
      partLabel: here ? here.label : (this.part ? '附件' : '正文'), partName: here && this.part ? here.name : '' });
    const saved = opts.resume && /^\d+$/.test(this.docId) ? progress.get(this.docId, this.part) : null;
    if (saved) return this.loadPage(saved.cursor, saved.revision, { resumed: saved.page, banner: opts.banner });
    return this.loadPage('0', '');
  },
  restart() { progress.clear(this.docId, this.part); return this.openPart(this.part); },
  loadPage(cursor, revision, opts = {}) {
    if (!this.visible) return Promise.resolve();
    const seq = this.seq = (this.seq || 0) + 1;
    const current = () => this.visible && seq === this.seq;
    this.setData({ loading: true, error: '', changed: false, segments: [], complete: false });
    if (!/^\d+$/.test(this.docId) || this.part && !/^\d+$/.test(this.part)) {
      this.setData({ loading: false, error: '阅读地址无效，请返回文件详情重新打开。' }); return Promise.resolve();
    }
    // 出错时"重试"重新请求这一页（不跳过、不回到第 1 页）
    this.lastRequest = { cursor, revision, opts };
    const params = revision ? { cursor, revision } : { cursor };
    return api.reading(this.docId, this.part, params).then((res) => {
      if (!current()) return;
      if (!res) throw new Error('阅读内容格式有误，请重试');
      const toc = tocItems(res.toc, this.part);
      const here = toc.find((item) => item.current) || { label: this.part ? '附件' : '正文' };
      const source = objectOf(res.source);
      const docTitle = this.part ? (this.data.docTitle || textOf(source.title)) : textOf(res.title);
      const head = { toc, docTitle, partLabel: here.label, partName: this.part ? textOf(res.title) : '', source,
        title: docTitle || '站内阅读' };
      if (!['ok', 'partial'].includes(res.status)) {
        progress.clear(this.docId, this.part); this.revision = '';
        this.setData(Object.assign(head, { segments: [], revision: '', nextCursor: null, prevCursor: null, page: 1, pages: 0,
          listHidden: false, status: res.status, notice: textOf(res.notice) || STATUS[res.status] || '这部分暂时读不了。',
          scannedPages: 0, complete: false, endText: '', nextPart: nextPartOf(toc, this.part), resumePage: 0 }));
        return;
      }
      if (typeof res.revision !== 'string' || !res.revision || revision && revision !== res.revision) {
        throw Object.assign(new Error('内容版本已变化，请重新阅读'), { status: 409 });
      }
      const segments = segmentsOf(res.segments || []);
      const next = res.nextCursor == null ? null : String(res.nextCursor);
      const prev = res.prevCursor == null ? null : String(res.prevCursor);
      const page = Number.isInteger(res.page) && res.page > 0 ? res.page : 1;
      const pages = Number.isInteger(res.pages) && res.pages >= page ? res.pages : page;
      if (next !== null && (!/^\d+$/.test(next) || next === cursor)) throw new Error('阅读分页信息有误，请重试');
      this.revision = res.revision;
      // 记住读到第几页：内容版本、这一页的续读位置（第 1 页不记）
      progress.save(this.docId, this.part, res.revision, cursor, page);
      const last = next === null;
      const scanned = Number(res.scannedPages) > 0 ? Number(res.scannedPages) : 0;
      this.setData(Object.assign(head, { revision: res.revision, status: res.status, listHidden: res.listHidden === true,
        notice: textOf(res.notice), segments, nextCursor: next, prevCursor: prev, page, pages,
        totalSegments: res.totalSegments || 0, scannedPages: scanned,
        complete: last && segments.length > 0, endText: last ? endTextOf(here.label, res.status, scanned) : '',
        nextPart: last ? nextPartOf(toc, this.part) : null,
        resumePage: opts.resumed && opts.banner && page > 1 ? page : 0, updatedNote: opts.updated || '' }));
    }).catch((err) => {
      if (!current()) return undefined;
      // 内容更新了（409），或记着的位置已不是一页的开头（422）：作废记着的位置，回到第 1 页
      if (err.status === 409 || (opts.resumed && err.status === 422)) {
        progress.clear(this.docId, this.part); this.revision = '';
        const what = this.part ? '附件' : '正文';
        const updated = err.status === 409 ? `${what}已更新，从第 1 页开始读` : '上次读到的位置已失效，从第 1 页开始读';
        if (cursor === '0' && !revision) {
          this.setData({ error: '内容版本已变化，请重新加载。', changed: true });
          return undefined;
        }
        return this.loadPage('0', '', { updated });
      }
      if ([403, 404, 410].includes(err.status)) {
        progress.clear(this.docId, this.part); this.revision = '';
        this.setData({ source: null, revision: '', nextCursor: null, prevCursor: null, segments: [], page: 1, pages: 0 });
      }
      this.setData({ error: err.message || '内容未加载，请重试', changed: false });
      return undefined;
    }).then(() => { if (current()) this.setData({ loading: false }); });
  },
  loadRecord() {
    if (!this.visible) return Promise.resolve();
    const seq = this.seq = (this.seq || 0) + 1;
    const current = () => this.visible && seq === this.seq;
    if (typeof wx.setNavigationBarTitle === 'function') wx.setNavigationBarTitle({ title: this.data.listTitle || LIST_TITLE });
    if (!ENTRY_TOKEN.test(this.entryToken)) {
      this.setData({ loading: false, record: null, listErrorKind: 'invalid', error: '这条记录的地址无效，请回查名单页重新查询。' });
      return Promise.resolve();
    }
    this.setData({ loading: true, error: '', listErrorKind: '', record: null });
    return Promise.resolve().then(() => api.listRecord(this.entryToken)).then((res) => {
      if (!current()) return;
      if (!res || typeof res !== 'object') {
        this.setData({ loading: false, record: null, listErrorKind: 'retry', error: '没有加载出来，请重试' });
        return;
      }
      this.setData({ loading: false, error: '', listErrorKind: '', record: listRowView(res) });
    }).catch((err) => {
      if (!current()) return;
      if (err && err.status === 404) {
        this.setData({ loading: false, record: null, listErrorKind: 'missing', error: '这条记录已更新或不存在，请回查名单页重新查询。' });
        return;
      }
      const message = err && typeof err.message === 'string' && err.message ? err.message : '没有加载出来，请重试';
      this.setData({ loading: false, record: null, listErrorKind: 'retry', error: message });
    });
  },
  next() {
    if (this.data.loading || this.data.nextCursor === null) return undefined;
    toTop();
    return this.loadPage(this.data.nextCursor, this.revision);
  },
  previous() {
    if (this.data.loading || this.data.prevCursor === null) return undefined;
    toTop();
    return this.loadPage(this.data.prevCursor, this.revision);
  },
  // "从开头看"：不用记着的位置，从第 1 页读
  fromStart() {
    if (this.data.loading) return undefined;
    progress.clear(this.docId, this.part);
    this.setData({ resumePage: 0 });
    toTop();
    return this.loadPage('0', this.revision);
  },
  // 目录里点正文或某个附件：读不了的不能点（状态已写在目录里）。读过的接着上次那页，写明"从你上次
  // 读到的第 N 页继续"、给"从开头看"
  openToc(e) {
    const id = String((e && e.currentTarget && e.currentTarget.dataset && e.currentTarget.dataset.id) || '');
    const item = this.data.toc.find((t) => t.id === id);
    if (!item || item.current || !item.readable || this.data.loading) return undefined;
    toTop();
    return this.openPart(id, { resume: true, banner: true });
  },
  // "接着读附件 N"：和目录里点一样，读过的接着上次那页并写明
  continueNext() {
    const target = this.data.nextPart;
    if (!target || this.data.loading) return undefined;
    toTop();
    return this.openPart(target.id, { resume: true, banner: true });
  },
  retry() {
    if (this.listMode) return this.loadRecord();
    toTop();
    if (this.data.changed || !this.lastRequest) return this.restart();
    const { cursor, revision, opts } = this.lastRequest;
    return this.loadPage(cursor, revision, opts);
  },
  feedback() {
    const id = this.listMode ? (this.data.record && this.data.record.docId || '') : this.docId;
    const title = this.listMode ? (this.data.record && this.data.record.docTitle || '') : (this.data.docTitle || this.data.title);
    wx.navigateTo({ url: `/pages/feedback/index?id=${encodeURIComponent(id)}&title=${encodeURIComponent(title)}` });
  },
  lookup() { wx.navigateTo({ url: '/pages/lists/index' }); },
  detail() { wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(this.docId)}` }); },
  openDetail() {
    const rec = this.data.record;
    if (!rec) return;
    if (!/^\d+$/.test(rec.docId)) return;
    wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(rec.docId)}` });
  },
  backToLists() {
    const openLists = () => wx.navigateTo({ url: '/pages/lists/index' });
    if (typeof wx.navigateBack !== 'function') { openLists(); return; }
    wx.navigateBack({ fail: openLists });
  },
});
