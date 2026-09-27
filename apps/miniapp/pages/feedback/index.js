const api = require('../../utils/api.js');
const { ensureConsent } = require('../../utils/consent.js');

const MAX = 500;

// 纠错与建议：只收文字和对应的文件，不收姓名、电话等联系方式
Page({
  data: { docId: '', title: '', content: '', count: 0, max: MAX, sending: false },

  onLoad(query) {
    this.setData({
      docId: query.id || '',
      title: query.title ? decodeURIComponent(query.title) : '',
    });
  },

  onInput(e) {
    const content = e.detail.value.slice(0, MAX);
    this.setData({ content, count: content.length });
  },

  submit() {
    const content = this.data.content.trim();
    if (!content) {
      wx.showToast({ title: '请写下要纠正或建议的内容', icon: 'none' });
      return;
    }
    if (this.data.sending) return;
    ensureConsent().then((ok) => { if (ok) this.send(content); });
  },

  send(content) {
    this.setData({ sending: true });
    api.feedback(this.data.docId, content)
      .then(() => {
        wx.showToast({ title: '已收到，谢谢你', icon: 'success' });
        setTimeout(() => wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/me/index' }) }), 1200);
      })
      .catch((err) => {
        this.setData({ sending: false });
        wx.showToast({ title: err.message || '提交失败，请稍后再试', icon: 'none', duration: 2500 });
      });
  },
});
