const api = require('../../utils/api.js');
const { todayISO } = require('../../utils/format.js');

const CHIPS = ['全部', '科技型中小企业', '专精特新'];

Page({
  data: {
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
    name: '',
    queried: '',
    chips: CHIPS,
    chip: '全部',
    weekTotal: 0,
    items: [],
  },

  onShow() {
    this.setData({ today: todayISO() });
  },

  onLoad() {
    const { statusBarHeight, navHeight, menuWidth } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight, menuWidth });
    api.lists().then((res) => this.setData({ weekTotal: res.week_total, items: res.items }));
  },

  back() {
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  onInput(e) {
    this.setData({ name: e.detail.value });
  },

  // 只查企业和机构，不查个人；名单明细接口（#29）就绪前先提示
  query() {
    const name = this.data.name.trim();
    if (!name) {
      wx.showToast({ title: '请输入企业全称或统一社会信用代码', icon: 'none' });
      return;
    }
    this.setData({ queried: name });
  },

  pickChip(e) {
    this.setData({ chip: e.currentTarget.dataset.chip });
  },
});
