const api = require('../../utils/api.js');
const { ensureConsent, syncConsent, loginOpen } = require('../../utils/consent.js');
const { exportWord } = require('../../utils/exporter.js');

// 我的保存（D-123）：原网页打不开、7 天后清理的通知，用户保存的正文和附件文字（存在服务器上，清理以后
// 还能看、能导出 Word；删除或注销时一起删）。两种状态：gated 未登录；live 列表。
// 协议定稿前（loginOpen 为 false，D-056）只显示"暂未开放"，不请求接口
const MAX_DEFAULT = 200;

function textOf(value) {
  return typeof value === 'string' ? value : '';
}

function short(title) {
  return title.length > 30 ? `${title.slice(0, 30)}…` : title;
}

// 字数：一万以上写"约 4.8 万字"
function charsText(chars) {
  if (chars >= 10000) return `约 ${(Math.round(chars / 1000) / 10).toFixed(1)} 万字`;
  return `约 ${chars} 字`;
}

function view(raw) {
  const item = raw && typeof raw === 'object' ? raw : {};
  const chars = Number.isInteger(item.chars) && item.chars > 0 ? item.chars : 0;
  return {
    id: String(item.id == null ? '' : item.id),
    docId: String(item.docId == null ? '' : item.docId),
    title: textOf(item.title) || '（没有标题）',
    meta: [textOf(item.issuer), textOf(item.date)].filter(Boolean).join(' · '),
    // 服务端写好的那句：原网页哪天起打不开、以官方为准、哪天保存的（D-123 ③：标明原网页撤下的日期）
    note: textOf(item.note),
    cleaned: item.cleaned === true,
    state: item.cleaned === true
      ? '原通知已清理，这里是保存的副本'
      : `${charsText(chars)}${item.truncated === true ? '（太长，后面的附件没有存全）' : ''}`,
  };
}

Page({
  data: { mode: 'live', loading: true, error: '', items: [], max: MAX_DEFAULT, loginOpen: loginOpen() },

  onShow() {
    if (!this.data.loginOpen) return;
    this.load();
  },

  load() {
    this.setData({ loading: true, error: '' });
    if (!api.isLive()) {
      this.setData({ mode: 'live', loading: false, items: [], error: '演示数据：连上接口并登录后，这里显示你保存的通知' });
      return Promise.resolve();
    }
    // 换了设备、本机没有同意记录时，先向服务端核对一次
    return syncConsent().then((ok) => {
      if (!ok) {
        this.setData({ mode: 'gated', loading: false, items: [] });
        return undefined;
      }
      return this.loadLive();
    }).catch((err) => this.setData({ loading: false, error: `加载失败：${err.message}` }));
  },

  loadLive() {
    const seq = this.seq = (this.seq || 0) + 1;
    this.setData({ mode: 'live', loading: true, error: '' });
    return api.savedList().then((res) => {
      if (seq !== this.seq) return;
      const items = (res && Array.isArray(res.items) ? res.items : []).map(view).filter((item) => /^\d+$/.test(item.id));
      const max = res && Number.isInteger(res.max) && res.max > 0 ? res.max : MAX_DEFAULT;
      this.setData({ items, max, loading: false });
    }).catch((err) => {
      if (seq !== this.seq) return;
      if (err.status === 403) { this.setData({ mode: 'gated', loading: false, items: [] }); return; }
      this.setData({ loading: false, error: `加载失败：${err.message}` });
    });
  },

  enable() {
    return ensureConsent({ purpose: 'save' }).then((ok) => { if (ok) this.load(); });
  },

  pick(e) {
    const id = String((e && e.currentTarget && e.currentTarget.dataset && e.currentTarget.dataset.id) || '');
    return this.data.items.find((item) => item.id === id) || null;
  },

  // 用阅读页翻页看保存的正文和附件
  read(e) {
    const item = this.pick(e);
    if (item) wx.navigateTo({ url: `/pages/reader/index?saved=${encodeURIComponent(item.id)}` });
  },

  exportOne(e) {
    const item = this.pick(e);
    if (!item) return Promise.resolve(false);
    return exportWord(() => api.exportSaved(item.id), { label: short(item.title), source: 'saved', savedId: item.id, docId: item.docId });
  },

  remove(e) {
    const item = this.pick(e);
    if (!item) return;
    wx.showModal({
      title: '删除保存的通知',
      content: item.cleaned ? '原通知已经清理，删除后就看不到了。确定删除？' : '确定删除这份保存的通知？',
      confirmText: '删除',
      confirmColor: '#E5484D',
      success: (res) => {
        if (!res.confirm) return;
        api.deleteSaved(item.id).then(() => {
          wx.showToast({ title: '已删除', icon: 'none' });
          this.loadLive();
        }, (err) => wx.showToast({ title: (err && err.message) || '没能删除，请稍后再试', icon: 'none' }));
      },
    });
  },
});
