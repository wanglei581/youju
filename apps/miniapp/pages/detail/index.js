const api = require('../../utils/api');
const labels = require('../../utils/labels');
const selection = require('../../utils/selection');
const exportsUtil = require('../../utils/exports');
const ui = require('../../utils/ui');

Page({
  data: { id: 0, state: 'loading', item: null, error: '', selected: false, exporting: '' },
  onLoad(options) { this.setData({ id: Number(options.id) || 0 }); this.load(); },
  onShow() { if (this.data.id) this.setData({ selected: selection.has(this.data.id) }); },
  load() {
    if (!this.data.id) { this.setData({ state: 'error', error: '资料编号无效' }); return; }
    this.setData({ state: 'loading', error: '' });
    api.getResource(this.data.id).then((raw) => {
      this.setData({ state: 'ready', item: labels.decorateResource(raw), selected: selection.has(this.data.id) });
    }, (error) => this.setData({ state: 'error', error: error.message || '资料不可用' }));
  },
  onCopy() { if (this.data.item) ui.copyLink(this.data.item.originalUrl, '原站链接'); },
  onSelect() {
    const item = this.data.item;
    if (!item) return;
    const result = selection.toggle(item.id, item.title);
    if (!result.ok) { ui.toast('选择数量已达上限'); return; }
    this.setData({ selected: result.selected });
  },
  onExport(e) {
    if (!this.data.item || this.data.exporting) return;
    const fmt = e.currentTarget.dataset.format;
    this.setData({ exporting: fmt });
    wx.showLoading({ title: '正在生成', mask: true });
    exportsUtil.run([{ id: this.data.item.id, title: this.data.item.title }], fmt).then((entry) => {
      wx.hideLoading(); this.setData({ exporting: '' });
      return ui.confirm({ title: '文件已生成', content: entry.displayName + ' 已保存到本机。', confirmText: '打开', cancelText: '稍后' }).then((ok) => {
        if (ok) return exportsUtil.openFile(entry).catch((error) => ui.alert('打开失败', error.message));
        return null;
      });
    }, (error) => { wx.hideLoading(); this.setData({ exporting: '' }); ui.alert('导出失败', error.message); });
  },
});
