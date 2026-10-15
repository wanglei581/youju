// 导出 Excel / Word：请求导出接口，把文件存到本机后用微信打开；打开后右上角菜单可转发给朋友或电脑版微信
const api = require('./api.js');
const { todayISO } = require('./format.js');
const exportsLog = require('./exports.js');

const MAX = 200;

function chooseFormat() {
  return new Promise((resolve) => {
    wx.showActionSheet({
      itemList: ['导出 Excel', '导出 Word'],
      success: (res) => resolve(res.tapIndex === 0 ? 'xlsx' : 'docx'),
      fail: () => resolve(null), // 用户取消
    });
  });
}

// 本机只保留最近一次导出的文件：新文件写好、记录存好之后再删旧的，免得占用手机空间（keep 为空时全部删除）。
// 返回没删掉的文件数
function clearOld(fs, keep) {
  let failed = 0;
  try {
    fs.readdirSync(wx.env.USER_DATA_PATH)
      .filter((name) => name.indexOf('有据导出_') === 0 && `${wx.env.USER_DATA_PATH}/${name}` !== keep)
      .forEach((name) => { try { fs.unlinkSync(`${wx.env.USER_DATA_PATH}/${name}`); } catch (e) { failed += 1; } });
  } catch (e) {
    failed += 1;
  }
  return failed;
}

function write(fs, filePath, buffer) {
  return new Promise((resolve, reject) => {
    fs.writeFile({ filePath, data: buffer, success: () => resolve(filePath), fail: reject });
  });
}

// 同一毫秒的并行保存也必须使用不同路径，取消旧下载时不能误删新文件。
let saveSequence = 0;
function save(buffer, format) {
  const fs = wx.getFileSystemManager();
  const filePath = `${wx.env.USER_DATA_PATH}/有据导出_${todayISO()}_${Date.now()}_${++saveSequence}.${format}`;
  // 写入失败就提示，不动旧文件
  return write(fs, filePath, buffer).catch(() => { throw new Error('保存文件失败，请稍后再试'); });
}

// 仅删除本次保存返回的应用导出路径，不接受任意文件路径或目录穿越。
function remove(filePath) {
  const prefix = `${wx.env.USER_DATA_PATH}/有据导出_`;
  if (typeof filePath !== 'string' || !filePath.startsWith(prefix) || !/^[^/\\]+\.(xlsx|docx)$/.test(filePath.slice(prefix.length))) return false;
  try { wx.getFileSystemManager().unlinkSync(filePath); return true; }
  catch (err) { return !exists(filePath); }
}

// 清空导出记录时，把本机的导出文件也删掉；返回没删掉的文件数
function removeFiles() {
  return clearOld(wx.getFileSystemManager(), null);
}

function exists(filePath) {
  try {
    wx.getFileSystemManager().accessSync(filePath);
    return true;
  } catch (e) {
    return false;
  }
}

function open(filePath, format) {
  return new Promise((resolve, reject) => {
    wx.openDocument({
      filePath,
      fileType: format,
      showMenu: true,
      success: () => resolve(true),
      fail: () => reject(new Error('打开文件失败，请稍后再试')),
    });
  });
}

// 返回 true 表示已打开导出文件。options.label：导出记录里显示的名称；options.format：重新导出时沿用原格式；
// options.replaces：重新导出时被替代的旧记录编号（新文件和记录存好就删掉旧记录，不管打开是否成功）
function exportDocs(ids, options) {
  const opts = options || {};
  if (!ids.length) {
    wx.showToast({ title: '请先选择文件', icon: 'none' });
    return Promise.resolve(false);
  }
  if (ids.length > MAX) {
    wx.showToast({ title: `一次最多导出 ${MAX} 份`, icon: 'none' });
    return Promise.resolve(false);
  }
  if (!api.isLive()) {
    wx.showToast({ title: '演示数据不能导出，连上接口后可用', icon: 'none' });
    return Promise.resolve(false);
  }
  const pick = opts.format ? Promise.resolve(opts.format) : chooseFormat();
  return pick.then((format) => {
    if (!format) return false;
    wx.showLoading({ title: '正在生成…', mask: true });
    let size = 0;
    return api.exportFile(ids, format)
      .then((buffer) => { size = buffer.byteLength || 0; return save(buffer, format); })
      .then((filePath) => {
        const recorded = exportsLog.add({
          id: Date.now(),
          label: opts.label || `${ids.length} 份文件`,
          count: ids.length,
          format,
          size,
          at: new Date().toISOString(),
          path: filePath,
          ids,
        }, opts.replaces);
        // 记录存好了（旧记录也在同一次写入里替换掉了）才删旧文件；没存上就保留旧文件，旧记录仍能打开
        if (recorded) clearOld(wx.getFileSystemManager(), filePath);
        wx.hideLoading();
        return open(filePath, format);
      })
      .catch((err) => {
        wx.hideLoading();
        wx.showToast({ title: err.message || '导出失败', icon: 'none' });
        return false;
      });
  });
}

module.exports = { exportDocs, MAX, open, exists, removeFiles, save, clearOld, remove };
