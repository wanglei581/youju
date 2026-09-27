const api = require('../../utils/api.js');
const { todayISO } = require('../../utils/format.js');
const { requestOnce } = require('../../utils/subscribe.js');

Page({
  data: { weekStart: '', newTotal: 0, subs: [], changes: [], subscribed: false },

  onLoad() {
    api.follow().then((res) => this.setData({ weekStart: res.week_start, newTotal: res.new_total, subs: res.subs, changes: res.changes }));
  },

  onShow() {
    this.setData({ today: todayISO() });
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 1 });
  },

  // 一次性订阅：用户在微信弹窗里允许后才算订阅成功，每次订阅提醒一次
  subscribeOnce() {
    requestOnce('newDocs', { type: 'follow' }).then((ok) => {
      if (!ok) return;
      this.setData({ subscribed: true });
      wx.showToast({ title: '已订阅 1 次，有新文件时提醒你', icon: 'none' });
    });
  },

  addSub() {
    wx.navigateTo({ url: '/pages/search/index' });
  },

  openSub() {
    wx.navigateTo({ url: '/pages/search/index' });
  },

  login() {
    wx.switchTab({ url: '/pages/me/index' });
  },
});
