const api = require('../../utils/api.js');
const { todayISO, cnDate } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');

const CHIPS = ['全部', '科技型中小企业', '专精特新'];

Page({
  data: {
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
    name: '',
    queried: '',
    looking: false,
    result: null,
    lookupError: '',
    coverageSince: '',
    demo: false,
    chips: CHIPS,
    chip: '全部',
    weekTotal: 0,
    total: 0,
    items: [],
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
  },

  onLoad() {
    const { statusBarHeight, navHeight, menuWidth } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight, menuWidth });
    this.reload();
  },

  reload() {
    this.setData({ error: '' });
    api.lists()
      .then((res) => this.setData({ weekTotal: res.week_total, total: res.total, items: res.items }))
      .catch((err) => this.setData({ error: `加载失败：${err.message}` }));
  },

  back() {
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  onInput(e) {
    this.setData({ name: e.detail.value });
  },

  // 只查企业和机构，不查个人（像个人姓名的，服务端返回说明）
  query() {
    const name = this.data.name.trim();
    if (name.replace(/\s+/g, '').length < 2) {
      wx.showToast({ title: '请输入企业全称或统一社会信用代码', icon: 'none' });
      return;
    }
    const seq = (this.lookupSeq || 0) + 1;
    this.lookupSeq = seq; // 只用最后一次查询的结果
    this.setData({ queried: name, looking: true, result: null, lookupError: '', demo: !api.isLive() });
    api.lookup(name)
      .then((result) => {
        if (seq !== this.lookupSeq) return;
        const since = result.coverage && result.coverage.since;
        this.setData({ looking: false, result, coverageSince: since ? cnDate(since) : '' });
      })
      .catch((err) => {
        if (seq !== this.lookupSeq) return;
        this.setData({ looking: false, lookupError: err.message });
      });
  },

  // 点名称相近的企业：换成这个名称再查一次
  pickSimilar(e) {
    this.setData({ name: e.currentTarget.dataset.name });
    this.query();
  },

  moreLists() {
    wx.navigateTo({ url: '/pages/search/index?kind=名单&time=不限' });
  },

  pickChip(e) {
    this.setData({ chip: e.currentTarget.dataset.chip });
  },
});
