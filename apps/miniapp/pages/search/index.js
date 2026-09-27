const api = require('../../utils/api.js');
const { todayISO, daysUntil } = require('../../utils/format.js');

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
const TYPE_KIND = { 申报通知: '申报通知', 名单与公示: '名单', 政策文件: '政策' };
const TIME_DAYS = { '近 7 天': 7, '近 30 天': 30, 今年: 'year', 不限: 0 };

function matchRegion(doc, regions) {
  if (!regions.length || regions.includes('不限地区')) return true;
  return regions.some((r) => (r === '国家部委' ? doc.region.startsWith('国家') : doc.region.startsWith(r)));
}

function matchType(doc, types) {
  if (!types.length) return true;
  return types.some((t) => (t === '其他' ? !['申报通知', '名单', '政策'].includes(doc.kind) : doc.kind === TYPE_KIND[t]));
}

function deptOf(doc) {
  const s = doc.issuer || '';
  if (/科学技术|科技/.test(s)) return '科技部门';
  if (/工业和信息化|经济和信息化|工信|经信/.test(s)) return '工信部门';
  return '其他部门';
}

function matchTime(doc, time) {
  const v = TIME_DAYS[time];
  if (!v) return true;
  if (v === 'year') return doc.date.slice(0, 4) === todayISO().slice(0, 4);
  return -daysUntil(doc.date) <= v;
}

Page({
  data: {
    q: '',
    tabs: KIND_TABS,
    tab: 'all',
    items: [],
    shown: [],
    total: 0,
    totalAll: 0,
    filterOpen: false,
    groups: GROUPS,
    picked: DEFAULT_PICKED,
    pickedMap: {},
    chipRegion: '地区',
    chipTime: '近 30 天',
    regionOn: false,
    statusOn: false,
  },

  onLoad(query) {
    const q = decodeURIComponent(query.q || '');
    const kind = decodeURIComponent(query.kind || '');
    api.search().then((res) => {
      this.setData({ q, items: res.items, tab: kind === '申报通知' || kind === '政策' ? kind : 'all' });
      this.apply();
    });
  },

  // 按关键词、筛选条件和页签计算结果和份数（接口就绪后改由服务端计算）
  apply() {
    const { items, q, picked, tab } = this.data;
    const kw = q.trim();
    const base = items.filter((d) => (!kw || d.title.includes(kw))
      && matchRegion(d, picked.region) && matchType(d, picked.type)
      && (!picked.status.length || picked.status.includes(d.status))
      && (!picked.dept.length || picked.dept.includes(deptOf(d))));
    const timed = base.filter((d) => matchTime(d, picked.time[0]));
    const shown = tab === 'all' ? timed : timed.filter((d) => d.kind === tab);
    const pickedMap = {};
    Object.keys(picked).forEach((g) => picked[g].forEach((o) => { pickedMap[`${g}:${o}`] = true; }));
    const regions = picked.region.filter((r) => r !== '不限地区');
    this.setData({
      shown,
      total: timed.length,
      totalAll: base.length,
      pickedMap,
      chipRegion: regions.length ? regions.join('、') : '地区',
      regionOn: regions.length > 0,
      chipTime: picked.time[0],
      statusOn: picked.status.length > 0,
    });
  },

  onInput(e) {
    this.setData({ q: e.detail.value });
    this.apply();
  },

  clearQ() {
    this.setData({ q: '' });
    this.apply();
  },

  cancel() {
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  pickTab(e) {
    this.setData({ tab: e.currentTarget.dataset.key });
    this.apply();
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
    this.setData({ picked: Object.assign({}, this.data.picked, { [group]: next }) });
    this.apply();
  },

  resetFilter() {
    this.setData({ picked: DEFAULT_PICKED });
    this.apply();
  },

  follow() {
    wx.showToast({ title: '关注功能随登录一起开放', icon: 'none' });
  },

  select() {
    wx.showToast({ title: '勾选文件后可导出 Excel/Word', icon: 'none' });
  },

  feedback() {
    wx.switchTab({ url: '/pages/me/index' });
  },
});
