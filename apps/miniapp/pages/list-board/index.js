const api = require('../../utils/api.js');
const { cnDate } = require('../../utils/format.js');

const PAGE_SIZE = 20;
const TYPES = ['全部', '高新技术企业', '专精特新', '科技型中小企业', '其他'];

Page({
  data: {
    statusBarHeight: 20,
    navHeight: 44,
    days: 30,
    page: 1,
    total: 0,
    items: [],
    stats: { by_year: [], by_province: [], by_type: [] },
    provinces: ['不限省份'],
    years: ['不限年度'],
    types: TYPES,
    province: '',
    year: null,
    type: '',
    loading: false,
    error: '',
  },

  onLoad() {
    const { statusBarHeight, navHeight } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight });
    this.load(true);
  },

  goBack() {
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  load(reset) {
    if (!reset && this.data.loading) return;
    const page = reset ? 1 : this.data.page + 1;
    const requestSeq = (this.requestSeq || 0) + 1;
    this.requestSeq = requestSeq;
    this.setData({ loading: true, error: '' });
    api.listBoard({
      days: this.data.days,
      year: this.data.year || '',
      province: this.data.province,
      type: this.data.type,
      page,
      size: PAGE_SIZE,
    }).then((res) => {
      if (requestSeq !== this.requestSeq) return;
      const recent = res.recent || { total: 0, items: [] };
      const items = (recent.items || []).map((item) => Object.assign({}, item, {
        dateText: item.date ? cnDate(item.date) : '日期未标明',
      }));
      const stats = res.stats || { by_year: [], by_province: [], by_type: [] };
      const provinceItems = stats.by_province || [];
      const yearItems = stats.by_year || [];
      this.setData({
        items: reset ? items : this.data.items.concat(items),
        total: recent.total || 0,
        page,
        stats,
        provinces: ['不限省份'].concat(provinceItems.map((item) => item.province)),
        years: ['不限年度'].concat(yearItems.map((item) => String(item.year))),
        loading: false,
      });
    }).catch((err) => {
      if (requestSeq !== this.requestSeq) return;
      this.setData({ loading: false, error: `加载失败：${err.message}` });
    });
  },

  retry() { this.load(true); },

  pickProvince(e) {
    const index = Number(e.detail.value);
    const value = index === 0 ? '' : this.data.provinces[index];
    this.setData({ province: value, items: [] });
    this.load(true);
  },

  pickYear(e) {
    const index = Number(e.detail.value);
    const value = index === 0 ? null : Number(this.data.years[index]);
    this.setData({ year: value, items: [] });
    this.load(true);
  },

  pickType(e) {
    const index = Number(e.detail.value);
    const value = index === 0 ? '' : this.data.types[index];
    this.setData({ type: value, items: [] });
    this.load(true);
  },

  more() {
    if (this.data.items.length < this.data.total) this.load(false);
  },

  open(e) {
    wx.navigateTo({ url: `/pages/detail/index?id=${e.currentTarget.dataset.id}` });
  },
});
