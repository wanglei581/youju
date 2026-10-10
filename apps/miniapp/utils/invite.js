// 卡片带来的码只保留最近一份；不在首页显示，也不自动建立账号。
const KEY = 'youjuInvite';
const TTL = 30 * 24 * 60 * 60 * 1000;
function clear() { try { wx.removeStorageSync(KEY); } catch (e) { /* 下次读取仍检查期限 */ } }
function read() {
  try {
    const row = wx.getStorageSync(KEY);
    if (row && /^[A-Za-z0-9_-]{12}$/.test(row.code) && Number.isFinite(row.at) && Date.now() >= row.at && Date.now() - row.at < TTL) return row.code;
  } catch (e) { /* 存储不可用不影响注册 */ }
  clear();
  return '';
}
function capture(query) {
  read();
  const code = query && query.inv;
  if (typeof code !== 'string' || !/^[A-Za-z0-9_-]{12}$/.test(code)) return;
  try { wx.setStorageSync(KEY, { code, at: Date.now() }); } catch (e) { /* 存储不可用不影响浏览 */ }
}
module.exports = { KEY, TTL, read, clear, capture };
