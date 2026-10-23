const api = require('../../utils/api.js');

const STATUS = {
  partial: '内容尚不完整，请勿据此认定已读到全部原文。', scan: '扫描页尚未形成可阅读内容。',
  empty: '暂未取得可阅读内容。', unavailable: '这份内容暂不可用。',
  list: '名单仅提供已提取的企业查询结果，暂不提供完整名单。', unsupported: '该格式暂不能在站内阅读。',
};
// 不把服务端字符串解释成 HTML；表格按实际单元格排列，不推测页码或字段。
function segmentsOf(items) {
  if (!Array.isArray(items) || items.length > 200) throw new Error('阅读内容格式有误，请重试');
  return items.map((s) => {
    if (!s || typeof s.text !== 'string' && !Array.isArray(s.cells)) throw new Error('阅读内容格式有误，请重试');
    const cells = Array.isArray(s.cells) ? s.cells : [];
    if (cells.length > 100 || cells.some((cell) => !cell || typeof cell.t !== 'string')) throw new Error('表格内容格式有误');
    const labels = { page: '第', sheet: '工作表', row: '行', table: '表', slide: '幻灯片' };
    const loc = s.loc && typeof s.loc === 'object' ? Object.keys(labels).filter((key) => s.loc[key] != null)
      .map((key) => `${labels[key]} ${s.loc[key]}${key === 'page' ? ' 页' : ''}`).join(' · ') : '';
    return { n: s.n, kind: s.kind, text: s.text || '', loc,
      cells: cells.map((cell) => ({ c: cell.c, t: cell.t, h: typeof cell.h === 'string' ? cell.h : '' })) };
  });
}
Page({
  data: { loading: false, error: '', changed: false, source: null, title: '', revision: '', status: '', notice: '',
    segments: [], nextCursor: null, page: 1, totalSegments: 0, scannedPages: 0, complete: false },
  onLoad(q) {
    this.docId = String(q.id || ''); this.attachmentId = q.attachment || '';
    this.cursors = ['']; this.cursorIndex = 0;
  },
  onShow() {
    this.visible = true;
    if (api.watchStartup) api.watchStartup(this);
    // 从后台或其他页面回来重新验证，旧内容不能在撤回后继续当作有效内容展示。
    return this.restart();
  },
  onHide() {
    this.visible = false; this.seq = (this.seq || 0) + 1;
    if (api.stopStartup) api.stopStartup(this);
    this.setData({ segments: [], source: null, loading: false, complete: false });
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
        this.setData({ segments: [], source: res.source || null, title: res.title || '站内阅读', revision: '', nextCursor: null, page: 1,
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
        status: res.status, notice: res.notice || STATUS[res.status] || (res.status === 'ok' ? '' : '内容尚未完成处理。'),
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
  retry() { return this.data.changed ? this.restart() : this.loadPage(); },
  feedback() { wx.navigateTo({ url: `/pages/feedback/index?id=${encodeURIComponent(this.docId)}&title=${encodeURIComponent(this.data.title)}` }); },
  lookup() { wx.navigateTo({ url: '/pages/lists/index' }); },
  detail() { wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(this.docId)}` }); },
});
