const api = require('../../utils/api.js');
const exportsLog = require('../../utils/exports.js');
const { exportDocs, open, exists, removeFiles } = require('../../utils/exporter.js');
const { shortTime } = require('../../utils/format.js');
const { openResult } = require('../../utils/advisor.js');
const { loginOpen } = require('../../utils/consent.js');

const FORMAT_NAME = { xlsx: 'Excel', docx: 'Word' };

// 转发时的文件名：去掉文件名里不能用的字符
function fileName(record) {
  const base = (record.label || '有据导出').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 40);
  return `${base}.${record.format}`;
}

// 导出记录只存在这台手机上（utils/exports.js）；演示模式显示演示数据
Page({
  data: { demo: true, files: [], loginOpen: loginOpen() },

  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 2 });
    this.load();
  },

  load() {
    this.records = exportsLog.list();
    const records = this.records.map((r) => ({
      id: r.id,
      badge: r.format.toUpperCase(),
      name: r.label,
      meta: `${r.count} ${r.source === 'advisor' ? '行' : '份'} · ${FORMAT_NAME[r.format] || r.format} · ${Math.max(1, Math.round((r.size || 0) / 1024))} KB · ${shortTime(r.at)}`,
      expired: !exists(r.path),
      local: true,
      canReexport: r.source !== 'advisor' || this.data.loginOpen,
    }));
    this.setData({ demo: !api.isLive(), files: records });
    if (!api.isLive()) api.files().then((files) => this.setData({ files: records.concat(files.map((f, i) => Object.assign({ id: `demo${i}` }, f))) }));
  },

  record(e) {
    const file = this.data.files[e.currentTarget.dataset.index];
    return file && this.records.find((record) => record.id === file.id);
  },

  open(e) {
    const r = this.record(e);
    if (!r) { wx.showToast({ title: '演示数据，导出后可以打开', icon: 'none' }); return; }
    open(r.path, r.format).catch((err) => wx.showToast({ title: err.message, icon: 'none' }));
  },

  // 转发到聊天；要在电脑上保存，发给"文件传输助手"
  forward(e) {
    const r = this.record(e);
    if (!r) { wx.showToast({ title: '演示数据，导出后可以转发', icon: 'none' }); return; }
    if (!wx.shareFileMessage) {
      wx.showToast({ title: '请点"打开"，再用右上角菜单转发', icon: 'none' });
      return;
    }
    wx.shareFileMessage({
      filePath: r.path,
      fileName: fileName(r),
      fail: (err) => {
        if (err && /cancel/.test(err.errMsg || '')) return;
        wx.showToast({ title: '没能转发，请点"打开"后用右上角菜单转发', icon: 'none' });
      },
    });
  },

  // 本机只保留最近一次导出的文件：更早的按原来的文件和格式重新导出
  reexport(e) {
    const r = this.record(e);
    if (!r) { wx.showToast({ title: '演示数据不能导出', icon: 'none' }); return; }
    if (r.source === 'advisor') {
      if (!this.data.loginOpen) return;
      openResult(r.taskId, { task: { kind: r.kind, rowCount: r.count } }).then(() => this.load())
        .catch((err) => { this.load(); wx.showToast({ title: err.message, icon: 'none' }); });
      return;
    }
    // 新文件和记录存好后，新记录替代这一条（打开失败也替代）
    exportDocs(r.ids, { label: r.label, format: r.format, replaces: r.id }).then(() => this.load());
  },

  clearAll() {
    wx.showModal({
      title: '清空导出记录',
      content: '清空这台手机上的导出记录和导出的文件？',
      confirmText: '清空',
      confirmColor: '#E5484D',
      success: (res) => {
        if (!res.confirm) return;
        // 记录和文件都要删，删完再看结果
        const cleared = exportsLog.clear();
        const filesLeft = removeFiles();
        this.load();
        wx.showToast({ title: cleared && filesLeft === 0 ? '已清空' : '有的记录或文件没删掉，请稍后再试', icon: 'none' });
      },
    });
  },
});
