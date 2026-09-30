// 关注、订阅提醒、纠错之前，先确认用户已同意用户协议和隐私保护指引（服务端记录，本机缓存版本号）。
// 协议还是草案（legal.final 为 false）时不开放同意，这些功能显示"协议定稿后开放"。
const legal = require('../content/legal.js');
const api = require('./api.js');
const exportsLog = require('./exports.js');
const { removeFiles } = require('./exporter.js');

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

// 本机没有同意记录时（如换了设备、还没进过"我的"），每次启动向服务端核对一次：
// 同意的是当前版本就补上本机记录。查询 /me 不会建用户记录。返回 true 表示已同意当前版本。
let pending = null;
let notAgreed = false; // 本次启动已核对过：服务端没有当前版本的同意，不再重复查
let outdated = false; // 服务端记录的是旧版本的同意：协议更新了，要重新阅读并同意
let epoch = 0; // 本机每次同意或注销加 1：在这之前发出的 /me 查询结果已过时，不再覆盖本机状态

function consentEpoch() {
  return epoch;
}

// 按服务端 /me 返回的同意记录更新本机状态（"我的"和 syncConsent 共用，保证两处一致）：
// 同意的是当前版本就记下；旧版本标记"协议已更新"；没有记录或旧版本都清掉本机记录。返回是否已同意当前版本。
// startedAt 是发出查询时的 consentEpoch()：之后本机又同意或注销过，这个结果已过时，不改状态
function applyServer(agreed, startedAt) {
  if (startedAt !== epoch) return agreedHere();
  const ok = !!agreed && agreed.version === legal.version;
  if (ok) remember();
  else forget();
  outdated = !!agreed && !ok;
  notAgreed = !ok;
  return ok;
}

function syncConsent() {
  if (!consentOpen()) return Promise.resolve(false);
  if (agreedHere()) return Promise.resolve(true);
  if (notAgreed) return Promise.resolve(false);
  if (!pending) {
    const started = epoch;
    pending = api.me()
      .then((res) => applyServer(res.agreed, started))
      .catch(() => false)
      .then((ok) => { pending = null; return ok; });
  }
  return pending;
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
  return syncConsent().then((synced) => (synced ? true : askConsent()));
}

// 读全文并同意的页面：依次显示两份文件全文，同意按钮在最后
const CONSENT_PAGE = '/pages/agreement/index?consent=1';

function askConsent() {
  // 协议更新后：只能先读更新后的全文，在全文最后同意
  if (outdated) {
    wx.showModal({
      title: '协议已更新',
      content: '《用户协议》和《隐私保护指引》已更新。请先阅读更新后的全文，在全文最后同意后，再使用关注、微信提醒和纠错。',
      confirmText: '阅读全文',
      cancelText: '暂不',
      success: (res) => { if (res.confirm) wx.navigateTo({ url: CONSENT_PAGE }); },
    });
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    wx.showModal({
      title: '请先阅读并同意',
      content: '使用关注、微信提醒和纠错前，请阅读并同意《用户协议》和《隐私保护指引》。我们只保存你的微信用户标识和你主动设置、提交的内容，可随时在\u201c我的\u201d里注销并删除。',
      confirmText: '同意',
      cancelText: '先看协议',
      success: (res) => {
        if (!res.confirm) {
          wx.navigateTo({ url: CONSENT_PAGE });
          resolve(false);
          return;
        }
        api.agree(legal.version)
          .then(() => { markAgreed(); resolve(true); })
          .catch((err) => { wx.showToast({ title: err.message || '网络不太好，请稍后再试', icon: 'none' }); resolve(false); });
      },
      fail: () => resolve(false),
    });
  });
}

// 服务端记录同意之后调用：记下本机记录，清掉"协议已更新"
function markAgreed() {
  remember();
  outdated = false;
  notAgreed = false;
  epoch += 1;
}

// 注销：撤回同意，删除服务端的全部个人信息。用户确认并删除成功时返回 true
function withdraw() {
  return new Promise((resolve) => {
    wx.showModal({
      title: '注销并删除数据',
      content: '将撤回你对用户协议和隐私保护指引的同意，并删除你在有据的关注、微信提醒、纠错记录，以及本机的导出记录和文件，删除后不能恢复。',
      confirmText: '注销',
      confirmColor: '#E5484D',
      success: (res) => {
        if (!res.confirm) { resolve(false); return; }
        api.deleteMe()
          .then(() => {
            exportsLog.clear();
            // 文件缺失或无法删除不影响注销；清空本机缓存也涵盖后续新增的个人数据。
            try { removeFiles(); } catch (e) { /* 忽略文件清理失败 */ }
            try { wx.clearStorageSync(); } catch (e) { forget(); }
            outdated = false;
            notAgreed = true;
            epoch += 1;
            wx.showToast({ title: '已注销，数据已删除', icon: 'none' });
            resolve(true);
          })
          .catch((err) => {
            wx.showToast({ title: err.message || '注销失败，请稍后再试', icon: 'none' });
            resolve(false);
          });
      },
      fail: () => resolve(false),
    });
  });
}

// 服务端记录的是旧版本的同意（协议已更新）
function consentOutdated() {
  return outdated;
}

module.exports = {
  ensureConsent, syncConsent, applyServer, consentEpoch, consentOpen, consentOutdated, agreedHere, remember, forget, withdraw, markAgreed, CONSENT_PAGE,
};
