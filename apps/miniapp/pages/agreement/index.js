Page({
  data: { type: 'user' },
  onLoad(query) {
    this.setData({ type: query.type === 'privacy' ? 'privacy' : 'user' });
    wx.setNavigationBarTitle({ title: query.type === 'privacy' ? '隐私保护指引' : '用户协议' });
  },
});
