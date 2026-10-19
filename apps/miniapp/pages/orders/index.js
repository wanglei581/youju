const api = require('../../utils/api.js');
const pay = require('../../utils/pay.js');
const { loginOpen } = require('../../utils/consent.js');

// 每次打开都给一份新的空表单：表单按 form.title 这样的路径改字段，共用一个对象会把上一张订单
// 没提交的内容带到下一张（agy 审阅，10/16）
function emptyForm() { return { titleType: 'personal', title: '', taxId: '', email: '' }; }
// 没有订单、也没在卖时直接回首页，不显示"暂未开放"之类的字样（D-074）：首发包带着这一页，
// 从旧链接直接打开时只会回到首页。有订单的人（停卖以后）照常看订单、退款、开票
function goHome() {
  wx.switchTab({ url: '/pages/home/index', fail: () => wx.reLaunch({ url: '/pages/home/index' }) });
}

// 打开订单页时最多替几张待确认的订单向微信核对（服务端也只核对每人最近 3 张）
const AUTO_CHECK = 3;
// "重新核对"的结果写成一句提示（服务端核对接口的 check；null 是刚核对过、这次没再问微信）
const CHECK_TOAST = {
  paid: '已核对到付款，已开通', unpaid: '还没查到这笔付款', closed: '没有确认到付款，订单已关闭',
  refunded: '这笔付款已经退回', manual: '付款已收到，正在人工核对', unavailable: '暂时没能核对，请稍后再试',
};

Page({
  // checking：正在"重新核对"的订单号
  data: { loginOpen: loginOpen(), loading: true, error: '', items: [], refunding: null, checking: null, message: '',
    invoiceFor: null, form: emptyForm(), formError: '', sending: false },
  onShow() { this.visible = true; this.load(); },
  onHide() { this.visible = false; this.serial = (this.serial || 0) + 1; },
  onUnload() { this.onHide(); },
  load() {
    if (!this.data.loginOpen) { this.setData({ loading: false }); return; }
    const serial = this.serial = (this.serial || 0) + 1;
    // 切走时还在"重新核对"的那一张：结果不再写回，按钮要放开
    this.setData({ loading: true, error: '', checking: null });
    api.payOrders().then((res) => {
      if (serial !== this.serial) return null;
      const items = res.items.map(pay.orderView);
      if (items.length) { this.setData({ loading: false, items }); this.autoCheck(serial); return null; }
      // 没有订单：直接问商品接口（不走能力接口的缓存，Codex 审阅）——有在售的档位写"还没有订单"；
      // 404 或没有档位才回首页；出错（没连上、服务端出错）留在本页给重试，不当成停卖
      return api.payProducts().then((res) => {
        if (serial !== this.serial) return;
        if (pay.normalizeProducts(res).length) this.setData({ loading: false, items: [] });
        else goHome();
      }, (err) => {
        if (serial !== this.serial) return;
        if (err && err.status === 404) goHome();
        else this.setData({ loading: false, error: (err && err.message) || '网络连接失败，请检查网络后重试' });
      });
    }).catch((err) => {
      if (serial !== this.serial) return;
      if (err.status === 404) { goHome(); return; }
      this.setData({ loading: false, error: err.message });
    });
  },
  // 打开订单页时，替待确认的订单向微信核对一次：付了款的当场开通，没付的写"还没查到这笔付款"。
  // 一张一张查，结果回来就换掉那一张；页面切走或重新载入后，旧的结果不再写回
  autoCheck(serial) {
    const ids = this.data.items.filter((item) => item.canCheck).slice(0, AUTO_CHECK).map((item) => item.orderId);
    ids.reduce((chain, id) => chain.then(() => {
      if (serial !== this.serial) return null;
      return api.checkPayOrder(id).then((order) => { if (serial === this.serial) this.replace(order); }, () => {});
    }), Promise.resolve()).catch(() => {});
  },
  replace(order) {
    if (!order || order.orderId === undefined) return;
    const view = pay.orderView(order);
    this.setData({ items: this.data.items.map((item) => (String(item.orderId) === String(order.orderId) ? view : item)) });
  },
  // "重新核对"：用户说自己付过款、页面还是待确认时点；结果写在这张订单上，另给一句提示
  recheck(e) {
    if (this.data.checking !== null) return;
    const order = this.data.items.find((item) => String(item.orderId) === String(e.currentTarget.dataset.id));
    if (!order || !order.canCheck) return;
    this.orderId = order.orderId;
    const serial = this.serial;
    this.setData({ checking: order.orderId });
    api.checkPayOrder(order.orderId).then((result) => {
      if (serial !== this.serial) return;
      this.setData({ checking: null });
      this.replace(result);
      const opened = result && result.status === '已完成';
      wx.showToast({ title: opened ? CHECK_TOAST.paid : CHECK_TOAST[result && result.check] || '正在核对，请过几秒再看', icon: 'none' });
    }).catch((err) => {
      if (serial !== this.serial) return;
      this.setData({ checking: null });
      wx.showToast({ title: (err && err.message) || CHECK_TOAST.unavailable, icon: 'none' });
    });
  },
  // 待确认、人工核对中的订单上的"遇到问题"：带上这张订单的编号
  problem(e) {
    this.orderId = e.currentTarget.dataset.id;
    this.feedback();
  },
  refund(e) {
    if (this.data.refunding !== null) return;
    const order = this.data.items.find((item) => String(item.orderId) === String(e.currentTarget.dataset.id));
    if (!order || !order.canRefund) return;
    this.orderId = order.orderId;
    this.setData({ refunding: order.orderId });
    wx.showModal({ title: '申请退款', content: order.refundConfirm, confirmText: '申请退款', success: (res) => {
      if (!res.confirm) { this.setData({ refunding: null }); return; }
      api.refundPayOrder(order.orderId).then(() => {
        this.setData({ refunding: null, message: pay.TEXT.refund });
        this.load();
      }).catch((err) => {
        this.setData({ refunding: null, message: err.message }); this.load();
      });
    }, fail: () => this.setData({ refunding: null }) });
  },
  // 发票：每笔订单一份，待开时可以改；不开的写着原因
  invoice(e) {
    if (this.data.sending) return;
    const order = this.data.items.find((item) => String(item.orderId) === String(e.currentTarget.dataset.id));
    if (!order || !order.invoiceAction) return;
    this.orderId = order.orderId;
    const old = order.invoice;
    this.setData({ invoiceFor: order.orderId, formError: '',
      form: old ? { titleType: old.titleType, title: old.title, taxId: old.taxId || '', email: old.email } : emptyForm() });
  },
  cancelInvoice() { if (!this.data.sending) this.setData({ invoiceFor: null, formError: '' }); },
  titleType(e) { this.setData({ 'form.titleType': e.detail.value, formError: '' }); },
  field(e) { this.setData({ [`form.${e.currentTarget.dataset.field}`]: e.detail.value, formError: '' }); },
  sendInvoice() {
    if (this.data.sending || this.data.invoiceFor === null) return;
    const form = this.data.form;
    const problem = pay.invoiceProblem(form);
    if (problem) { this.setData({ formError: problem }); return; }
    const orderId = this.data.invoiceFor;
    this.setData({ sending: true, formError: '' });
    api.requestInvoice(orderId, pay.invoiceBody(form)).then((order) => {
      const view = pay.orderView(order);
      this.setData({ sending: false, invoiceFor: null, message: pay.TEXT.invoiceSent,
        items: this.data.items.map((item) => (String(item.orderId) === String(orderId) ? view : item)) });
    }).catch((err) => { this.setData({ sending: false, formError: err.message }); });
  },
  feedback() { wx.navigateTo({ url: `/pages/feedback/index?type=pay${this.orderId ? '&orderId=' + encodeURIComponent(this.orderId) : ''}` }); },
});
