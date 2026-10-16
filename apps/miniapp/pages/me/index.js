const legal = require('../../content/legal.js');
const api = require('../../utils/api.js');
const { applyServer, consentEpoch, withdraw, markAgreed, loginOpen, openPrivacy, CONSENT_PAGE, agreeFailed, featureNames } = require('../../utils/consent.js');
const { remindOpen } = require('../../utils/subscribe.js');
const { loadCoverage, notCoveredNames } = require('../../utils/coverage.js');

Page({
  data: {
    // 协议定稿前（D-056）不显示登录卡片、纠错和申请覆盖，也不查询登录状态；定稿后照旧
    loginOpen: loginOpen(),
    // 订阅消息模板申请下来之前，不写"提醒"相关的字样
    remindOpen: remindOpen('newDocs') || remindOpen('deadline'),
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
    profileLoading: true,
    profileError: '',
    followCount: 0,
    reminderCount: 0,
    // "我的订单"：支付在售、或有过订单时才显示（服务端 /me 的 pay；没开放支付时不放空入口，D-068）
    ordersEntry: false,
    // 数据从哪里来：取自收录范围接口（演示时是演示数据）；取到之前不显示数字
    stats: null,
    notCovered: '',
  },

  onLoad() {
    const { statusBarHeight, navHeight, menuWidth } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight, menuWidth });
  },

  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 3 });
    if (api.watchStartup) api.watchStartup(this);
    this.refresh();
    this.loadStats();
  },

  onHide() {
    if (api.stopStartup) api.stopStartup(this);
  },

  onUnload() {
    if (api.stopStartup) api.stopStartup(this);
  },

  loadStats() {
    loadCoverage().then((c) => {
      if (c) this.setData({ stats: { sources: c.sources, provinces: c.provinces, cities: (c.cities || []).length, districts: (c.districts || []).length }, notCovered: notCoveredNames(c) });
    });
  },

  // 以服务端记录为准：换了设备也能恢复登录状态；服务端没有记录（如在别处注销过）或协议更新了，清掉本机记录。
  // 查询 /me 不会建用户记录。
  refresh() {
    if (!api.isLive() || !this.data.loginOpen) {
      this.setData({ signedIn: false, hasRecord: false, outdated: false, profileLoading: false, profileError: '', ordersEntry: false });
      return;
    }
    const started = consentEpoch();
    const seq = (this.profileSeq || 0) + 1;
    this.profileSeq = seq;
    this.setData({ profileLoading: true, profileError: '' });
    api.me()
      .then((res) => {
        // 查询发出后本机又同意或注销过：结果已过时，等下一次刷新
        if (started !== consentEpoch() || seq !== this.profileSeq) return;
        // 与关注页等共用同一份状态：旧版本同意会标记"协议已更新"，弹窗只能先读全文
        const current = applyServer(res.agreed, started) && loginOpen();
        const subs = res.subscriptions || {};
        this.setData({
          profileLoading: false,
          signedIn: current,
          hasRecord: !!res.agreed,
          outdated: !!res.agreed && !current,
          followCount: res.follows,
          reminderCount: (subs.newDocs || 0) + (subs.deadline || 0),
          ordersEntry: !!(res.pay && (res.pay.enabled || res.pay.hasOrders)),
        });
      })
      .catch((err) => {
        if (started !== consentEpoch() || seq !== this.profileSeq) return;
        this.setData({ profileLoading: false, profileError: `登录状态加载失败：${err.message || '请稍后再试'}` });
      });
  },

  // 登录前必须主动勾选同意协议（agy 合规意见；微信隐私协议开发指南要求用户明确同意）
  toggleAgree() {
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
        wx.showToast({ title: `已登录，${featureNames()}可以用了`, icon: 'none' });
        this.refresh();
      })
      .catch(agreeFailed);
  },

  // 注销：撤回同意，删除服务端的关注、提醒、纠错等全部数据
  deleteAccount() {
    withdraw().then((ok) => {
      if (ok) this.setData({ signedIn: false, hasRecord: false, outdated: false, agreed: false, nudge: false, followCount: 0, reminderCount: 0, profileLoading: false, profileError: '' });
    });
  },

  myData() {
    wx.navigateTo({ url: '/pages/my-data/index' });
  },

  openAgreement(e) {
    wx.navigateTo({ url: `/pages/agreement/index?type=${e.currentTarget.dataset.type}` });
  },

  // 协议定稿前（D-056）：打开小程序后台填写的官方隐私保护指引，不显示草案
  openPrivacy() {
    openPrivacy();
  },

  go(e) {
    const { url, tab } = e.currentTarget.dataset;
    if (tab) wx.switchTab({ url });
    else wx.navigateTo({ url });
  },

  feedback() {
    wx.navigateTo({ url: '/pages/feedback/index' });
  },

  // 申请覆盖：用纠错页提交想增加的地区和栏目
  coverage() {
    wx.navigateTo({ url: '/pages/feedback/index?type=coverage' });
  },
});
