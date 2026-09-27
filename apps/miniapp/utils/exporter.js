// 导出 Excel / Word：请求导出接口，把文件存到本机后用微信打开；打开后右上角菜单可转发给朋友或电脑版微信
const api = require('./api.js');
const { todayISO } = require('./format.js');

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

function save(buffer, format) {
  const filePath = `${wx.env.USER_DATA_PATH}/有据导出_${todayISO()}_${Date.now() % 100000}.${format}`;
  return new Promise((resolve, reject) => {
    wx.getFileSystemManager().writeFile({
      filePath,
      data: buffer,
      success: () => resolve(filePath),
      fail: () => reject(new Error('保存文件失败，请稍后再试')),
    });
  });
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

// 返回 true 表示已打开导出文件
function exportDocs(ids) {
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
  return chooseFormat().then((format) => {
    if (!format) return false;
    wx.showLoading({ title: '正在生成…', mask: true });
    return api.exportFile(ids, format)
      .then((buffer) => save(buffer, format))
      .then((filePath) => { wx.hideLoading(); return open(filePath, format); })
      .catch((err) => {
        wx.hideLoading();
        wx.showToast({ title: err.message || '导出失败', icon: 'none' });
        return false;
      });
  });
}

module.exports = { exportDocs, MAX };
