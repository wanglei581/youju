const api = require('../../utils/api.js');
Page({
  data: { loading: true, closed: false, error: '', inviteCode: '', monthCount: 0, remaining: 0 },
  onShow() { return this.load(); },
  load() {
    const seq = this.seq = (this.seq || 0) + 1;
    this.setData({ loading: true, closed: false, error: '', inviteCode: '' });
    // 只有主动转发按钮能带码；不开放右上角菜单转发。
    wx.hideShareMenu();
    return api.inviteInfo().then((res) => {
      if (seq === this.seq) this.setData({ loading: false, inviteCode: res.inviteCode, monthCount: res.monthCount, remaining: Math.max(0, res.monthLimit - res.monthCount) });
    }).catch((err) => {
      if (seq === this.seq) this.setData({ loading: false, closed: err.status === 404, error: err.status === 404 ? '' : err.message });
    });
  },
  onShareAppMessage() {
    const code = !this.data.closed && !this.data.loading && !this.data.error && this.data.inviteCode;
    return { title: '查申报通知和公示名单，每条附原文链接', path: `/pages/home/index${code ? '?inv=' + encodeURIComponent(code) : ''}` };
  },
});
