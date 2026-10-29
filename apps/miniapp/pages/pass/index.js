const api = require('../../utils/api.js');
const pay = require('../../utils/pay.js');
const payTerms = require('../../content/pay-terms.js');
const { loginOpen } = require('../../utils/consent.js');
const { toolConsent } = require('../../utils/data-tools.js');

// 付款后还没确认完的订单记在本机。有它在就不下新单，先接着查它：
// 确认超时后再点"支付"，不能变成重复扣款（Codex 复审，10/10）。
// 48 小时内有效——服务端超过 48 小时还确认不了会关单。
const UNSETTLED_KEY = 'pay.unsettled';
const UNSETTLED_TTL = 48 * 3600 * 1000;
// 用户取消支付后只再查几秒（防取消回调不准），不用等满一分钟
const CANCEL_CHECK = 6000;
// 付款后切到后台，回来时截止时间可能已过：至少再查这么久，不直接判超时（agy 审阅，10/16）
const RESUME_CHECK = 10000;
function readUnsettled() {
  try {
    const saved = wx.getStorageSync(UNSETTLED_KEY);
    return saved && saved.orderId && Date.now() - saved.at < UNSETTLED_TTL ? saved.orderId : null;
  } catch (e) { return null; }
}
function saveUnsettled(orderId) {
  try { wx.setStorageSync(UNSETTLED_KEY, { orderId, at: Date.now() }); } catch (e) { /* 存不了就只在本页内记 */ }
}
function clearUnsettled() {
  try { wx.removeStorageSync(UNSETTLED_KEY); } catch (e) { /* 忽略 */ }
}
// 没在卖（商品接口 404 或没有档位）时直接回首页，页面上不出现"暂未开放"之类的字样（D-074）：
// 首发包带着这一页，从旧链接或分享直接打开时，审核人员和用户都只会回到首页
function goHome() {
  wx.switchTab({ url: '/pages/home/index', fail: () => wx.reLaunch({ url: '/pages/home/index' }) });
}
// 停卖时还留在本页的情形：刚付过款、或还有一笔待确认——不送回首页，告诉他去订单页看
const CLOSED_NOTE = '现在没有在售的档位。已付款的订单可以在"我的 → 我的订单"查看。';

Page({
  // 没有沙箱了（创始人 10/29：付款一律现网、真扣钱）：协议定稿、写明了发票才显示价格和付款按钮
  // tiers：在售的档位（30 天、90 天，D-070），selected 是选中的那一档；product 是选中的那一档（下单用）
  // renew：续买多送的规则一句（和价格同一屏）；expires：现有使用期到哪天；soon：3 天内到期的提示
  // invoiceText：能开哪种发票（服务端商品接口带回）；没有时不能付款，服务端也不卖
  data: { loginOpen: loginOpen(), loading: true, error: '', tiers: [], selected: 0, product: null, price: '', renew: null, expires: '', soon: '',
    termsFinal: pay.termsFinal(payTerms), invoiceText: '',
    checked: false, busy: false, message: '', payError: false, confirmed: false, closedNote: '',
    // 付款结果没确认到、或转了人工：消息下面放"查看我的订单"
    ordersLink: false },
  onShow() {
    this.visible = true;
    if (!this.data.loginOpen) { goHome(); return; }
    if (this.payResume && this.poller) {
      this.payResume = false;
      this.payDeadline = Math.max(this.payDeadline || 0, Date.now() + RESUME_CHECK);
      this.poller.start(this.payDeadline);
    }
    if (!this.data.busy) this.load();
  },
  onHide() {
    this.visible = false;
    if (this.poller && this.poller.isActive()) { this.poller.stop(); this.payResume = true; }
  },
  onUnload() { this.onHide(); this.unloaded = true; this.payResume = false; this.loadSerial = (this.loadSerial || 0) + 1; },
  load() {
    if (!this.data.loginOpen) { goHome(); return; }
    const serial = this.loadSerial = (this.loadSerial || 0) + 1;
    this.setData({ loading: true, error: '', closedNote: '' });
    api.payProducts().then((res) => {
      if (serial !== this.loadSerial) return;
      const tiers = pay.normalizeProducts(res).map(pay.tierView);
      // 协议没定稿、没写明发票：付不了款，和没在卖一样回首页，不出现"暂未开放"（D-074 ⑧）。
      // 服务端只对带定稿协议版本号、配了发票的包卖，走到这里是配置不一致
      if (!tiers.length || !(this.data.termsFinal && pay.invoiceType(res))) { this.closed(); return; }
      // 重新载入时保留用户选的那一档（按天数认），没有了就回到第一档
      const keep = this.data.product ? tiers.findIndex((tier) => tier.days === this.data.product.days) : -1;
      const selected = keep >= 0 ? keep : 0;
      this.setData({ loading: false, tiers, selected, product: tiers[selected] || null, price: tiers[selected] ? tiers[selected].price : '',
        renew: pay.renewRule(res), invoiceText: pay.invoiceType(res) });
      if (tiers.length && this.data.loginOpen) api.advisorStatus({ refresh: true }).then((status) => {
        if (serial !== this.loadSerial) return;
        const active = !!(status.pass && status.pass.active);
        this.setData({ expires: active ? pay.date(status.pass.expiresAt) : '', soon: pay.expiryNotice(status.pass) });
      }).catch(() => { if (serial === this.loadSerial) this.setData({ expires: '', soon: '' }); });
    }).catch((err) => {
      if (serial !== this.loadSerial) return;
      if (err.status === 404) { this.closed(); return; }
      this.setData({ loading: false, tiers: [], product: null, price: '', renew: null, invoiceText: '', error: err.message });
    });
  },
  // 没在卖：刚开通的照旧显示开通结果；还有待确认的一笔时写去订单页看；页面已隐藏就不跳（回来时 onShow 会重新载入）；
  // 其余直接回首页（Codex 审阅）
  closed() {
    if (this.data.confirmed) { this.setData({ loading: false }); return; }
    if (this.unsettledId || readUnsettled()) {
      this.setData({ loading: false, tiers: [], product: null, price: '', renew: null, invoiceText: '', error: '', closedNote: CLOSED_NOTE });
      return;
    }
    this.setData({ loading: true, tiers: [], product: null, price: '', renew: null, invoiceText: '', error: '' });
    if (this.visible) goHome();
  },
  // 选一档：付款中、已开通时不能换
  pick(e) {
    if (this.data.busy || this.data.confirmed) return;
    const index = Number(e.currentTarget.dataset.index);
    const tier = this.data.tiers[index];
    if (!tier) return;
    this.setData({ selected: index, product: tier, price: tier.price, message: '', payError: false });
  },
  check(e) { this.setData({ checked: (e.detail.value || []).includes('agree') }); },
  terms() { wx.navigateTo({ url: '/pages/agreement/index?type=pay' }); },
  buy() {
    if (!this.data.product || this.data.busy || this.data.confirmed || !this.data.loginOpen) return;
    if (!(this.data.termsFinal && this.data.invoiceText)) { this.closed(); return; }
    if (!this.data.checked) { this.setData({ message: pay.TEXT.termsRequired, payError: false }); wx.showToast({ title: pay.TEXT.termsRequired, icon: 'none' }); return; }
    if (typeof wx.requestVirtualPayment !== 'function') { this.setData({ message: pay.TEXT.unsupported, payError: true }); return; }
    // 上一笔还没确认完：不下新单，接着查它
    const unsettled = this.unsettledId || readUnsettled();
    if (unsettled) { this.recheck(unsettled); return; }
    this.setData({ busy: true, message: pay.TEXT.ordering, payError: false, ordersLink: false });
    // 和工具一样：先同意《工具使用说明》（服务端下单也会检查）
    toolConsent().then((agreed) => {
      if (this.unloaded) return;
      if (!agreed) { this.setData({ busy: false, message: '' }); wx.navigateTo({ url: '/pages/advisor-terms/index' }); return; }
      return this.startPay();
    }).catch((err) => {
      if (this.unloaded) return;
      this.setData({ busy: false, message: err.message || pay.TEXT.orderFailed, payError: true });
      // 409：上一笔其实已付款、刚开通（服务端下单前先查了微信），或协议已更新——重新载入使用期和商品
      if (err && err.status === 409) this.load();
    });
  },
  watch(orderId) {
    this.orderId = orderId;
    this.poller = pay.createPoller({
      // 每次都请服务端当场向微信核对（付了款就开通），不只是读订单：推送晚到、没处理成时也不用等每分钟的补单
      // （10/19 沙箱实测：付了款，页面等了一分钟还是"待支付"）。
      // 查不到这张订单（比如换了账号）：当作已关闭，不让它一直挡着
      fetch: () => api.checkPayOrder(orderId).catch((err) => { if (err && err.status === 404) return { status: '已关闭' }; throw err; }),
      onDone: (result) => this.payDone(result), timers: { set: setTimeout, clear: clearTimeout }, now: () => Date.now(),
    });
  },
  recheck(orderId) {
    this.rechecking = true; this.cancelledPay = false;
    this.setData({ busy: true, message: pay.TEXT.confirming, payError: false, ordersLink: false });
    this.watch(orderId);
    this.payDeadline = Date.now() + pay.POLL_TIMEOUT;
    this.poller.start(this.payDeadline);
  },
  startPay() {
    return new Promise((resolve, reject) => {
      const fail = () => reject(new Error(pay.TEXT.login));
      wx.login({ success: (res) => res && res.code ? resolve(res.code) : fail(), fail });
    }).then((code) => {
      if (this.unloaded) return null;
      // terms_version：用户刚勾选同意的《付费服务协议》版本，随订单保存
      // invoice_type：页面上写的发票类型，服务端核对和现在的一致才下单（订单记下用户看到的那句）
      return api.createPayOrder({ product_id: this.data.product.productId, code, terms_version: payTerms.version, invoice_type: this.data.invoiceText });
    }).then((order) => {
      if (!order || this.unloaded) return;
      this.rechecking = false;
      this.watch(order.orderId);
      try {
        wx.requestVirtualPayment(Object.assign(pay.paymentParams(order), {
          success: () => this.afterPay(false), fail: (err) => this.afterPay(pay.isCancel(err)),
        }));
      } catch (err) { this.afterPay(pay.isCancel(err)); }
    });
  },
  afterPay(cancelled) {
    if (this.unloaded || this.payDeadline) return;
    this.cancelledPay = cancelled;
    // 没取消：可能已经扣款，记下这一笔，确认之前不再下新单
    if (!cancelled) { this.unsettledId = this.orderId; saveUnsettled(this.orderId); }
    this.payDeadline = Date.now() + (cancelled ? CANCEL_CHECK : pay.POLL_TIMEOUT);
    this.setData({ message: cancelled ? pay.TEXT.cancelled : pay.TEXT.confirming, payError: false });
    if (cancelled) wx.showToast({ title: pay.TEXT.cancelled, icon: 'none' });
    if (!this.visible) { this.payResume = true; return; }
    this.poller.start(this.payDeadline);
  },
  payDone(result) {
    this.payResume = false; this.payDeadline = null;
    const rechecking = this.rechecking; this.rechecking = false;
    // 订单有了明确结果（开通，或关闭、退款、转人工）才放开，超时不放开
    if (result.state !== 'timeout') { this.unsettledId = null; clearUnsettled(); }
    if (result.state === 'success') {
      const order = result.order;
      this.setData({ busy: false, confirmed: true, message: `已开通，使用期到 ${pay.date(order.passEndsAt)}`, payError: false });
      // 续购、退款造成的分段使用期，以状态接口的连续到期时间展示。
      api.advisorStatus({ refresh: true }).then((status) => {
        if (!this.unloaded && status.pass && status.pass.active) this.setData({ expires: pay.date(status.pass.expiresAt), soon: pay.expiryNotice(status.pass), message: `已开通，使用期到 ${pay.date(status.pass.expiresAt)}` });
      }).catch(() => {});
    } else if (result.state === 'timeout') {
      const message = this.cancelledPay ? pay.TEXT.cancelled : rechecking ? pay.TEXT.unsettled : pay.TEXT.timeout;
      // 没确认到结果（不是自己取消的）：给一个去订单页的入口，订单页可以"重新核对"
      this.setData({ busy: false, message, payError: false, ordersLink: !this.cancelledPay });
    } else {
      const status = result.order.status;
      const closed = status === '已关闭';
      // 转了人工：和订单页同一句说明（钱收到了没有、多久处理、能不能直接申请退款，按这张订单的实际情况写）
      const message = closed ? pay.TEXT.closed : status === '需人工处理' ? pay.stateNote(result.order) : status === '已退款' ? pay.TEXT.refunded : `订单状态：${status}`;
      this.setData({ busy: false, message, payError: !closed, ordersLink: !closed });
    }
  },
  orders() { wx.navigateTo({ url: '/pages/orders/index' }); },
  tools() { wx.navigateTo({ url: '/pages/advisor/index' }); },
  // 付款前要看得到：已收录哪些省、哪些城市（D-070）
  coverage() { wx.navigateTo({ url: '/pages/about/index' }); },
  feedback() { wx.navigateTo({ url: `/pages/feedback/index?type=pay${this.orderId ? '&orderId=' + encodeURIComponent(this.orderId) : ''}` }); },
});
