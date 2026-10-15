const api = require('../../utils/api.js');
const terms = require('../../content/advisor-terms.js');
const { toolConsent } = require('../../utils/data-tools.js');
const { ensureConsent } = require('../../utils/consent.js');
Page({
  data: { paragraphs: terms.paragraphs, loading: true, error: '', agreed: false, checked: false, sending: false, enabled: false },
  onShow() { this.dead = false; this.load(); },
  onUnload() { this.dead = true; },
  load() {
    this.setData({ loading: true, error: '', checked: false });
    api.advisorStatus().then((res) => {
      if (!res.enabled) throw new Error('数据工具暂时不可用');
      return toolConsent();
    }).then((agreed) => { if (!this.dead) this.setData({ loading: false, agreed, enabled: true }); })
      .catch((err) => { if (!this.dead) this.setData({ loading: false, enabled: false, error: err.message }); });
  },
  check(e) { if (!this.data.sending) this.setData({ checked: e.detail.value.includes('agree') }); },
  agree() {
    if (this.data.loading || !this.data.enabled || !this.data.checked || this.data.sending) return;
    this.setData({ sending: true, error: '' });
    ensureConsent().then((ok) => {
      if (!ok || this.dead) { if (!this.dead) this.setData({ sending: false }); return; }
      return api.agree(terms.version, 'advisor').then(() => {
        if (this.dead) return;
        this.setData({ sending: false, agreed: true });
        wx.navigateBack({ fail: () => wx.redirectTo({ url: '/pages/advisor/index' }) });
      });
    }).catch((err) => { if (!this.dead) this.setData({ sending: false, error: err.message }); });
  },
});
