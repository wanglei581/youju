/* Selected-record export: POST /api/public/exports -> ArrayBuffer -> file in
 * wx.env.USER_DATA_PATH/exports -> local history -> wx.openDocument.
 * The server does not keep export records; this history exists only on this device. */
const api = require('./api');
const config = require('./config');
const format = require('./format');
const selection = require('./selection');
const settings = require('./settings');
const storage = require('./storage');

const KEY = config.STORAGE_KEYS.exports;
const FORMAT_LABELS = { xlsx: 'Excel', docx: 'Word' };

function exportDir() {
  return wx.env.USER_DATA_PATH + '/exports';
}

function fs() {
  return wx.getFileSystemManager();
}

function ensureDir() {
  const dir = exportDir();
  try {
    fs().accessSync(dir);
  } catch (error) {
    fs().mkdirSync(dir, true);
  }
  return dir;
}

function fileExists(path) {
  if (!path) return false;
  try {
    fs().accessSync(path);
    return true;
  } catch (error) {
    return false;
  }
}

function describeFsError(err) {
  const msg = String((err && err.errMsg) || '');
  if (/exceeded|limit|storage/i.test(msg)) return '本机小程序存储空间不足，请在「文件」页删除旧文件后重试。';
  return '文件保存失败：' + (msg || '未知错误');
}

function writeBuffer(buffer, fmt, count) {
  return new Promise((resolve, reject) => {
    let dir;
    try {
      dir = ensureDir();
    } catch (error) {
      reject(Object.assign(new Error(describeFsError(error)), { kind: 'fs' }));
      return;
    }
    const now = Date.now();
    const fileName = 'industry-results-' + format.stamp(now) + '-' + Math.floor(Math.random() * 1000) + '.' + fmt;
    const filePath = dir + '/' + fileName;
    fs().writeFile({
      filePath,
      data: buffer,
      success() {
        resolve({
          filePath,
          fileName,
          displayName: '行业资料_' + format.stamp(now).slice(0, 13).replace('-', '_') + '_' + count + '条.' + fmt,
          createdAt: now,
        });
      },
      fail(err) {
        reject(Object.assign(new Error(describeFsError(err)), { kind: 'fs' }));
      },
    });
  });
}

function history() {
  const saved = storage.read(KEY, []);
  return Array.isArray(saved) ? saved.filter((entry) => entry && entry.id && entry.filePath) : [];
}

function saveHistory(list) {
  storage.write(KEY, list.slice(0, config.MAX_EXPORT_HISTORY));
}

function unlink(path) {
  if (!fileExists(path)) return;
  try {
    fs().unlinkSync(path);
  } catch (error) {
    /* ignore: file may already be gone */
  }
}

function addHistory(entry) {
  const list = history();
  list.unshift(entry);
  // Files beyond the history limit are deleted so they do not linger unseen.
  list.slice(config.MAX_EXPORT_HISTORY).forEach((old) => unlink(old.filePath));
  saveHistory(list);
}

function removeEntry(id) {
  const list = history();
  const entry = list.find((item) => item.id === id);
  if (entry) unlink(entry.filePath);
  saveHistory(list.filter((item) => item.id !== id));
}

function clearAll() {
  history().forEach((entry) => unlink(entry.filePath));
  storage.remove(KEY);
}

/**
 * Extracts a withdrawn record id from the server error "资料 12 未发布或已撤回".
 */
function withdrawnId(message) {
  const match = /资料\s*(\d+)\s*未发布或已撤回/.exec(String(message || ''));
  return match ? Number(match[1]) : 0;
}

/**
 * Runs an export for `items` ([{ id, title }]). Resolves with the history entry.
 * On a "withdrawn" error the record is removed from the current selection and
 * the error message says so.
 */
function run(items, fmt) {
  const rows = (items || []).filter((item) => Number.isInteger(Number(item.id)) && Number(item.id) > 0);
  const ids = rows.map((item) => Number(item.id));
  if (!ids.length) return Promise.reject(new Error('请先选择要导出的资料'));
  if (ids.length > config.MAX_SELECTED) return Promise.reject(new Error('一次最多导出 ' + config.MAX_SELECTED + ' 条'));
  return api.exportRecords(ids, fmt).then((result) => writeBuffer(result.data, fmt, ids.length).then((file) => {
    const entry = {
      id: 'e' + file.createdAt.toString(36) + Math.floor(Math.random() * 1e4).toString(36),
      format: fmt,
      count: ids.length,
      items: rows.map((item) => ({ id: Number(item.id), title: format.truncate(item.title, 80) })),
      filePath: file.filePath,
      fileName: file.fileName,
      displayName: file.displayName,
      size: result.size,
      createdAt: file.createdAt,
      baseUrl: settings.getBaseUrl(),
    };
    addHistory(entry);
    return entry;
  })).catch((error) => {
    const withdrawn = withdrawnId(error && error.message);
    if (withdrawn && selection.remove(withdrawn)) {
      error.message += '；已从所选中移除，请重新导出。';
    }
    throw error;
  });
}

function openFile(entry) {
  return new Promise((resolve, reject) => {
    if (!fileExists(entry.filePath)) {
      reject(new Error('本机文件已不存在（可能被清理），可重新导出'));
      return;
    }
    wx.openDocument({
      filePath: entry.filePath,
      fileType: entry.format,
      showMenu: true,
      success: resolve,
      fail(err) {
        reject(new Error('无法打开文件：' + String((err && err.errMsg) || '当前环境不支持预览')));
      },
    });
  });
}

/** Sends the file to a WeChat chat (real device, base library ≥ 2.16.1). */
function shareFile(entry) {
  return new Promise((resolve, reject) => {
    if (typeof wx.shareFileMessage !== 'function') {
      reject(new Error('当前微信版本不支持发送文件，请先「打开」后用右上角菜单转发'));
      return;
    }
    if (!fileExists(entry.filePath)) {
      reject(new Error('本机文件已不存在，可重新导出'));
      return;
    }
    wx.shareFileMessage({
      filePath: entry.filePath,
      fileName: entry.displayName,
      success: resolve,
      fail(err) {
        const msg = String((err && err.errMsg) || '');
        if (/cancel/i.test(msg)) resolve();
        else reject(new Error('发送失败：' + (msg || '当前环境不支持')));
      },
    });
  });
}

module.exports = {
  FORMAT_LABELS,
  run,
  history,
  removeEntry,
  clearAll,
  fileExists,
  openFile,
  shareFile,
  withdrawnId,
};
