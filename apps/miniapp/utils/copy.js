// 可选记录官方来源网址；核心阅读通过原生页面完成。
// message：复制成功后的提示（附件打不开时写明原因），不给就用默认的
function copyLink(url, message) {
  if (!url) return;
  wx.setClipboardData({
    data: url,
    success() {
      wx.showToast({ title: message || '来源网址已复制', icon: 'none', duration: message ? 3000 : 2200 });
    },
    // 剪贴板属于隐私接口：用户没有同意隐私保护指引时会失败
    fail() {
      wx.showToast({ title: '没能复制：请先同意微信弹出的隐私保护指引，再点一次', icon: 'none', duration: 2500 });
    },
  });
}

module.exports = { copyLink };
