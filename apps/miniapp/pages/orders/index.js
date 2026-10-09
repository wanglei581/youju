const api = require('../../utils/api.js');
const pay = require('../../utils/pay.js');
const { loginOpen } = require('../../utils/consent.js');
Page({
  data: { loginOpen: loginOpen(), loading: true, error: '', items: [], refunding: null, message: '' },
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
  invoice(e) { this.orderId = e.currentTarget.dataset.id; wx.navigateTo({ url: `/pages/feedback/index?type=invoice&orderId=${encodeURIComponent(e.currentTarget.dataset.id)}` }); },
  feedback() { wx.navigateTo({ url: `/pages/feedback/index?type=pay${this.orderId ? '&orderId=' + encodeURIComponent(this.orderId) : ''}` }); },
});
