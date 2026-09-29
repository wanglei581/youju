const api = require('../../utils/api.js');
const { todayISO } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { exportDocs, MAX } = require('../../utils/exporter.js');
const { ensureConsent } = require('../../utils/consent.js');
const { requestOnce } = require('../../utils/subscribe.js');
const { loadCoverage, notCoveredNames, allUncovered } = require('../../utils/coverage.js');

const KIND_TABS = [
  { key: 'all', label: '全部' },
  { key: '申报通知', label: '申报通知' },
  { key: '名单', label: '名单与公示' },
  { key: '政策', label: '政策' },
];

// 筛选项：地区"不限地区"与具体省份互斥；发布时间单选（取值与服务端 /api/v1/search 一致）
const GROUPS = [
  { key: 'region', title: '地区', hint: '可多选', multi: true,
    options: ['不限地区', '国家部委', '北京', '天津', '河北', '上海', '江苏', '安徽', '山东', '广东', '四川', '更多省份'],
    note: '' }, // 还没收录的省份取自收录范围接口
  { key: 'type', title: '类型', hint: '可多选', multi: true, options: ['申报通知', '名单与公示', '政策文件', '其他'] },
  { key: 'status', title: '名单状态', hint: '可多选', multi: true, options: ['公示', '公布'] },
  { key: 'time', title: '发布时间', hint: '单选', multi: false, options: ['近 7 天', '近 30 天', '今年', '不限'] },
  { key: 'dept', title: '发文部门', hint: '可多选', multi: true, options: ['科技部门', '工信部门', '其他部门'] },
];
const ALL_PROVINCES = ['北京', '天津', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江', '上海', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北', '湖南', '广东', '广西', '海南', '重庆', '四川', '贵州', '云南', '西藏', '陕西', '甘肃', '青海', '宁夏', '新疆'];
const DEFAULT_PICKED = { region: ['不限地区'], type: [], status: [], time: ['近 30 天'], dept: [] };
const PAGE = 20;

Page({
  data: {
    q: '',
    tabs: KIND_TABS,
    tab: 'all',
    items: [],
    total: 0,
    totalAll: 0,
    loading: false,
    error: '',
    filterOpen: false,
    groups: GROUPS,
    picked: DEFAULT_PICKED,
    pickedMap: {},
    chipRegion: '地区',
    chipTime: '近 30 天',
    regionOn: false,
    statusOn: false,
    // 选择模式：勾选文件后导出 Excel / Word
    selecting: false,
    chosen: {},
    chosenCount: 0,
    // 没有结果时：选中的地区全部还没收录，如"浙江（正在接入）"；否则附一行"暂未收录"的说明
    uncovered: '',
    notCoveredNote: '',
  },

  onLoad(query) {
    const kind = decodeURIComponent(query.kind || '');
    const time = decodeURIComponent(query.time || '');
    if (['近 7 天', '近 30 天', '今年', '不限'].includes(time)) {
      this.setData({ picked: Object.assign({}, DEFAULT_PICKED, { time: [time] }) });
    }
    this.setData({
      q: decodeURIComponent(query.q || ''),
      tab: ['申报通知', '名单', '政策'].includes(kind) ? kind : 'all',
    });
    this.refreshChips();
    this.apply(true);
    // 地区筛选下的说明：还没收录的省份
    loadCoverage().then((c) => {
      const names = notCoveredNames(c);
      if (!names) return;
      const note = `${names}暂未收录`;
      this.setData({ groups: this.data.groups.map((g) => (g.key === 'region' ? Object.assign({}, g, { note }) : g)) });
    });
  },

  onHide() {
    stopDay(this);
  },

  onUnload() {
    stopDay(this);
  },

  onShow() {
    this.setData({ today: todayISO() });
    // 页面在后台跨过零点后再显示，时间筛选要按今天重新查
    if (this.loaded) this.apply(true);
    watchDay(this, (day) => {
      this.setData({ today: day });
      this.apply(true);
    });
  },

  // 按关键词、页签、筛选条件查询；reset 为 false 时是"加载更多"
  // 当前的搜索条件（与服务端 /api/v1/search 的参数、关注条件一致）
  conditions() {
    const { q, tab, picked } = this.data;
    return {
      q: q.trim(),
      kind: tab,
      regions: picked.region.join(','),
      types: picked.type.join(','),
      status: picked.status.join(','),
      time: picked.time[0],
      depts: picked.dept.join(','),
    };
  },

  // 网络不好出错时，点"重试"按当前条件重新搜索
  retry() {
    this.apply(true);
  },

  apply(reset) {
    const { items } = this.data;
    const params = Object.assign(this.conditions(), { limit: PAGE, offset: reset ? 0 : items.length });
    const seq = (this.seq || 0) + 1;
    this.seq = seq;
    // 换了关键词或筛选条件，之前勾选的不再对应当前结果，清空
    this.setData(reset ? { loading: true, error: '', chosen: {}, chosenCount: 0, uncovered: '', notCoveredNote: '' } : { loading: true, error: '' });
    api.search(params)
      .then((res) => {
        if (seq !== this.seq) return; // 只用最后一次查询的结果
        this.loaded = true;
        this.setData({
          items: reset ? res.items : items.concat(res.items),
          total: res.total,
          totalAll: res.total_all,
          loading: false,
        });
        // 没有结果时区分"该地区还没收录"和"没有匹配的文件"（验收 A8）：只有选中的地区全部
        // 还没收录才说"还没收录"，不从关键词里推断地名
        if (!res.total) {
          const regions = params.regions.split(',');
          loadCoverage().then((c) => {
            if (seq !== this.seq) return;
            const hit = allUncovered(c, regions);
            this.setData({
              uncovered: hit.map((r) => `${r.name}（${r.reason}）`).join('、'),
              notCoveredNote: notCoveredNames(c),
            });
          });
        }
      })
      .catch((err) => {
        if (seq !== this.seq) return;
        // 重新查询失败时不保留旧结果，避免把旧条件的结果当成新结果
        this.setData(reset ? { loading: false, error: err.message, items: [], total: 0, totalAll: 0 } : { loading: false, error: err.message });
      });
  },

  refreshChips() {
    const { picked } = this.data;
    const pickedMap = {};
    Object.keys(picked).forEach((g) => picked[g].forEach((o) => { pickedMap[`${g}:${o}`] = true; }));
    const regions = picked.region.filter((r) => r !== '不限地区');
    this.setData({
      pickedMap,
      chipRegion: regions.length ? regions.join('、') : '地区',
      regionOn: regions.length > 0,
      chipTime: picked.time[0],
      statusOn: picked.status.length > 0,
    });
  },

  onInput(e) {
    this.setData({ q: e.detail.value, loading: true });
    this.seq = (this.seq || 0) + 1; // 作废进行中的查询，防止旧结果回写
    clearTimeout(this.inputTimer);
    this.inputTimer = setTimeout(() => this.apply(true), 300);
  },

  clearQ() {
    this.setData({ q: '' });
    this.apply(true);
  },

  cancel() {
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  pickTab(e) {
    this.setData({ tab: e.currentTarget.dataset.key });
    this.apply(true);
  },

  loadMore() {
    if (!this.data.loading && this.data.items.length < this.data.total) this.apply(false);
  },

  onReachBottom() {
    this.loadMore();
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
    // "更多省份"只是展开全部省份，不是筛选条件
    if (group === 'region' && option === '更多省份') {
      const groups = this.data.groups.map((g) => (g.key === 'region'
        ? Object.assign({}, g, { options: ['不限地区', '国家部委'].concat(ALL_PROVINCES) }) : g));
      this.setData({ groups });
      return;
    }
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
    this.refreshChips();
    this.apply(true);
  },

  resetFilter() {
    this.setData({ picked: DEFAULT_PICKED });
    this.refreshChips();
    this.apply(true);
  },

  // 默认的关注名称，如"\u201c高新技术企业\u201d · 山东 · 申报通知"
  followName() {
    const c = this.conditions();
    const tab = KIND_TABS.find((t) => t.key === c.kind);
    const regions = c.regions.split(',').filter((r) => r && r !== '不限地区');
    const parts = [];
    if (c.q) parts.push(`\u201c${c.q}\u201d`);
    parts.push(regions.length ? regions.slice(0, 3).join('、') : '不限地区');
    parts.push(tab ? tab.label : '全部');
    return parts.join(' · ').slice(0, 40);
  },

  // 关注这个搜索：保存当前条件，有新文件时在"关注"里标出；关注后可以订阅一次微信提醒
  follow() {
    ensureConsent().then((ok) => {
      if (!ok) return;
      const conditions = this.conditions();
      if (!conditions.q) delete conditions.q;
      wx.showModal({
        title: '关注这个搜索',
        editable: true,
        placeholderText: '给这个关注起个名字',
        content: this.followName(),
        confirmText: '关注',
        success: (res) => {
          if (!res.confirm) return;
          const name = (res.content || '').trim().slice(0, 40) || this.followName();
          api.createFollow(name, conditions)
            .then((created) => wx.showModal({
              title: '已关注',
              content: '有新文件时会在"关注"里标出。要不要再订阅一次微信提醒？',
              confirmText: '订阅提醒',
              cancelText: '不用了',
              success: (r) => {
                if (!r.confirm) return;
                requestOnce('newDocs', { followId: created.id }).then((subscribed) => {
                  if (subscribed) wx.showToast({ title: '已订阅 1 次微信提醒', icon: 'none' });
                });
              },
            }))
            .catch((err) => wx.showToast({ title: err.message || '关注失败，请稍后再试', icon: 'none' }));
        },
      });
    });
  },

  // 进入或退出选择模式；退出时清空勾选
  select() {
    const selecting = !this.data.selecting;
    this.setData({ selecting, chosen: {}, chosenCount: 0 });
  },

  togglePick(e) {
    if (!this.data.selecting) return;
    const id = e.currentTarget.dataset.id;
    const chosen = Object.assign({}, this.data.chosen);
    if (chosen[id]) {
      delete chosen[id];
    } else {
      if (Object.keys(chosen).length >= MAX) {
        wx.showToast({ title: `一次最多导出 ${MAX} 份`, icon: 'none' });
        return;
      }
      chosen[id] = true;
    }
    this.setData({ chosen, chosenCount: Object.keys(chosen).length });
  },

  // 全选已加载的（最多 200 份）
  pickAll() {
    const chosen = {};
    this.data.items.slice(0, MAX).forEach((item) => { chosen[item.id] = true; });
    this.setData({ chosen, chosenCount: Object.keys(chosen).length });
  },

  exportChosen() {
    // 按列表里的顺序导出
    const ids = this.data.items.filter((item) => this.data.chosen[item.id]).map((item) => item.id);
    exportDocs(ids, { label: this.followName() });
  },

  feedback() {
    wx.switchTab({ url: '/pages/me/index' });
  },
});
