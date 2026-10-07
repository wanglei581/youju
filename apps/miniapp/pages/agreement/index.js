const legal = require('../../content/legal.js');
const api = require('../../utils/api.js');
const { consentOpen, markAgreed, loginOpen } = require('../../utils/consent.js');

// 阅读协议。consent=1 时是"同意模式"：依次显示《用户协议》和《隐私保护指引》全文，
// 同意按钮放在最后，读完才能同意（首次同意"先看协议"、协议更新后重新同意都走这里）。
// 协议定稿前（loginOpen 为 false，D-056）不显示草案：入口都改为打开官方隐私保护指引，直接打开这一页时只显示"暂未开放"
Page({
  data: { docs: [], final: false, status: '', disclaimer: '', consent: false, sending: false, loginOpen: loginOpen() },

  onLoad(query) {
    if (!this.data.loginOpen) return;
    const consent = query.consent === '1' && consentOpen();
    let docs;
    if (consent) docs = [legal.user, legal.privacy];
    else docs = [query.type === 'privacy' ? legal.privacy : legal.user];
    this.setData({ docs, final: legal.final, status: legal.status, disclaimer: legal.disclaimer, consent });
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
