const config = require('./config.js');
const api = require('./api.js');
const { ensureConsent, agreedHere, consentOpen } = require('./consent.js');

const KIND_TEXT = {
  newDocs: '这个关注有新文件时，用微信提醒你一次。',
  deadline: '截止前 3 天，用微信提醒你一次。',
};

// 一次性订阅消息：用户在微信弹窗里允许，并且服务端保存成功，才算订阅成功；每次订阅提醒一次。
// target 说明订阅的是什么（如某份文件的截止、某个关注条件），由服务端保存后按时发送（#30）。
// 微信要求订阅弹窗由用户点击直接调起（基础库 2.8.2 起），所以：
// 已同意时在点击里同步调起；刚同意（中间有网络请求）时，再弹一次确认，用户点"订阅"时调起。
function requestOnce(kind, target) {
  if (consentOpen() && agreedHere()) return ask(kind, target);
  return ensureConsent().then((ok) => {
    if (!ok) return false;
    return new Promise((resolve) => {
      wx.showModal({
        title: '订阅微信提醒',
        content: KIND_TEXT[kind] || '',
        confirmText: '订阅',
        cancelText: '不用了',
        success: (res) => resolve(res.confirm ? ask(kind, target) : false),
        fail: () => resolve(false),
      });
    });
  });
}

function ask(kind, target) {
  const id = config.templates[kind];
  if (!id) {
    wx.showModal({ title: '订阅消息还没开通', content: '在小程序后台申请订阅消息模板后即可订阅（当前是开发版）。', showCancel: false });
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    wx.requestSubscribeMessage({
      tmplIds: [id],
      // 结果：accept 允许；reject 拒绝；ban 被后台封禁；filter 模板标题同名被过滤
      success: (res) => {
        if (res[id] === 'accept') { resolve(true); return; }
        wx.showToast({ title: res[id] === 'reject' ? '你没有允许，不会提醒' : '这个提醒暂时不能订阅', icon: 'none' });
        resolve(false);
      },
      // 20004：用户关闭了订阅消息主开关
      fail: (err) => {
        const title = err && err.errCode === 20004 ? '你关闭了订阅消息，可在右上角"···"→设置里打开' : '没能打开订阅，请稍后再试';
        wx.showToast({ title, icon: 'none', duration: 2500 });
        resolve(false);
      },
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
