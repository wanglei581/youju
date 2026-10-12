// 先显示上次的数据（10/12 冷启动诊断）：接口服务空闲后要重新启动，打开页面时新数据可能要等几十秒。
// 页面每次取到数据就存一份到本机，下次打开先显示（3 天内的），同时去取新的。
// 只存公开数据（通知、名单、统计），不存客户信息；截止日期显示时按手机日期重新算。
const { clockText } = require('./format.js');

const MAX_AGE = 3 * 86400000;

function readCache(key, maxAge = MAX_AGE) {
  try {
    const value = wx.getStorageSync(key);
    if (value && value.at && value.data && Date.now() - value.at < maxAge) return value;
  } catch (e) { /* 读不到就当没有 */ }
  return null;
}

function writeCache(key, data) {
  try { wx.setStorageSync(key, { at: Date.now(), data }); } catch (e) { /* 存不了不影响显示 */ }
}

// "上次更新 10月12日 21:05"
function cachedLabel(at) {
  return `上次更新 ${clockText(new Date(at).toISOString())}`;
}

module.exports = { readCache, writeCache, cachedLabel };
