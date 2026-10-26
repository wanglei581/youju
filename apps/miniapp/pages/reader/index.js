const api = require('../../utils/api.js');

const STATUS = {
  partial: '内容尚不完整，请勿据此认定已读到全部原文。', scan: '扫描页尚未形成可阅读内容。',
  empty: '暂未取得可阅读内容。', unavailable: '这份内容暂不可用。',
  list: '名单仅提供已提取的企业查询结果，暂不提供完整名单。', unsupported: '该格式暂不能在站内阅读。',
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

Page({
  data: { loading: false, error: '', changed: false, source: null, title: '', revision: '', status: '', notice: '',
    segments: [], nextCursor: null, page: 1, totalSegments: 0, scannedPages: 0, complete: false,
    listMode: false, listTitle: LIST_TITLE, listErrorKind: '', record: null, listHidden: false },
  onLoad(q) {
    this.docId = String(q.id || ''); this.attachmentId = q.attachment || '';
    this.cursors = ['']; this.cursorIndex = 0;
    this.listMode = q.entry != null;
    this.entryToken = this.listMode ? String(q.entry) : '';
    if (this.listMode) this.setData({ listMode: true });
  },
  onShow() {
    this.visible = true;
    if (api.watchStartup) api.watchStartup(this);
    // 从后台或其他页面回来重新验证，旧内容不能在撤回后继续当作有效内容展示。
    if (this.listMode) return this.loadRecord();
    return this.restart();
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
  restart() {
    this.cursors = ['']; this.cursorIndex = 0;
    this.setData({ segments: [], source: null, revision: '', nextCursor: null, page: 1, complete: false });
    return this.loadPage();
  },
  loadPage() {
    if (!this.visible) return Promise.resolve();
    const seq = this.seq = (this.seq || 0) + 1;
    const current = () => this.visible && seq === this.seq;
    const cursor = this.cursors[this.cursorIndex];
    const revision = this.data.revision;
    this.setData({ loading: true, error: '', changed: false, segments: [], complete: false });
    if (!/^\d+$/.test(this.docId) || this.attachmentId && !/^\d+$/.test(this.attachmentId)) {
      this.setData({ loading: false, error: '阅读地址无效，请返回文件详情重新打开。' }); return Promise.resolve();
    }
    return api.reading(this.docId, this.attachmentId, { cursor, revision }).then((res) => {
      if (!current()) return;
      if (!res) throw new Error('阅读内容格式有误，请重试');
      if (!['ok', 'partial'].includes(res.status)) {
        this.cursors = ['']; this.cursorIndex = 0;
        this.setData({ segments: [], source: res.source || null, title: res.title || '站内阅读', revision: '', nextCursor: null, page: 1, listHidden: false,
          status: res.status, notice: res.notice || STATUS[res.status] || '内容尚未完成处理。', scannedPages: res.scannedPages || 0, complete: false });
        return;
      }
      if (typeof res.revision !== 'string' || !res.revision || revision && revision !== res.revision) {
        throw Object.assign(new Error('内容版本已变化，请重新阅读'), { status: 409 });
      }
      const segments = segmentsOf(res.segments || []);
      const next = res.nextCursor == null ? null : String(res.nextCursor);
      if (next !== null && (!next || this.cursors.slice(0, this.cursorIndex + 1).includes(next))) throw new Error('阅读分页信息有误，请重试');
      this.setData({ title: res.title || '站内阅读', source: res.source || {}, revision: res.revision,
        status: res.status, listHidden: res.listHidden === true, notice: res.notice || STATUS[res.status] || (res.status === 'ok' ? '' : '内容尚未完成处理。'),
        segments, nextCursor: next, page: this.cursorIndex + 1, totalSegments: res.totalSegments || 0,
        scannedPages: res.scannedPages || 0, complete: res.status === 'ok' && next === null && segments.length > 0 });
    }).catch((err) => {
      if (!current()) return;
      const changed = err.status === 409;
      if (changed || [403, 404, 410].includes(err.status)) {
        this.cursors = ['']; this.cursorIndex = 0;
        this.setData({ source: null, revision: '', nextCursor: null, segments: [], page: 1 });
      }
      this.setData({ error: changed ? '内容已更新，本次阅读已停止，请重新加载，避免混用不同版本。' : err.message || '内容未加载，请重试', changed });
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
    if (this.data.loading || this.data.nextCursor === null) return;
    this.cursors = this.cursors.slice(0, this.cursorIndex + 1);
    this.cursors.push(this.data.nextCursor); this.cursorIndex += 1;
    return this.loadPage();
  },
  previous() {
    if (this.data.loading || this.cursorIndex < 1) return;
    this.cursorIndex -= 1; return this.loadPage();
  },
  retry() {
    if (this.listMode) return this.loadRecord();
    return this.data.changed ? this.restart() : this.loadPage();
  },
  feedback() {
    const id = this.listMode ? (this.data.record && this.data.record.docId || '') : this.docId;
    const title = this.listMode ? (this.data.record && this.data.record.docTitle || '') : this.data.title;
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
