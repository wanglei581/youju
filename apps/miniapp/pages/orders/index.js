const api = require('../../utils/api.js');
const pay = require('../../utils/pay.js');
const { loginOpen } = require('../../utils/consent.js');

const EMPTY_FORM = { titleType: 'personal', title: '', taxId: '', email: '' };

Page({
  data: { loginOpen: loginOpen(), loading: true, error: '', items: [], refunding: null, message: '',
    invoiceFor: null, form: EMPTY_FORM, formError: '', sending: false },
  onShow() { this.visible = true; this.load(); },
  onHide() { this.visible = false; this.serial = (this.serial || 0) + 1; },
  onUnload() { this.onHide(); },
  load() {
    if (!this.data.loginOpen) { this.setData({ loading: false }); return; }
    const serial = this.serial = (this.serial || 0) + 1;
    this.setData({ loading: true, error: '' });
    api.payOrders().then((res) => {
      if (serial === this.serial) this.setData({ loading: false, items: res.items.map(pay.orderView) });
    }).catch((err) => { if (serial === this.serial) this.setData({ loading: false, error: err.status === 404 ? '付费服务暂未开放' : err.message }); });
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
      form: old ? { titleType: old.titleType, title: old.title, taxId: old.taxId || '', email: old.email } : EMPTY_FORM });
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
