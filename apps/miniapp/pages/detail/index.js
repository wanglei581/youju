const api = require('../../utils/api.js');
const { dueInfo, cnDate, weekday, kindClass } = require('../../utils/format.js');
const { copyLink } = require('../../utils/copy.js');
const { requestOnce } = require('../../utils/subscribe.js');

Page({
  data: { doc: null, due: null, dueDate: '', dueWeek: '', tagClasses: [], reminded: false, error: '' },

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
        });
      })
      .catch((err) => this.setData({ error: err.message }));
  },

  // 截止提醒：一次性订阅消息，用户在微信弹窗里允许后才算订阅成功
  toggleRemind() {
    if (this.data.reminded) {
      wx.showToast({ title: '已订阅：截止前 3 天提醒你一次', icon: 'none' });
      return;
    }
    requestOnce('deadline').then((ok) => {
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

  exportDoc() {
    wx.switchTab({ url: '/pages/files/index' });
  },

  feedback() {
    wx.showToast({ title: '纠错入口即将开放', icon: 'none' });
  },
});
