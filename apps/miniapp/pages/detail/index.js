const api = require('../../utils/api.js');
const { dueInfo, cnDate, weekday, kindClass } = require('../../utils/format.js');
const { copyLink } = require('../../utils/copy.js');
const { requestOnce } = require('../../utils/subscribe.js');
const { remindedDocs } = require('../../utils/reminders.js');
const { exportDocs } = require('../../utils/exporter.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');

// 正文默认显示的字数：在这个长度附近的换行处截断，读起来不断在半句
const BODY_PREVIEW = 600;

function previewOf(text) {
  if (text.length <= BODY_PREVIEW) return text;
  const cut = text.lastIndexOf('\n', BODY_PREVIEW);
  return `${text.slice(0, cut > BODY_PREVIEW - 200 ? cut : BODY_PREVIEW)}……`;
}

Page({
  data: { doc: null, due: null, dueDate: '', dueWeek: '', changes: [], tagClasses: [], reminded: false, error: '',
    bodyOpen: false, bodyFolded: false, bodyPreview: '', bodyRest: 0 },

  onLoad(query) {
    this.docId = query.id;
    this.load();
  },

  // 网络不好加载失败时，页面上点"重试"再加载一次
  load() {
    this.setData({ error: '' });
    api.detail(this.docId)
      .then((doc) => {
        // 只有人工核对过的截止日期才显示（合规红线）
        const due = dueInfo(doc.deadline);
        const body = doc.body && doc.body.status === 'ok' ? doc.body : null;
        this.setData({
          doc,
          // 正文默认只显示开头一段；比这长的才给"展开全文"
          bodyOpen: false,
          bodyFolded: !!body && body.text.length > BODY_PREVIEW,
          bodyPreview: body ? previewOf(body.text) : '',
          bodyRest: body ? body.total - body.text.length : 0,
          due,
          dueDate: due ? cnDate(doc.deadline.date) : '',
          dueWeek: due ? `${weekday(doc.deadline.date)}${doc.deadline.time ? ` ${doc.deadline.time}` : ''}` : '',
          tagClasses: (doc.tags || []).map((t) => kindClass(t)),
          // 已变更的截止（延期、调整、撤销）：不再当作有效截止，写明原因并链接到新通知
          changes: (doc.deadline_changes || []).map((c) => Object.assign({}, c, {
            dateText: `${cnDate(c.date)}${c.time ? ` ${c.time}` : ''}`,
          })),
        });
        this.syncReminded();
      })
      .catch((err) => this.setData({ error: err.message }));
  },

  toggleBody() {
    this.setData({ bodyOpen: !this.data.bodyOpen });
  },

  // 这份文件有没有还没发出的截止提醒（以服务端为准）
  syncReminded() {
    const { doc } = this.data;
    if (!doc) return;
    remindedDocs().then((map) => this.setData({ reminded: !!map[doc.id] }));
  },

  // 跨日返回页面时重算剩余天数
  onShow() {
    this.recompute();
    this.syncReminded();
    watchDay(this, () => this.recompute());
  },

  onHide() {
    stopDay(this);
  },

  onUnload() {
    stopDay(this);
  },

  openNew(e) {
    const id = e.currentTarget.dataset.id;
    if (id) wx.navigateTo({ url: `/pages/detail/index?id=${id}` });
  },

  recompute() {
    if (this.data.doc) this.setData({ due: dueInfo(this.data.doc.deadline) });
  },

  // 截止提醒：一次性订阅消息，用户在微信弹窗里允许后才算订阅成功
  toggleRemind() {
    if (this.data.reminded) {
      wx.showToast({ title: '已订阅：截止前 3 天提醒你一次', icon: 'none' });
      return;
    }
    const { doc } = this.data;
    const due = dueInfo(doc && doc.deadline);
    this.setData({ due });
    if (!due || due.days < 3) {
      wx.showToast({ title: '离截止不到 3 天，来不及提前提醒了', icon: 'none' });
      return;
    }
    requestOnce('deadline', { type: 'deadline', docId: doc.id }).then((ok) => {
      if (!ok) return;
      this.setData({ reminded: true });
      wx.showToast({ title: '已订阅截止提醒', icon: 'none' });
    });
  },

  copy() {
    copyLink(this.data.doc && this.data.doc.url);
  },

  // 导出本份：Excel 或 Word，含原文链接和数据说明
  exportDoc() {
    const { doc } = this.data;
    if (doc) exportDocs([doc.id], { label: doc.title.length > 30 ? `${doc.title.slice(0, 30)}…` : doc.title });
  },

  feedback() {
    const doc = this.data.doc;
    if (!doc) return;
    wx.navigateTo({ url: `/pages/feedback/index?id=${doc.id}&title=${encodeURIComponent(doc.title)}` });
  },
});
