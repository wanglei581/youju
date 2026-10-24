const legal = require('../../content/legal.js');
const api = require('../../utils/api.js');
const { applyServer, consentEpoch, withdraw, markAgreed, loginOpen, openPrivacy, CONSENT_PAGE, agreeFailed, featureNames } = require('../../utils/consent.js');
const { remindOpen } = require('../../utils/subscribe.js');
const { loadCoverage, notCoveredNames } = require('../../utils/coverage.js');
// 支付的工具函数用到时才取（和原来"批量工具"一行的小字一样）：没买过、没在卖的人不加载
function payUtils() { return require('../../utils/pay.js'); }
// 去购买页那一行的名字（D-076 ②，10/22 写明是买）
const BUY = '买批量工具';
const RENEW = '续买批量工具';

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
    // "我的订单"：有过订单才显示（服务端 /me 的 pay；停卖后已付款的人照样能看订单、退款、开票）
    ordersEntry: false,
    // 去购买页的一行：在售时所有人都显示（D-076，原来只给买过的人续买）；没在售时不显示，正式版开卖前看不到。
    // 10/22 起写明是买（九宫格的"批量工具"是用工具的那一页）：使用期内写"续买批量工具"，其余写"买批量工具"。
    // passHint：没买过写最便宜一档的价格（"30 天 29 元起"），使用期内写到期日，过期了写"使用期已到期"；取不到就不写
    passEntry: false,
    passLabel: BUY,
    passHint: '',
    // 页头"批量工具"标识：使用期内才有（买的、送的都算，不看在不在卖），写到哪天；点了去购买页（在售时）或订单页
    passBadge: '',
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
      this.setData({ signedIn: false, hasRecord: false, outdated: false, profileLoading: false, profileError: '', ordersEntry: false, passEntry: false, passLabel: BUY, passHint: '', passBadge: '' });
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
          ordersEntry: !!(res.pay && res.pay.hasOrders === true),
          passEntry: !!(res.pay && res.pay.enabled === true),
          passLabel: res.pass && res.pass.active === true ? RENEW : BUY,
          passHint: '',
          passBadge: current && res.pass && res.pass.active === true ? payUtils().passBadge(res.pass) : '',
        });
        if (res.pay && res.pay.enabled === true) this.loadPassHint(seq, res.pay.hasOrders === true, res.pass);
      })
      .catch((err) => {
        if (started !== consentEpoch() || seq !== this.profileSeq) return;
        // 确认不了还在不在卖：先收起"批量工具"，不在可能已停卖时留着购买入口（D-074，Codex 审阅）
        this.setData({ profileLoading: false, profileError: `登录状态加载失败：${err.message || '请稍后再试'}`, passEntry: false, passLabel: BUY, passHint: '', passBadge: '' });
      });
  },

  // 去购买页那一行的小字：使用期内写到期日，用完了写"使用期已到期"（/me 带的使用期；老的服务端
  // 没带时读 /advisor/status，名字也跟着它写"续买"或"买"）；没买过、或者只有没开通的订单（待确认、已关闭）写最便宜一档（读商品接口）。
  // 都是在售时才调用，取不到就不写，不影响点进去
  loadPassHint(seq, bought, pass) {
    const pay = payUtils();
    const done = (hint, label) => { if (seq === this.profileSeq && this.data.passEntry) this.setData(label ? { passHint: hint, passLabel: label } : { passHint: hint }); };
    if (pass && pass.active === true && pay.monthDay(pass.expiresAt)) { done(`使用期到 ${pay.monthDay(pass.expiresAt)}`); return; }
    if (pass && pass.expired === true) { done('使用期已到期'); return; }
    if (bought && !pass) {
      Promise.resolve().then(() => api.advisorStatus()).then((status) => {
        const pass = status && status.pass;
        const active = !!(pass && pass.active && pay.monthDay(pass.expiresAt));
        done(active ? `使用期到 ${pay.monthDay(pass.expiresAt)}` : '使用期已到期', active ? RENEW : BUY);
      }).catch(() => done(''));
      return;
    }
    Promise.resolve().then(() => api.payProducts()).then((res) => {
      const cheapest = pay.normalizeProducts(res)[0];
      done(cheapest ? `${cheapest.days} 天 ${pay.yuan(cheapest.price)} 元起` : '');
    }).catch(() => done(''));
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

  // 页头"批量工具"标识：在售时去购买页（看到期日、续买），不在售时去订单页；
  // 只有送的使用期、又没在卖的（没有订单）去批量工具页用工具（Codex 复核：不能点了没反应）
  openPass() {
    if (this.data.passEntry) wx.navigateTo({ url: '/pages/pass/index' });
    else if (this.data.ordersEntry) wx.navigateTo({ url: '/pages/orders/index' });
    else wx.navigateTo({ url: '/pages/advisor/index' });
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
