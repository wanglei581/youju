const api = require('../../utils/api.js');

const KIND_TABS = [
  { key: 'all', label: '全部' },
  { key: '申报通知', label: '申报通知' },
  { key: '名单', label: '名单与公示' },
  { key: '政策', label: '政策' },
];

// 筛选项：地区"不限地区"与具体省份互斥；发布时间单选
const GROUPS = [
  { key: 'region', title: '地区', hint: '可多选', multi: true,
    options: ['不限地区', '国家部委', '北京', '天津', '河北', '上海', '江苏', '安徽', '山东', '广东', '四川', '更多省份'],
    note: '广西、甘肃暂未覆盖；浙江正在接入' },
  { key: 'type', title: '类型', hint: '可多选', multi: true, options: ['申报通知', '名单与公示', '政策文件', '其他'] },
  { key: 'status', title: '名单状态', hint: '可多选', multi: true, options: ['公示', '正式公布'] },
  { key: 'time', title: '发布时间', hint: '单选', multi: false, options: ['近 7 天', '近 30 天', '今年', '不限'] },
  { key: 'dept', title: '发文部门', hint: '可多选', multi: true, options: ['科技部门', '工信部门', '其他部门'] },
];
const DEFAULT_PICKED = { region: ['不限地区'], type: [], status: [], time: ['近 30 天'], dept: [] };

Page({
  data: {
    q: '',
    tabs: KIND_TABS,
    tab: 'all',
    total30: 0,
    totalAll: 0,
    items: [],
    shown: [],
    filterOpen: false,
    groups: GROUPS,
    picked: DEFAULT_PICKED,
    pickedMap: {},
  },

  onLoad(query) {
    const q = decodeURIComponent(query.q || '');
    const kind = decodeURIComponent(query.kind || '');
    api.search().then((res) => {
      const items = res.items;
      this.setData({ q: q || res.q, total30: res.total_30d, totalAll: res.total_all, items, shown: items });
      if (kind) this.applyTab(kind === '申报通知' || kind === '政策' ? kind : 'all');
    });
    this.refreshPickedMap(DEFAULT_PICKED);
  },

  onInput(e) {
    this.setData({ q: e.detail.value });
  },

  clearQ() {
    this.setData({ q: '' });
  },

  cancel() {
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  pickTab(e) {
    this.applyTab(e.currentTarget.dataset.key);
  },

  applyTab(tab) {
    const shown = tab === 'all' ? this.data.items : this.data.items.filter((d) => d.kind === tab);
    this.setData({ tab, shown });
  },

  openFilter() {
    this.setData({ filterOpen: true });
  },

  onFilterVisible(e) {
    this.setData({ filterOpen: e.detail.visible });
  },

  closeFilter() {
    this.setData({ filterOpen: false });
  },

  pickOption(e) {
    const { group, option } = e.currentTarget.dataset;
    const def = GROUPS.find((g) => g.key === group);
    const cur = this.data.picked[group] || [];
    let next;
    if (!def.multi) {
      next = [option];
    } else if (group === 'region' && option === '不限地区') {
      next = ['不限地区'];
    } else {
      next = cur.includes(option) ? cur.filter((o) => o !== option) : cur.filter((o) => o !== '不限地区').concat(option);
      if (group === 'region' && next.length === 0) next = ['不限地区'];
    }
    const picked = Object.assign({}, this.data.picked, { [group]: next });
    this.setData({ picked });
    this.refreshPickedMap(picked);
  },

  resetFilter() {
    this.setData({ picked: DEFAULT_PICKED });
    this.refreshPickedMap(DEFAULT_PICKED);
  },

  refreshPickedMap(picked) {
    const pickedMap = {};
    Object.keys(picked).forEach((g) => picked[g].forEach((o) => { pickedMap[`${g}:${o}`] = true; }));
    this.setData({ pickedMap });
  },

  follow() {
    wx.showToast({ title: '已关注这个搜索', icon: 'success' });
  },

  select() {
    wx.showToast({ title: '勾选文件后可导出 Excel/Word', icon: 'none' });
  },

  feedback() {
    wx.switchTab({ url: '/pages/me/index' });
  },
});
