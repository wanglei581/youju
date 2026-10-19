const api = require('../../utils/api.js');
const pay = require('../../utils/pay.js');
const { loginOpen } = require('../../utils/consent.js');

// 每次打开都给一份新的空表单：表单按 form.title 这样的路径改字段，共用一个对象会把上一张订单
// 没提交的内容带到下一张（agy 审阅，10/16）
function emptyForm() { return { titleType: 'personal', title: '', taxId: '', email: '' }; }
// 没有订单、也没在卖时直接回首页，不显示"暂未开放"之类的字样（D-074）：首发包带着这一页，
// 从旧链接直接打开时只会回到首页。有订单的人（停卖以后）照常看订单、退款、开票
function goHome() {
  wx.switchTab({ url: '/pages/home/index' });
}

Page({
  data: { loginOpen: loginOpen(), loading: true, error: '', items: [], refunding: null, message: '',
    invoiceFor: null, form: emptyForm(), formError: '', sending: false },
  onShow() { this.visible = true; this.load(); },
  onHide() { this.visible = false; this.serial = (this.serial || 0) + 1; },
  onUnload() { this.onHide(); },
  load() {
    if (!this.data.loginOpen) { this.setData({ loading: false }); return; }
    const serial = this.serial = (this.serial || 0) + 1;
    this.setData({ loading: true, error: '' });
    api.payOrders().then((res) => {
      if (serial !== this.serial) return null;
      const items = res.items.map(pay.orderView);
      if (items.length) { this.setData({ loading: false, items }); return null; }
      // 没有订单：在售时写"还没有订单"，不在售就回首页（能力接口失败也按不在售处理）
      return api.advisorCapabilities().then((caps) => {
        if (serial !== this.serial) return;
        if (caps && caps.pay && caps.pay.enabled === true) this.setData({ loading: false, items: [] });
        else goHome();
      });
    }).catch((err) => {
      if (serial !== this.serial) return;
      if (err.status === 404) { goHome(); return; }
      this.setData({ loading: false, error: err.message });
    });
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
