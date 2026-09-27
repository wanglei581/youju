const legal = require('../../content/legal.js');

const ENV_NAME = { develop: '开发版', trial: '体验版' };

// 关于有据：运营者、备案号、联系和投诉渠道
// （微信小程序运营规范 3.10 侵权投诉机制、3.13 披露机构信息和咨询投诉渠道）
Page({
  data: { op: legal.operator, disclaimer: legal.disclaimer, version: '' },

  onLoad() {
    let version = '';
    try {
      const mp = wx.getAccountInfoSync().miniProgram;
      version = mp.version || ENV_NAME[mp.envVersion] || '';
    } catch (e) { /* 取不到版本号不影响页面 */ }
    this.setData({ version });
  },

  copyEmail() {
    const { email } = this.data.op;
    if (!email || email.indexOf('【') === 0) return;
    wx.setClipboardData({
      data: email,
      success: () => wx.showToast({ title: '邮箱已复制', icon: 'none' }),
      fail: () => wx.showToast({ title: '没能复制，请同意隐私保护指引后再试', icon: 'none' }),
    });
  },

  feedback() {
    wx.navigateTo({ url: '/pages/feedback/index' });
  },

  openAgreement(e) {
    wx.navigateTo({ url: `/pages/agreement/index?type=${e.currentTarget.dataset.type}` });
  },
});
