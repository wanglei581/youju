const api = require('../../utils/api.js');
const { ensureConsent } = require('../../utils/consent.js');
const { VERSION, consent } = require('../../utils/advisor.js');

Page({
  data: { loading: true, error: '', agreed: false, sending: false, checked: false },
  onShow() { this.load(); },
  load() {
    this.setData({ loading: true, error: '' });
    consent().then((agreed) => this.setData({ loading: false, agreed }))
      .catch((err) => this.setData({ loading: false, error: err.message }));
  },
  check(e) { this.setData({ checked: e.detail.value.includes('agree') }); },
  agree() {
    if (this.data.sending || !this.data.checked) return;
    this.setData({ sending: true, error: '' });
    (api.isLive() ? ensureConsent() : Promise.resolve(true)).then((ok) => {
      if (!ok) { this.setData({ sending: false }); return; }
      return api.agree(VERSION, 'advisor').then(() => {
        this.setData({ sending: false, agreed: true });
        wx.navigateBack({ fail: () => wx.redirectTo({ url: '/pages/advisor/index' }) });
      });
    }).catch((err) => this.setData({ sending: false, error: err.message }));
  },
});
