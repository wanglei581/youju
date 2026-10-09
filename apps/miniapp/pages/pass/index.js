const api = require('../../utils/api.js');
const pay = require('../../utils/pay.js');
const payTerms = require('../../content/pay-terms.js');
const { loginOpen } = require('../../utils/consent.js');
const { consent } = require('../../utils/advisor.js');

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
    this.setData({ busy: true, message: pay.TEXT.ordering, payError: false });
    consent().then((agreed) => {
      if (this.unloaded) return;
      if (!agreed) { this.setData({ busy: false, message: '' }); wx.navigateTo({ url: '/pages/advisor-terms/index' }); return; }
      return this.startPay();
    }).catch((err) => { if (!this.unloaded) this.setData({ busy: false, message: err.message || pay.TEXT.orderFailed, payError: true }); });
  },
  startPay() {
    return new Promise((resolve, reject) => {
      const fail = () => reject(new Error(pay.TEXT.login));
      wx.login({ success: (res) => res && res.code ? resolve(res.code) : fail(), fail });
    }).then((code) => {
      if (this.unloaded) return null;
      return api.createPayOrder({ product_id: this.data.product.productId, code });
    }).then((order) => {
      if (!order || this.unloaded) return;
      this.orderId = order.orderId;
      this.poller = pay.createPoller({ fetch: () => api.payOrder(order.orderId), onDone: (result) => this.payDone(result), timers: { set: setTimeout, clear: clearTimeout }, now: () => Date.now() });
      try {
        wx.requestVirtualPayment(Object.assign(pay.paymentParams(order), {
          success: () => this.afterPay(false), fail: (err) => this.afterPay(pay.isCancel(err)),
        }));
      } catch (err) { this.afterPay(pay.isCancel(err)); }
    });
  },
  afterPay(cancelled) {
    if (this.unloaded || this.payDeadline) return;
    this.payDeadline = Date.now() + pay.POLL_TIMEOUT;
    this.setData({ message: cancelled ? pay.TEXT.cancelled : pay.TEXT.confirming, payError: false });
    if (cancelled) wx.showToast({ title: pay.TEXT.cancelled, icon: 'none' });
    if (!this.visible) { this.payResume = true; return; }
    this.poller.start(this.payDeadline);
  },
  payDone(result) {
    this.payResume = false; this.payDeadline = null;
    if (result.state === 'success') {
      const order = result.order;
      this.setData({ busy: false, confirmed: true, message: `已开通，使用期到 ${pay.date(order.passEndsAt)}`, payError: false });
      // 续购、退款造成的分段使用期，以状态接口的连续到期时间展示。
      api.advisorStatus({ refresh: true }).then((status) => {
        if (!this.unloaded && status.pass && status.pass.active) this.setData({ expires: pay.date(status.pass.expiresAt), message: `已开通，使用期到 ${pay.date(status.pass.expiresAt)}` });
      }).catch(() => {});
    } else this.setData({ busy: false, message: result.state === 'timeout' ? pay.TEXT.timeout : `订单状态：${result.order.status}`, payError: result.state === 'failed' });
  },
  tools() { wx.navigateTo({ url: '/pages/advisor/index' }); },
  feedback() { wx.navigateTo({ url: `/pages/feedback/index?type=pay${this.orderId ? '&orderId=' + encodeURIComponent(this.orderId) : ''}` }); },
});
