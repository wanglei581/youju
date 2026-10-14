// 投诉举报：公布入口、处理流程和反馈时限（《互联网信息服务深度合成管理规定》第十二条、
// 《生成式人工智能服务管理暂行办法》第十五条）。AI 页面底部和关于页都能进来。
const legal = require('../../content/legal.js');
const { loginOpen } = require('../../utils/consent.js');

Page({
  data: { op: legal.operator, loginOpen: loginOpen() },
  onShow() { this.setData({ loginOpen: loginOpen() }); },
  copyEmail() {
    const { email } = this.data.op;
    if (!email || email.indexOf('【') === 0) return;
    wx.setClipboardData({
      data: email,
      success: () => wx.showToast({ title: '邮箱已复制', icon: 'none' }),
      fail: () => wx.showToast({ title: '没能复制，请记下邮箱地址', icon: 'none' }),
    });
  },
  feedback() { if (this.data.loginOpen) wx.navigateTo({ url: '/pages/feedback/index' }); },
});
