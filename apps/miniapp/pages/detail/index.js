const api = require('../../utils/api.js');
const { dueTone, kindClass } = require('../../utils/format.js');
const { copyLink } = require('../../utils/copy.js');

Page({
  data: { doc: null, tone: '', tagClasses: [], reminded: false, error: '' },

  onLoad(query) {
    api.detail(query.id)
      .then((doc) => {
        this.setData({
          doc,
          tone: doc.deadline ? dueTone(doc.deadline.days) : '',
          tagClasses: (doc.tags || []).map((t) => kindClass(t)),
        });
      })
      .catch((err) => this.setData({ error: err.message }));
  },

  // 截止提醒：正式版用一次性订阅消息，截止前 3 天提醒一次
  toggleRemind() {
    const reminded = !this.data.reminded;
    this.setData({ reminded });
    wx.showToast({ title: reminded ? '已订阅截止提醒' : '已取消', icon: 'none' });
  },

  copy() {
    copyLink(this.data.doc && this.data.doc.url);
  },

  favorite() {
    wx.showToast({ title: '已收藏', icon: 'success' });
  },

  exportDoc() {
    wx.switchTab({ url: '/pages/files/index' });
  },

  feedback() {
    wx.showToast({ title: '纠错入口即将开放', icon: 'none' });
  },
});
