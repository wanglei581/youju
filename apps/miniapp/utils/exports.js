// 导出记录：只存在这台手机上（不上传），最多 20 条。
// 本机只保留最近一次导出的文件（见 exporter.js），更早的记录显示"文件已清理"，可以重新导出。
const KEY = 'youjuExports';
const LIMIT = 20;

function list() {
  try { return wx.getStorageSync(KEY) || []; } catch (e) { return []; }
}

// 以下都返回是否成功。add 的 replaces：重新导出时被替代的旧记录编号，和新增在同一次写入里完成
function add(record, replaces) {
  const rest = list().filter((r) => r.id !== replaces);
  try { wx.setStorageSync(KEY, [record].concat(rest).slice(0, LIMIT)); return true; } catch (e) { return false; }
}

function clear() {
  try { wx.removeStorageSync(KEY); return true; } catch (e) { return false; }
}

module.exports = { list, add, clear };
