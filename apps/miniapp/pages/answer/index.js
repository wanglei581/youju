/* 找答案: search published records, see rights/coverage, select and export. */
const api = require('../../utils/api');
const config = require('../../utils/config');
const exportsUtil = require('../../utils/exports');
const labels = require('../../utils/labels');
const selection = require('../../utils/selection');
const settings = require('../../utils/settings');
const ui = require('../../utils/ui');
const watches = require('../../utils/watches');

const app = getApp();

Page({
  data: {
    baseLabel: '',
    healthTone: '',
    query: '',
    kind: '',
    kindIndex: 0,
    industry: '',
    region: '',
    kindOptions: labels.KIND_OPTIONS,
    templates: labels.TEMPLATES,
    hasFilters: false,
    filterSummary: '',

    status: 'idle', // idle | loading | ready | empty | error
    items: [],
    total: 0,
    capped: false,
    errorMessage: '',
    errorKind: '',
    resultLimit: config.RESULT_LIMIT,

    selectedMap: {},
    selectedCount: 0,
    selectedOffPage: 0,
    pageAllSelected: false,
    maxSelected: config.MAX_SELECTED,
    exporting: '',
  },

  onLoad() {
    this._seq = 0;
    this._task = null;
    this._items = [];
    this._lastBase = '';
    this.search();
  },

  onShow() {
    const pending = app.globalData.pendingFilters;
    if (pending) {
      app.globalData.pendingFilters = null;
      this.applyFilters(pending);
      this.search();
    } else if (this._lastBase && this._lastBase !== settings.getBaseUrl()) {
      this.search();
    }
    this.syncSelection();
    this.syncHealth();
  },

  onUnload() {
    if (this._task && this._task.abort) this._task.abort();
  },

  onPullDownRefresh() {
    this.search().then(() => wx.stopPullDownRefresh());
  },

  // ---------- filters ----------

  currentFilters() {
    return api.cleanFilters({
      q: this.data.query,
      kind: this.data.kind,
      industry: this.data.industry,
      region: this.data.region,
    });
  },

  applyFilters(filters) {
    const f = api.cleanFilters(filters);
    const kind = labels.KIND_LABELS[f.kind] ? f.kind : '';
    this.setData({
      query: f.q || '',
      kind,
      kindIndex: labels.kindIndex(kind),
      industry: f.industry || '',
      region: f.region || '',
    });
  },

  onQueryInput(e) {
    this.setData({ query: e.detail.value });
  },
  onIndustryInput(e) {
    this.setData({ industry: e.detail.value });
  },
  onRegionInput(e) {
    this.setData({ region: e.detail.value });
  },
  onClearQuery() {
    this.setData({ query: '' });
    this.search();
  },

  onKindChange(e) {
    const index = Number(e.detail.value) || 0;
    const option = labels.KIND_OPTIONS[index] || labels.KIND_OPTIONS[0];
    this.setData({ kindIndex: index, kind: option.value });
    this.search();
  },

  onTemplate(e) {
    const kind = e.currentTarget.dataset.kind;
    const next = this.data.kind === kind ? '' : kind;
    this.setData({ kind: next, kindIndex: labels.kindIndex(next) });
    this.search();
  },

  onClearFilters() {
    this.setData({ query: '', kind: '', kindIndex: 0, industry: '', region: '' });
    this.search();
  },

  onSearch() {
    this.search();
  },

  // ---------- search ----------

  search() {
    const filters = this.currentFilters();
    const seq = ++this._seq;
    if (this._task && this._task.abort) this._task.abort();
    const baseUrl = settings.getBaseUrl();
    this._lastBase = baseUrl;
    this.setData({
      status: 'loading',
      errorMessage: '',
      errorKind: '',
      baseLabel: settings.shortLabel(baseUrl),
      hasFilters: Object.keys(filters).length > 0,
      filterSummary: labels.describeFilters(filters),
    });
    const task = api.searchResources(filters);
    this._task = task;
    return task.then((data) => {
      if (seq !== this._seq) return;
      const items = (Array.isArray(data.items) ? data.items : []).map(labels.decorateResource);
      this._items = items;
      this.setData({
        status: items.length ? 'ready' : 'empty',
        items,
        total: items.length,
        capped: items.length >= config.RESULT_LIMIT,
        healthTone: 'ok',
      });
      this.syncSelection();
    }, (error) => {
      if (seq !== this._seq || error.kind === 'abort') return;
      this._items = [];
      this.setData({
        status: 'error',
        items: [],
        total: 0,
        capped: false,
        errorMessage: error.message || '未知错误',
        errorKind: error.kind || '',
        healthTone: error.kind === 'http' ? '' : 'warn',
      });
      this.syncSelection();
    });
  },

  syncHealth() {
    const health = app.globalData.health;
    if (health && health.baseUrl === settings.getBaseUrl() && this.data.status !== 'error') {
      this.setData({ healthTone: health.ok ? 'ok' : 'warn' });
    }
  },

  // ---------- selection ----------

  syncSelection() {
    const ids = selection.ids();
    const map = {};
    ids.forEach((id) => { map[id] = true; });
    const onPage = this._items.filter((item) => map[item.id]).length;
    this.setData({
      selectedMap: map,
      selectedCount: ids.length,
      selectedOffPage: ids.length - onPage,
      pageAllSelected: this._items.length > 0 && onPage === this._items.length,
    });
  },

  onToggle(e) {
    const result = selection.toggle(e.detail.id, e.detail.title);
    if (!result.ok && result.reason === 'limit') {
      ui.toast('一次最多导出 ' + config.MAX_SELECTED + ' 条');
    }
    this.syncSelection();
  },

  onTogglePage() {
    if (!this._items.length) return;
    if (this.data.pageAllSelected) {
      selection.removeMany(this._items.map((item) => item.id));
    } else {
      const result = selection.addMany(this._items);
      if (result.limited) ui.toast('已达上限 ' + config.MAX_SELECTED + ' 条，部分未选中');
    }
    this.syncSelection();
  },

  onClearSelection() {
    if (this.data.exporting) return;
    selection.clear();
    this.syncSelection();
  },

  // ---------- navigation & actions ----------

  onOpen(e) {
    wx.navigateTo({ url: '/pages/detail/index?id=' + e.detail.id });
  },

  onCopy(e) {
    ui.copyLink(e.detail.url, '原站链接');
  },

  onBrowseSources() {
    wx.navigateTo({ url: '/pages/sources/index' });
  },

  onOpenSettings() {
    wx.navigateTo({ url: '/pages/settings/index' });
  },

  onWatchQuery() {
    if (this.data.status !== 'ready' && this.data.status !== 'empty') return;
    const filters = this.currentFilters();
    const result = watches.create({ filters }, this._items);
    if (!result.ok) {
      ui.toast(result.message);
      return;
    }
    ui.confirm({
      title: '已加入关注',
      content: '当前 ' + this._items.length + ' 条结果已作为基线。本版本不会自动监测或推送；之后请在「关注」页手动点「检查更新」对比变化。',
      confirmText: '去关注页',
      cancelText: '留在这里',
    }).then((go) => {
      if (go) wx.switchTab({ url: '/pages/watch/index' });
    });
  },

  // ---------- export ----------

  onExport(e) {
    const fmt = e.currentTarget.dataset.format;
    if (this.data.exporting || !selection.count()) return;
    const items = selection.list();
    const label = exportsUtil.FORMAT_LABELS[fmt];
    this.setData({ exporting: fmt });
    wx.showLoading({ title: '正在生成 ' + label, mask: true });
    exportsUtil.run(items, fmt).then((entry) => {
      wx.hideLoading();
      this.setData({ exporting: '' });
      this.syncSelection();
      return ui.confirm({
        title: label + ' 文件已生成',
        content: entry.displayName + '（' + entry.count + ' 条）已保存到本机，可在「文件」页再次打开或转发。',
        confirmText: '打开',
        cancelText: '稍后',
      }).then((open) => {
        if (open) return exportsUtil.openFile(entry).catch((error) => ui.alert('无法打开', error.message));
        return null;
      });
    }, (error) => {
      wx.hideLoading();
      this.setData({ exporting: '' });
      this.syncSelection();
      ui.alert('导出失败', error.message || '未知错误');
    });
  },
});
