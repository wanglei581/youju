const api = require('../../utils/api.js');
const { dueInfo, cnDate, weekday, kindClass } = require('../../utils/format.js');
const { copyLink } = require('../../utils/copy.js');
const { requestOnce } = require('../../utils/subscribe.js');
const { exportDocs } = require('../../utils/exporter.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');

Page({
  data: { doc: null, due: null, dueDate: '', dueWeek: '', changes: [], tagClasses: [], reminded: false, error: '' },

  onLoad(query) {
    api.detail(query.id)
      .then((doc) => {
        // 只有人工核对过的截止日期才显示（合规红线）
        const due = dueInfo(doc.deadline);
        this.setData({
          doc,
          due,
          dueDate: due ? cnDate(doc.deadline.date) : '',
          dueWeek: due ? `${weekday(doc.deadline.date)}${doc.deadline.time ? ` ${doc.deadline.time}` : ''}` : '',
          tagClasses: (doc.tags || []).map((t) => kindClass(t)),
          // 已变更的截止（延期、调整、撤销）：不再当作有效截止，写明原因并链接到新通知
          changes: (doc.deadline_changes || []).map((c) => Object.assign({}, c, {
            dateText: `${cnDate(c.date)}${c.time ? ` ${c.time}` : ''}`,
          })),
        });
      })
      .catch((err) => this.setData({ error: err.message }));
  },

  // 跨日返回页面时重算剩余天数
  onShow() {
    this.recompute();
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
      wx.showToast({ title: '不到 3 天就截止了，请尽快办理', icon: 'none' });
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

  favorite() {
    wx.showToast({ title: '收藏功能即将开放', icon: 'none' });
  },

  // 导出本份：Excel 或 Word，含原文链接和数据说明
  exportDoc() {
    if (this.data.doc) exportDocs([this.data.doc.id]);
  },

  feedback() {
    const doc = this.data.doc;
    if (!doc) return;
    wx.navigateTo({ url: `/pages/feedback/index?id=${doc.id}&title=${encodeURIComponent(doc.title)}` });
  },
});
