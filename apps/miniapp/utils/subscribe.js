const config = require('./config.js');

// 一次性订阅消息：只有用户在微信弹窗里点"允许"才算订阅成功，每次订阅提醒一次
function requestOnce(kind) {
  const id = config.templates[kind];
  if (!id) {
    wx.showModal({
      title: '订阅消息还没开通',
      content: '在小程序后台申请订阅消息模板后即可订阅（当前是开发版）。',
      showCancel: false,
    });
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    wx.requestSubscribeMessage({
      tmplIds: [id],
      success: (res) => resolve(res[id] === 'accept'),
      fail: () => resolve(false),
    });
  });
}

module.exports = { requestOnce };
