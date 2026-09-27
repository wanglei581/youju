// 小程序不能打开政府网站（web-view 需要配置业务域名），原文入口统一为"复制原文链接"
function copyLink(url) {
  if (!url) return;
  wx.setClipboardData({
    data: url,
    success() {
      wx.showToast({ title: '已复制，请粘贴到浏览器打开', icon: 'none', duration: 2200 });
    },
  });
}

module.exports = { copyLink };
