const api = require('./api.js');
function rewardDate(iso) {
  const stamp = Date.parse(iso);
  return Number.isFinite(stamp) ? new Date(stamp + 8 * 3600000).toISOString().slice(0, 10) : '';
}
function clueTitles(items) {
  const titles = {};
  items.forEach((item) => {
    if (!titles[item.document_id]) titles[item.document_id] = api.detail(item.document_id).then((doc) => doc.title, () => '通知标题暂不可用');
  });
  return Promise.all(items.map((item) => titles[item.document_id].then((title) => Object.assign({}, item, { title }))));
}
function removeClue(items, id) {
  const row = items.find((item) => String(item.id) === String(id));
  if (!row || row.status !== '待核对') return Promise.resolve(false);
  return new Promise((resolve) => wx.showModal({
    title: '删除线索', content: '删除这条待核对的线索？删除后不能恢复。', confirmText: '删除',
    success: (res) => {
      if (!res.confirm) { resolve(false); return; }
      api.deleteDeadlineClue(id).then(() => resolve(true), (err) => {
        wx.showToast({ title: err.message || '没删成，请稍后再试', icon: 'none' }); resolve(false);
      });
    }, fail: () => resolve(false),
  }));
}
module.exports = { rewardDate, clueTitles, removeClue };
