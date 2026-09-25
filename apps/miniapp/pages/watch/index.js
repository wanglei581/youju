const watches = require('../../utils/watches');
const labels = require('../../utils/labels');
const format = require('../../utils/format');
const ui = require('../../utils/ui');
const app = getApp();

function decorate(watch) {
  const changes = watch.changes || {};
  return Object.assign({}, watch, {
    filtersLabel: labels.describeFilters(watch.filters) || '全部已发布资料',
    checkedLabel: watch.lastSuccessAt ? format.dateTime(watch.lastSuccessAt) : '尚未建立基线',
    attemptLabel: watch.lastAttemptAt ? format.dateTime(watch.lastAttemptAt) : '',
    addedTotal: changes.addedTotal || 0,
    revisedTotal: changes.revisedTotal || 0,
    missingTotal: changes.missingTotal || 0,
    changes,
  });
}

Page({
  data: { items: [], checkingId: '' },
  onShow() { this.reload(); },
  reload() { this.setData({ items: watches.list().map(decorate) }); },
  onFind() { wx.switchTab({ url: '/pages/answer/index' }); },
  onCheck(e) {
    const id = e.currentTarget.dataset.id;
    if (this.data.checkingId) return;
    this.setData({ checkingId: id });
    watches.refresh(id).then((result) => {
      this.setData({ checkingId: '' });
      this.reload();
      ui.toast(watches.resultSummary(result));
    });
  },
  onSearch(e) {
    const item = watches.get(e.currentTarget.dataset.id);
    if (!item) return;
    app.globalData.pendingFilters = item.filters;
    wx.switchTab({ url: '/pages/answer/index' });
  },
  onDelete(e) {
    const id = e.currentTarget.dataset.id;
    ui.confirm({ title: '删除关注？', content: '本机保存的查询范围与上次对比基线将一并删除。', confirmText: '删除' }).then((ok) => {
      if (ok) { watches.remove(id); this.reload(); }
    });
  },
});
