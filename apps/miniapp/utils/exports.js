// 导出记录：只存在这台手机上（不上传），最多 20 条。
// 本机只保留最近一次导出的文件（见 exporter.js），更早的记录显示"文件已清理"，可以重新导出。
const KEY = 'youjuExports';
const LIMIT = 20;

function list() {
  try { return wx.getStorageSync(KEY) || []; } catch (e) { return []; }
}

function add(record) {
  const items = [record].concat(list()).slice(0, LIMIT);
  try { wx.setStorageSync(KEY, items); } catch (e) { /* 存不下不影响导出 */ }
}

function remove(id) {
  try { wx.setStorageSync(KEY, list().filter((r) => r.id !== id)); } catch (e) { /* 忽略 */ }
}

function clear() {
  try { wx.removeStorageSync(KEY); } catch (e) { /* 忽略 */ }
}

module.exports = { list, add, remove, clear };
