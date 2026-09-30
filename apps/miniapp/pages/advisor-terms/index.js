const api = require('../../utils/api.js');
const { ensureConsent } = require('../../utils/consent.js');
const { VERSION, TERMS_TEXT, TERMS_FINAL, consent } = require('../../utils/advisor.js');

Page({
  data: { loading: true, error: '', agreed: false, sending: false, checked: false, enabled: false, final: TERMS_FINAL, text: TERMS_TEXT, version: VERSION },
  onShow() { this.load(); },
  load() {
    this.setData({ loading: true, error: '', enabled: false, checked: false });
    api.advisorStatus().then((res) => {
      this.setData({ enabled: !!res.enabled });
      return res.enabled && this.data.final ? consent() : false;
    }).then((agreed) => this.setData({ loading: false, agreed }))
      .catch((err) => this.setData({ loading: false, enabled: false, error: err.status === 404 ? '' : err.message }));
  },
  check(e) { this.setData({ checked: e.detail.value.includes('agree') }); },
  agree() {
    if (this.data.loading || this.data.error || !this.data.enabled || !this.data.final || this.data.sending || !this.data.checked) return;
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
