const api = require('../../utils/api.js');

Page({
  data: { weekStart: '', newTotal: 0, subs: [], changes: [], subscribed: false },

  onLoad() {
    api.follow().then((res) => this.setData({ weekStart: res.week_start, newTotal: res.new_total, subs: res.subs, changes: res.changes }));
  },

  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 1 });
  },

  // 一次性订阅：用户每点一次，有新文件时用微信提醒一次（D-017、决策记录 D-014 合规要求）
  subscribeOnce() {
    this.setData({ subscribed: true });
    wx.showToast({ title: '已订阅 1 次，有新文件时提醒你', icon: 'none' });
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
