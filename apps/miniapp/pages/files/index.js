const exportsUtil = require('../../utils/exports');
const format = require('../../utils/format');
const selection = require('../../utils/selection');
const ui = require('../../utils/ui');

Page({
  data: { items: [], selectedCount: 0 },
  onShow() { this.reload(); },
  reload() {
    this.setData({
      selectedCount: selection.count(),
      items: exportsUtil.history().map((entry) => Object.assign({}, entry, {
        when: format.dateTime(entry.createdAt),
        sizeLabel: format.bytes(entry.size),
        exists: exportsUtil.fileExists(entry.filePath),
      })),
    });
  },
  onFind() { wx.switchTab({ url: '/pages/answer/index' }); },
  onOpen(e) {
    const item = exportsUtil.history().find((entry) => entry.id === e.currentTarget.dataset.id);
    if (item) exportsUtil.openFile(item).catch((error) => ui.alert('打开失败', error.message));
  },
  onShare(e) {
    const item = exportsUtil.history().find((entry) => entry.id === e.currentTarget.dataset.id);
    if (item) exportsUtil.shareFile(item).catch((error) => ui.alert('发送失败', error.message));
  },
  onDelete(e) {
    const id = e.currentTarget.dataset.id;
    ui.confirm({ title: '删除本机文件？', content: '删除后如需该文件，请重新查询并导出。', confirmText: '删除' }).then((ok) => {
      if (ok) { exportsUtil.removeEntry(id); this.reload(); }
    });
  },
  onClear() {
    ui.confirm({ title: '清空文件？', content: '所有保存在本机小程序内的导出文件及历史记录将被删除。', confirmText: '清空' }).then((ok) => {
      if (ok) { exportsUtil.clearAll(); this.reload(); }
    });
  },
});
