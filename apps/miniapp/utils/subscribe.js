const config = require('./config.js');
const api = require('./api.js');

// 一次性订阅消息：用户在微信弹窗里允许，并且服务端保存成功，才算订阅成功；每次订阅提醒一次。
// target 说明订阅的是什么（如某份文件的截止、某个关注条件），由服务端保存后按时发送（#30）。
function requestOnce(kind, target) {
  const id = config.templates[kind];
  if (!id) {
    wx.showModal({ title: '订阅消息还没开通', content: '在小程序后台申请订阅消息模板后即可订阅（当前是开发版）。', showCancel: false });
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    wx.requestSubscribeMessage({
      tmplIds: [id],
      success: (res) => resolve(res[id] === 'accept'),
      fail: () => resolve(false),
    });
  }).then((accepted) => {
    if (!accepted) return false;
    return api.saveSubscription(kind, target)
      .then(() => true)
      .catch(() => {
        wx.showToast({ title: '订阅暂不可用，请稍后再试', icon: 'none' });
        return false;
      });
  });
}

module.exports = { requestOnce };
