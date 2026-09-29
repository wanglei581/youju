const api = require('../../utils/api.js');
const { exportDocs } = require('../../utils/exporter.js');

const PAGE_SIZE = 20;

Page({
  data: {
    days: 7,
    summary: { 7: 0, 14: 0, 30: 0 },
    items: [],
    total: 0,
    page: 1,
    loading: false,
    error: '',
    province: '',
    dept: '',
    provinces: ['不限省份'],
    depts: ['不限部门', '科技部门', '工信部门', '其他部门'],
  },

  onLoad() { this.load(true); },
  goBack() { wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) }); },

  load(reset) {
    if (this.data.loading) return;
    const page = reset ? 1 : this.data.page + 1;
    this.setData({ loading: true, error: '' });
    api.deadlineReport({ days: this.data.days, province: this.data.province, dept: this.data.dept, page, size: PAGE_SIZE })
      .then((res) => {
        const provinces = Object.keys(res.by_province || {}).sort();
        const depts = ['科技部门', '工信部门', '其他部门'];
        this.setData({
          summary: res.summary || { 7: 0, 14: 0, 30: 0 },
          items: reset ? res.items : this.data.items.concat(res.items),
          total: res.total, page, loading: false,
          provinces: ['不限省份'].concat(provinces), depts: ['不限部门'].concat(depts),
        });
      })
      .catch((err) => this.setData({ loading: false, error: err.message }));
  },

  retry() { this.load(true); },
  pickDays(e) { this.setData({ days: Number(e.currentTarget.dataset.days), items: [] }); this.load(true); },
  pickProvince(e) {
    const value = e.detail.value === 0 ? '' : this.data.provinces[e.detail.value];
    this.setData({ province: value, items: [] }); this.load(true);
  },
  pickDept(e) {
    const value = e.detail.value === 0 ? '' : this.data.depts[e.detail.value];
    this.setData({ dept: value, items: [] }); this.load(true);
  },
  more() { if (this.data.items.length < this.data.total) this.load(false); },
  open(e) { wx.navigateTo({ url: `/pages/detail/index?id=${e.currentTarget.dataset.id}` }); },
  exportOne(e) { exportDocs([e.currentTarget.dataset.id], { label: '截止雷达' }); },
});
