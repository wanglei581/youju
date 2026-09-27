Page({
  data: {
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
    agreed: false,
    nudge: false,
  },

  onLoad() {
    const { statusBarHeight, navHeight, menuWidth } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight, menuWidth });
  },

  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 3 });
  },

  // 登录前必须主动勾选同意协议（agy 合规意见；微信隐私协议开发指南要求用户明确同意）
  toggleAgree() {
    this.setData({ agreed: !this.data.agreed, nudge: false });
  },

  login() {
    if (!this.data.agreed) {
      this.setData({ nudge: true });
      return;
    }
    wx.showToast({ title: '登录接口就绪后可用', icon: 'none' });
  },

  openAgreement(e) {
    wx.navigateTo({ url: `/pages/agreement/index?type=${e.currentTarget.dataset.type}` });
  },

  go(e) {
    const { url, tab } = e.currentTarget.dataset;
    if (tab) wx.switchTab({ url });
    else wx.navigateTo({ url });
  },

  todo() {
    wx.showToast({ title: '即将开放', icon: 'none' });
  },
});
