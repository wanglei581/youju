const api = require('../../utils/api.js');

Page({
  data: { files: [] },

  onLoad() {
    api.files().then((files) => this.setData({ files }));
  },

  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 2 });
  },

  // 电脑版微信：发到"文件传输助手"后在电脑上另存；手机：转发给同事或客户
  sendToPc() {
    wx.showToast({ title: '导出接口就绪后可用', icon: 'none' });
  },

  forward() {
    wx.showToast({ title: '导出接口就绪后可用', icon: 'none' });
  },

  reexport() {
    wx.showToast({ title: '导出接口就绪后可用', icon: 'none' });
  },

  login() {
    wx.switchTab({ url: '/pages/me/index' });
  },
});
