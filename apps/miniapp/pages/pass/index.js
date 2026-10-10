const api = require('../../utils/api.js');
const pay = require('../../utils/pay.js');
const payTerms = require('../../content/pay-terms.js');
const { loginOpen } = require('../../utils/consent.js');
const { consent } = require('../../utils/advisor.js');

// 付款后还没确认完的订单记在本机。有它在就不下新单，先接着查它：
// 确认超时后再点"支付"，不能变成重复扣款（Codex 复审，10/10）。
// 48 小时内有效——服务端超过 48 小时还确认不了会关单。
const UNSETTLED_KEY = 'pay.unsettled';
const UNSETTLED_TTL = 48 * 3600 * 1000;
// 用户取消支付后只再查几秒（防取消回调不准），不用等满一分钟
const CANCEL_CHECK = 6000;
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

Page({
  data: { loginOpen: loginOpen(), loading: true, error: '', product: null, price: '', expires: '', termsFinal: pay.termsFinal(payTerms), checked: false, busy: false, message: '', payError: false, confirmed: false },
  onShow() {
    this.visible = true;
    if (this.payResume && this.poller) { this.payResume = false; this.poller.start(this.payDeadline); }
    if (!this.data.busy) this.load();
  },
  onHide() {
    this.visible = false;
    if (this.poller && this.poller.isActive()) { this.poller.stop(); this.payResume = true; }
  },
  onUnload() { this.onHide(); this.unloaded = true; this.payResume = false; this.loadSerial = (this.loadSerial || 0) + 1; },
  load() {
    const serial = this.loadSerial = (this.loadSerial || 0) + 1;
    this.setData({ loading: true, error: '' });
    api.payProducts().then((res) => {
      if (serial !== this.loadSerial) return;
      const product = pay.normalizeProducts(res)[0] || null;
      this.setData({ loading: false, product, price: product ? pay.yuan(product.price) : '' });
      if (product && this.data.loginOpen) api.advisorStatus({ refresh: true }).then((status) => {
        if (serial === this.loadSerial) this.setData({ expires: status.pass && status.pass.active ? pay.date(status.pass.expiresAt) : '' });
      }).catch(() => { if (serial === this.loadSerial) this.setData({ expires: '' }); });
    }).catch((err) => {
      if (serial !== this.loadSerial) return;
      this.setData({ loading: false, product: null, price: '', error: err.status === 404 ? '' : err.message });
    });
  },
  check(e) { this.setData({ checked: (e.detail.value || []).includes('agree') }); },
  terms() { wx.navigateTo({ url: '/pages/agreement/index?type=pay' }); },
  buy() {
    if (!this.data.product || this.data.busy || this.data.confirmed || !this.data.loginOpen) return;
    if (!this.data.termsFinal) { this.setData({ message: pay.TEXT.termsPending }); return; }
    if (!this.data.checked) { this.setData({ message: pay.TEXT.termsRequired, payError: false }); wx.showToast({ title: pay.TEXT.termsRequired, icon: 'none' }); return; }
    if (typeof wx.requestVirtualPayment !== 'function') { this.setData({ message: pay.TEXT.unsupported, payError: true }); return; }
    // 上一笔还没确认完：不下新单，接着查它
    const unsettled = this.unsettledId || readUnsettled();
    if (unsettled) { this.recheck(unsettled); return; }
    this.setData({ busy: true, message: pay.TEXT.ordering, payError: false });
    consent().then((agreed) => {
      if (this.unloaded) return;
      if (!agreed) { this.setData({ busy: false, message: '' }); wx.navigateTo({ url: '/pages/advisor-terms/index' }); return; }
      return this.startPay();
    }).catch((err) => { if (!this.unloaded) this.setData({ busy: false, message: err.message || pay.TEXT.orderFailed, payError: true }); });
  },
  watch(orderId) {
    this.orderId = orderId;
    this.poller = pay.createPoller({
      // 查不到这张订单（比如换了账号）：当作已关闭，不让它一直挡着
      fetch: () => api.payOrder(orderId).catch((err) => { if (err && err.status === 404) return { status: '已关闭' }; throw err; }),
      onDone: (result) => this.payDone(result), timers: { set: setTimeout, clear: clearTimeout }, now: () => Date.now(),
    });
  },
  recheck(orderId) {
    this.rechecking = true; this.cancelledPay = false;
    this.setData({ busy: true, message: pay.TEXT.confirming, payError: false });
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
      return api.createPayOrder({ product_id: this.data.product.productId, code, terms_version: payTerms.version });
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
        if (!this.unloaded && status.pass && status.pass.active) this.setData({ expires: pay.date(status.pass.expiresAt), message: `已开通，使用期到 ${pay.date(status.pass.expiresAt)}` });
      }).catch(() => {});
    } else if (result.state === 'timeout') {
      const message = this.cancelledPay ? pay.TEXT.cancelled : rechecking ? pay.TEXT.unsettled : pay.TEXT.timeout;
      this.setData({ busy: false, message, payError: false });
    } else {
      const closed = result.order.status === '已关闭';
      this.setData({ busy: false, message: closed ? pay.TEXT.closed : `订单状态：${result.order.status}`, payError: !closed });
    }
  },
  tools() { wx.navigateTo({ url: '/pages/advisor/index' }); },
  feedback() { wx.navigateTo({ url: `/pages/feedback/index?type=pay${this.orderId ? '&orderId=' + encodeURIComponent(this.orderId) : ''}` }); },
});
