const api = require('../../utils/api.js');
const { todayISO } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');

const SCOPES = [
  { key: 'apply', label: '查申报', placeholder: '搜申报通知，如"高新技术企业"' },
  { key: 'list', label: '查名单', placeholder: '输入企业全称，查上过哪些名单' },
  { key: 'policy', label: '查政策', placeholder: '搜政策文件，如"数字经济"' },
  { key: 'number', label: '查文号', placeholder: '输入文号，如"辽工信投资〔2026〕130号"' },
];
const TABS = [
  { key: 'all', label: '最新' },
  { key: '申报通知', label: '申报通知' },
  { key: '名单', label: '名单与公示' },
  { key: '政策', label: '政策' },
];

Page({
  data: {
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
    scopes: SCOPES,
    scope: 0,
    tabs: TABS,
    tab: 'all',
    hot: ['高新技术企业', '专精特新', '科技型中小企业'],
    stats: null,
    items: [],
    shown: [],
  },

  onLoad() {
    const { statusBarHeight, navHeight, menuWidth } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight, menuWidth });
    api.home().then(({ stats, items }) => this.setData({ stats, items, shown: items }));
  },

  onHide() {
    stopDay(this);
  },

  onUnload() {
    stopDay(this);
  },

  onShow() {
    this.setData({ today: todayISO() });
    watchDay(this, (day) => this.setData({ today: day }));
    if (typeof this.getTabBar === 'function' && this.getTabBar()) {
      this.getTabBar().setData({ selected: 0 });
    }
  },

  pickScope(e) {
    const scope = e.currentTarget.dataset.index;
    if (SCOPES[scope].key === 'list') {
      wx.navigateTo({ url: '/pages/lists/index' });
      return;
    }
    this.setData({ scope });
  },

  pickTab(e) {
    const tab = e.currentTarget.dataset.key;
    const shown = tab === 'all' ? this.data.items : this.data.items.filter((d) => d.kind === tab);
    this.setData({ tab, shown });
  },

  goSearch(e) {
    const q = (e && e.currentTarget.dataset.q) || '';
    wx.navigateTo({ url: `/pages/search/index?q=${encodeURIComponent(q)}` });
  },

  goEntry(e) {
    wx.navigateTo({ url: e.currentTarget.dataset.url });
  },

  goFollow() {
    wx.switchTab({ url: '/pages/follow/index' });
  },

  goCoverage() {
    wx.switchTab({ url: '/pages/me/index' });
  },
});
