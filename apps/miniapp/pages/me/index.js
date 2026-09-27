const legal = require('../../content/legal.js');
const api = require('../../utils/api.js');
const { agreedHere, forget } = require('../../utils/consent.js');

Page({
  data: {
    legalFinal: legal.final,
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
    agreed: false,
    nudge: false,
    // 已在服务端同意协议（相当于"登录"：之后关注、提醒、纠错可用）
    signedIn: false,
    followCount: 0,
    reminderCount: 0,
  },

  onLoad() {
    const { statusBarHeight, navHeight, menuWidth } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight, menuWidth });
  },

  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 3 });
    this.refresh();
  },

  // 以服务端记录为准：本机记着已同意、但服务端没有记录（如在别处注销过）时，清掉本机记录
  refresh() {
    if (!api.isLive() || !agreedHere()) {
      this.setData({ signedIn: false });
      return;
    }
    api.me()
      .then((res) => {
        if (!res.agreed || res.agreed.version !== legal.version) {
          forget();
          this.setData({ signedIn: false });
          return;
        }
        const subs = res.subscriptions || {};
        this.setData({
          signedIn: true,
          followCount: res.follows,
          reminderCount: (subs.newDocs || 0) + (subs.deadline || 0),
        });
      })
      .catch(() => { /* 网络不好时保持原样 */ });
  },

  // 登录前必须主动勾选同意协议（agy 合规意见；微信隐私协议开发指南要求用户明确同意）
  toggleAgree() {
    if (!this.data.legalFinal) {
      wx.showToast({ title: '协议正在定稿，定稿后开放登录', icon: 'none' });
      return;
    }
    this.setData({ agreed: !this.data.agreed, nudge: false });
  },

  // 勾选同意后"微信登录"：服务端记录同意（小程序经云托管调用时平台自动带上用户标识，不需要另外授权）
  login() {
    if (!this.data.agreed) {
      this.setData({ nudge: true });
      return;
    }
    if (!api.isLive()) {
      wx.showToast({ title: '演示数据不能登录', icon: 'none' });
      return;
    }
    api.agree(legal.version)
      .then(() => {
        try { wx.setStorageSync(api.AGREED_KEY, legal.version); } catch (e) { /* 下次再同步 */ }
        wx.showToast({ title: '已登录，关注和微信提醒可以用了', icon: 'none' });
        this.refresh();
      })
      .catch((err) => wx.showToast({ title: err.message || '网络不太好，请稍后再试', icon: 'none' }));
  },

  // 注销：撤回同意，删除服务端的关注、提醒、纠错等全部数据
  deleteAccount() {
    wx.showModal({
      title: '注销并删除数据',
      content: '将撤回你对用户协议和隐私保护指引的同意，并删除你在有据的关注、微信提醒、纠错记录，删除后不能恢复。',
      confirmText: '注销',
      confirmColor: '#E5484D',
      success: (res) => {
        if (!res.confirm) return;
        api.deleteMe()
          .then(() => {
            this.setData({ signedIn: false, agreed: false, followCount: 0, reminderCount: 0 });
            wx.showToast({ title: '已注销，数据已删除', icon: 'none' });
          })
          .catch((err) => wx.showToast({ title: err.message || '注销失败，请稍后再试', icon: 'none' }));
      },
    });
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

  feedback() {
    wx.navigateTo({ url: '/pages/feedback/index' });
  },
});
