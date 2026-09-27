const legal = require('../../content/legal.js');
const api = require('../../utils/api.js');
const { applyServer, consentEpoch, withdraw, markAgreed, CONSENT_PAGE } = require('../../utils/consent.js');

Page({
  data: {
    legalFinal: legal.final,
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
    agreed: false,
    nudge: false,
    // 已在服务端同意当前版本的协议（相当于"登录"：之后关注、提醒、纠错可用）
    signedIn: false,
    // 服务端有这个用户的记录（包括同意的是旧版本）：可以注销
    hasRecord: false,
    // 同意的是旧版本：协议更新了，要重新阅读并同意
    outdated: false,
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

  // 以服务端记录为准：换了设备也能恢复登录状态；服务端没有记录（如在别处注销过）或协议更新了，清掉本机记录。
  // 查询 /me 不会建用户记录。
  refresh() {
    if (!api.isLive()) {
      this.setData({ signedIn: false, hasRecord: false, outdated: false });
      return;
    }
    const started = consentEpoch();
    api.me()
      .then((res) => {
        // 查询发出后本机又同意或注销过：结果已过时，等下一次刷新
        if (started !== consentEpoch()) return;
        // 与关注页等共用同一份状态：旧版本同意会标记"协议已更新"，弹窗只能先读全文
        const current = applyServer(res.agreed, started) && legal.final;
        const subs = res.subscriptions || {};
        this.setData({
          signedIn: current,
          hasRecord: !!res.agreed,
          outdated: !!res.agreed && !current,
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
    // 协议更新后：先读更新后的全文，在全文最后同意
    if (this.data.outdated) {
      wx.navigateTo({ url: CONSENT_PAGE });
      return;
    }
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
        markAgreed();
        wx.showToast({ title: '已登录，关注和微信提醒可以用了', icon: 'none' });
        this.refresh();
      })
      .catch((err) => wx.showToast({ title: err.message || '网络不太好，请稍后再试', icon: 'none' }));
  },

  // 注销：撤回同意，删除服务端的关注、提醒、纠错等全部数据
  deleteAccount() {
    withdraw().then((ok) => {
      if (ok) this.setData({ signedIn: false, hasRecord: false, outdated: false, agreed: false, followCount: 0, reminderCount: 0 });
    });
  },

  myData() {
    wx.navigateTo({ url: '/pages/my-data/index' });
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
