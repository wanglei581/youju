const api = require('../../utils/api');
const labels = require('../../utils/labels');
const ui = require('../../utils/ui');

Page({
  data: { state: 'loading', items: [], error: '' },
  onLoad() { this.load(); },
  onPullDownRefresh() { this.load().then(() => wx.stopPullDownRefresh()); },
  load() {
    this.setData({ state: 'loading', error: '' });
    return api.listSources().then((data) => {
      const items = (Array.isArray(data.items) ? data.items : []).map(labels.decorateSource);
      this.setData({ items, state: items.length ? 'ready' : 'empty' });
    }, (error) => this.setData({ state: 'error', error: error.message || '来源加载失败' }));
  },
  onCopy(e) { ui.copyLink(e.currentTarget.dataset.url, '来源链接'); },
});
