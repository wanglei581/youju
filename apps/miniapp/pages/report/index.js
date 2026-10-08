const api = require('../../utils/api.js');
const { exportDocs } = require('../../utils/exporter.js');
const { todayISO, verifyingNote } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');

const PAGE_SIZE = 20;

Page({
  data: {
    statusBarHeight: 20,
    navHeight: 44,
    days: 7,
    summary: { 7: 0, 14: 0, 30: 0 },
    items: [],
    total: 0,
    page: 1,
    toolPickerOpen: false,
    // 没有内容时的说明：截止日期正在人工核对，目前已核对几条（服务端给的数字）
    verifyingNote: '',
    loading: false,
    error: '',
    province: '',
    dept: '',
    support: '',
    provinces: ['不限省份'],
    depts: ['不限部门', '科技部门', '工信部门', '其他部门'],
    supports: ['不限支持方式', '资金支持', '贷款贴息', '资格认定', '入库备案', '项目立项', '其他'],
  },

  onLoad() {
    const { statusBarHeight, navHeight } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight });
    this.load(true);
  },
  onShow() {
    if (this.loadedDay !== todayISO()) this.load(true);
    watchDay(this, () => this.load(true));
  },
  onHide() { stopDay(this); },
  onUnload() { stopDay(this); },
  goBack() { wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) }); },

  load(reset) {
    if (!reset && this.data.loading) return;
    const page = reset ? 1 : this.data.page + 1;
    const requestSeq = (this.requestSeq || 0) + 1;
    this.requestSeq = requestSeq;
    this.loadedDay = todayISO();
    const unfiltered = this.data.days === 30 && !this.data.province && !this.data.dept && !this.data.support;
    if (!this.optionsLoading && !this.filterOptionsLoaded && !unfiltered) this.loadOptions();
    this.setData({ loading: true, error: '' });
    api.deadlineReport({ days: this.data.days, province: this.data.province, dept: this.data.dept, support: this.data.support, page, size: PAGE_SIZE })
      .then((res) => {
        if (requestSeq !== this.requestSeq) return;
        if (unfiltered && !this.filterOptionsLoaded) this.setOptions(res);
        const provinces = this.data.provinces;
        const depts = ['科技部门', '工信部门', '其他部门'];
        this.setData({
          summary: res.summary || { 7: 0, 14: 0, 30: 0 },
          items: reset ? res.items : this.data.items.concat(res.items),
          total: res.total, page, loading: false,
          verifyingNote: verifyingNote(res.verified_total),
          provinces, depts: ['不限部门'].concat(depts),
        });
      })
      .catch((err) => {
        if (requestSeq !== this.requestSeq) return;
        this.setData({ loading: false, error: err.message });
      });
  },

  openToolPicker() { this.setData({ toolPickerOpen: true }); },
  closeToolPicker() { this.setData({ toolPickerOpen: false }); },
  setOptions(res) {
    this.filterOptionsLoaded = true;
    this.setData({ provinces: ['不限省份'].concat(Object.keys(res.by_province || {}).sort()) });
  },

  loadOptions() {
    this.optionsLoading = true;
    api.deadlineReport({ days: 30, province: '', dept: '', support: '', page: 1, size: 1 })
      .then((res) => this.setOptions(res))
      .catch(() => {})
      .finally(() => { this.optionsLoading = false; });
  },

  retry() { this.load(true); },
  pickDays(e) { this.setData({ days: Number(e.currentTarget.dataset.days), items: [] }); this.load(true); },
  pickProvince(e) {
    this.closeToolPicker();
    const index = Number(e.detail.value);
    const value = index === 0 ? '' : this.data.provinces[index];
    this.setData({ province: value, items: [] }); this.load(true);
  },
  pickDept(e) {
    this.closeToolPicker();
    const index = Number(e.detail.value);
    const value = index === 0 ? '' : this.data.depts[index];
    this.setData({ dept: value, items: [] }); this.load(true);
  },
  pickSupport(e) {
    this.closeToolPicker();
    const index = Number(e.detail.value);
    const value = index === 0 ? '' : this.data.supports[index];
    this.setData({ support: value, items: [] }); this.load(true);
  },
  more() { if (this.data.items.length < this.data.total) this.load(false); },
  open(e) { wx.navigateTo({ url: `/pages/detail/index?id=${e.currentTarget.dataset.id}` }); },
  // 没有内容时去搜索申报通知；选了省份、部门的带过去（取值和搜索筛选一致）。
  // 国家部委在搜索里是"层级"（UI 第二轮起不在地区里），服务端两处是同一个条件，结果相同
  goSearch() {
    const params = [`kind=${encodeURIComponent('申报通知')}`];
    const { province } = this.data;
    if (province === '国家部委') params.push(`levels=${encodeURIComponent(province)}`);
    else if (province) params.push(`regions=${encodeURIComponent(province)}`);
    if (this.data.dept) params.push(`depts=${encodeURIComponent(this.data.dept)}`);
    wx.navigateTo({ url: `/pages/search/index?${params.join('&')}` });
  },
  exportOne(e) { exportDocs([e.currentTarget.dataset.id], { label: '截止雷达' }); },
});
