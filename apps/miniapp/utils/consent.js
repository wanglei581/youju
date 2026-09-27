// 关注、订阅提醒、纠错之前，先确认用户已同意用户协议和隐私保护指引（服务端记录，本机缓存版本号）。
// 协议还是草案（legal.final 为 false）时不开放同意，这些功能显示"协议定稿后开放"。
const legal = require('../content/legal.js');
const api = require('./api.js');

// 协议已定稿、连着真实接口时，才开放同意和需要同意的功能
function consentOpen() {
  return legal.final && api.isLive();
}

function agreedHere() {
  try { return wx.getStorageSync(api.AGREED_KEY) === legal.version; } catch (e) { return false; }
}

function remember() {
  try { wx.setStorageSync(api.AGREED_KEY, legal.version); } catch (e) { /* 缓存写不进去下次再问 */ }
}

function forget() {
  api.forgetAgreed();
}

// 返回 true 表示已同意，可以继续
function ensureConsent() {
  if (!legal.final) {
    wx.showModal({
      title: '协议定稿后开放',
      content: '关注、微信提醒和纠错需要先同意《用户协议》和《隐私保护指引》，协议正在定稿，定稿后开放。',
      showCancel: false,
    });
    return Promise.resolve(false);
  }
  if (!api.isLive()) {
    wx.showToast({ title: '演示数据不能使用这项功能', icon: 'none' });
    return Promise.resolve(false);
  }
  if (agreedHere()) return Promise.resolve(true);
  return new Promise((resolve) => {
    wx.showModal({
      title: '请先阅读并同意',
      content: '使用关注、微信提醒和纠错前，请阅读并同意《用户协议》和《隐私保护指引》。我们只保存你的微信用户标识和你主动设置、提交的内容，可随时在\u201c我的\u201d里注销并删除。',
      confirmText: '同意',
      cancelText: '先看协议',
      success: (res) => {
        if (!res.confirm) {
          wx.navigateTo({ url: '/pages/agreement/index' });
          resolve(false);
          return;
        }
        api.agree(legal.version)
          .then(() => { remember(); resolve(true); })
          .catch((err) => { wx.showToast({ title: err.message || '网络不太好，请稍后再试', icon: 'none' }); resolve(false); });
      },
      fail: () => resolve(false),
    });
  });
}

module.exports = { ensureConsent, consentOpen, agreedHere, remember, forget };
