/* Small UI helpers shared by pages. */

function toast(title, icon) {
  wx.showToast({ title: String(title || '').slice(0, 40), icon: icon || 'none', duration: 2200 });
}

/**
 * Mini-programs cannot open arbitrary external web pages without a registered
 * business domain, so original URLs are copied for use in a browser.
 */
function copyLink(url, label) {
  const text = String(url || '');
  if (!/^https?:\/\//i.test(text)) {
    toast('没有可复制的链接');
    return;
  }
  wx.setClipboardData({
    data: text,
    success() {
      toast((label || '链接') + '已复制，可粘贴到浏览器打开');
    },
    fail() {
      toast('复制失败，请长按链接文字手动复制');
    },
  });
}

function confirm(options) {
  return new Promise((resolve) => {
    wx.showModal(Object.assign({ confirmColor: '#0B3633' }, options, {
      success(res) { resolve(!!res.confirm); },
      fail() { resolve(false); },
    }));
  });
}

function alert(title, content) {
  return new Promise((resolve) => {
    wx.showModal({
      title,
      content: String(content || ''),
      showCancel: false,
      confirmText: '知道了',
      confirmColor: '#0B3633',
      complete() { resolve(); },
    });
  });
}

module.exports = { toast, copyLink, confirm, alert };
