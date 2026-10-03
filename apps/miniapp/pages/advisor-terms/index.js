const api = require('../../utils/api.js');
const { ensureConsent } = require('../../utils/consent.js');
const { VERSION, TERMS_TEXT, TERMS_FINAL, consent } = require('../../utils/advisor.js');

Page({
  data: { loading: true, error: '', agreed: false, sending: false, checked: false, enabled: false, final: TERMS_FINAL, text: TERMS_TEXT, version: VERSION, demo: false },
  onShow() { this.load(); },
  load() {
    this.setData({ loading: true, error: '', enabled: false, checked: false });
    const demo = !api.isLive();
    this.setData({ demo, final: demo || TERMS_FINAL, text: demo ? '本页为演示使用说明，仅用于体验流程。请使用虚构企业信息；演示任务只在这台手机保存任务类型、行数和进度，不保存你提交的客户内容。生成的 Excel 是固定示例，不代表真实查询结果。你可以在“我的任务”中删除演示任务。' : TERMS_TEXT });
    api.advisorStatus({ refresh: true }).then((res) => {
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
