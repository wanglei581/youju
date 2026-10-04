const legal = require('../../content/legal.js');
const payTerms = require('../../content/pay-terms.js');
const api = require('../../utils/api.js');
const { consentOpen, markAgreed } = require('../../utils/consent.js');

// 阅读协议。consent=1 时是"同意模式"：依次显示《用户协议》和《隐私保护指引》全文，
// 同意按钮放在最后，读完才能同意（首次同意"先看协议"、协议更新后重新同意都走这里）
Page({
  data: { docs: [], final: false, status: '', disclaimer: '', consent: false, sending: false },

  onLoad(query) {
    const consent = query.consent === '1' && consentOpen();
    let docs;
    if (consent) docs = [legal.user, legal.privacy];
    else if (query.type === 'pay') docs = [{ title: payTerms.title, sections: [{ h: '', p: [payTerms.text] }] }];
    else docs = [query.type === 'privacy' ? legal.privacy : legal.user];
    const isPay = !consent && query.type === 'pay';
    this.setData({
      docs, consent, disclaimer: legal.disclaimer,
      final: isPay ? payTerms.final : legal.final,
      status: isPay ? '草稿：律师定稿前不开放付费' : legal.status,
    });
    wx.setNavigationBarTitle({ title: consent ? '用户协议和隐私保护指引' : docs[0].title });
  },

  agree() {
    if (this.data.sending) return;
    this.setData({ sending: true });
    api.agree(legal.version)
      .then(() => {
        markAgreed();
        wx.showToast({ title: '已同意，关注和微信提醒可以用了', icon: 'none' });
        setTimeout(() => wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/me/index' }) }), 1000);
      })
      .catch((err) => {
        this.setData({ sending: false });
        wx.showToast({ title: err.message || '网络不太好，请稍后再试', icon: 'none' });
      });
  },
});
