// 小程序不能打开政府网站（web-view 需要配置业务域名），原文入口统一为"复制原文链接"。
// message：复制成功后的提示（附件打不开时写明原因），不给就用默认的
function copyLink(url, message) {
  if (!url) return;
  wx.setClipboardData({
    data: url,
    success() {
      wx.showToast({ title: message || '已复制，请粘贴到浏览器打开', icon: 'none', duration: message ? 3000 : 2200 });
    },
    // 剪贴板属于隐私接口：用户没有同意隐私保护指引时会失败
    fail() {
      wx.showToast({ title: '没能复制，请同意隐私保护指引后再试', icon: 'none', duration: 2500 });
    },
  });
}

module.exports = { copyLink };
