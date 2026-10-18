const api = require('../../utils/api.js');
const { todayISO, cnDate } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { readCache, writeCache, cachedLabel } = require('../../utils/cache.js');

// 最近发布的名单和比对范围存一份到本机，下次打开先显示（utils/cache.js）
const LISTS_CACHE = 'youjuListsCacheV1';
const SCOPE_CACHE = 'youjuListScopeV1';

const CHIPS = ['全部', '科技型中小企业', '专精特新'];

// 转发（D-070）：查到了名单才在卡片标题和路径里带企业名称（名单上的都是企业和机构）；没查、正在查、
// 没查到、查询出错（如输入像个人姓名被服务端拒绝）时只转发这一页，不带输入的内容
const decode = (value) => { try { return decodeURIComponent(value || ''); } catch (e) { return String(value || ''); } };
// 从转发卡片打开时页面栈里只有这一页：左上角显示"首页"（点了回首页），不显示返回箭头
const firstPage = () => typeof getCurrentPages === 'function' && getCurrentPages().length <= 1;

Page({
  data: {
    // 列表行用的当天日期：onShow 时填上；先给空串，第一次渲染时不给组件传 null
    today: '',
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
    // 页面栈里只有这一页（从转发卡片打开）：左上角显示"首页"
    firstPage: false,
    name: '',
    rulesOpen: false, // 查询说明默认收起
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
    loading: true,
    shown: [],
    // 比对范围：按类型、省份、年度的名单份数和读出的企业条数（原名单看板的统计）
    scope: null,
    scopeError: false,
    // 最近发布的名单显示的是本机存的上次数据时写明；staleError：这次没更新成功
    cachedText: '',
    staleError: false,
    scopeOpen: false,
  },

  onHide() {
    if (api.stopStartup) api.stopStartup(this);
    stopDay(this);
  },

  onUnload() {
    if (api.stopStartup) api.stopStartup(this);
    stopDay(this);
  },

  onShow() {
    if (api.watchStartup) api.watchStartup(this);
    this.setData({ today: todayISO() });
    watchDay(this, (day) => this.setData({ today: day }));
  },

  onLoad(query) {
    if (api.watchStartup) api.watchStartup(this);
    const { statusBarHeight, navHeight, menuWidth } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight, menuWidth, firstPage: firstPage() });
    this.reload();
    // 从带企业名称的转发卡片打开：按这个名称查一次，和转发人看到的是同一个查询
    const name = decode(query && query.q).trim();
    if (name.replace(/\s+/g, '').length >= 2) {
      this.setData({ name });
      this.query();
    }
  },

  // 转发给好友和群（D-070）：不开朋友圈，不带任何奖励
  onShareAppMessage() {
    const { queried, result, looking, lookupError } = this.data;
    const found = !looking && !lookupError && result && result.items && result.items.length > 0;
    if (!found) return { title: '查企业上过哪些名单', path: '/pages/lists/index' };
    return { title: `查名单：${queried}`, path: `/pages/lists/index?q=${encodeURIComponent(queried)}` };
  },

  reload() {
    if (!this.listsShown) {
      const cached = readCache(LISTS_CACHE);
      if (cached) {
        this.listsShown = true;
        this.setData({ loading: false, weekTotal: cached.data.week_total, total: cached.data.total, items: cached.data.items, cachedText: cachedLabel(cached.at) });
        this.filterItems();
      }
    }
    this.setData(this.data.cachedText ? { error: '', staleError: false } : { loading: true, error: '' });
    this.loadScope();
    const seq = this.listSeq = (this.listSeq || 0) + 1;
    let live = false;
    let fresh = false;
    const showLists = (res, cachedText) => {
      this.listsShown = true;
      this.setData({ loading: false, weekTotal: res.week_total, total: res.total, items: res.items, cachedText, staleError: false });
      this.filterItems();
      writeCache(LISTS_CACHE, { week_total: res.week_total, total: res.total, items: res.items });
    };
    // 另向常驻的 admin 要首屏快照（不等接口服务冷启动）；实时的回来了就以实时的为准
    if (api.snapshotOnce) {
      api.snapshotOnce().then((snap) => {
        if (!snap || !snap.lists || live || seq !== this.listSeq) return;
        fresh = true;
        showLists(snap.lists, '');
        if (snap.scope) this.applyScope(snap.scope);
      });
    }
    api.lists()
      .then((res) => {
        live = true;
        showLists(res, '');
      })
      .catch((err) => {
        // 显示着快照就不报错；显示着本机存的上次名单就留着，只写明这次没更新
        if (fresh) return;
        if (this.data.cachedText) this.setData({ staleError: true });
        else this.setData({ loading: false, error: `加载失败：${err.message}` });
      });
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

  toggleRules() {
    this.setData({ rulesOpen: !this.data.rulesOpen });
  },

  // 点名称相近的企业：换成这个名称再查一次
  pickSimilar(e) {
    this.setData({ name: e.currentTarget.dataset.name });
    this.query();
  },

  moreLists() {
    wx.navigateTo({ url: '/pages/search/index?kind=名单&time=不限' });
  },

  // 比对范围加载失败不影响查名单；手里没有上次的范围时写明没取到，可以重试
  loadScope() {
    if (!this.data.scope) {
      const cached = readCache(SCOPE_CACHE);
      if (cached) this.setData({ scope: cached.data });
    }
    this.setData({ scopeError: false });
    // 连点重试时只认最后一次请求的结果，先发的晚回来也不覆盖
    const seq = (this.scopeSeq || 0) + 1;
    this.scopeSeq = seq;
    api.listBoard({ days: 30, page: 1, size: 1 })
      .then((res) => { if (seq === this.scopeSeq) this.applyScope(res.stats || {}); })
      .catch(() => { if (seq === this.scopeSeq && !this.data.scope) this.setData({ scopeError: true }); });
  },

  // 名单看板的统计 → 比对范围（按类型合计份数）
  applyScope(stats) {
    const byType = stats.by_type || [];
    const scope = byType.length ? {
      lists: byType.reduce((sum, item) => sum + (item.lists || 0), 0),
      byType, byProvince: stats.by_province || [], byYear: stats.by_year || [],
    } : null;
    this.setData({ scope });
    if (scope) writeCache(SCOPE_CACHE, scope);
  },

  toggleScope() {
    this.setData({ scopeOpen: !this.data.scopeOpen });
  },

  pickChip(e) {
    this.setData({ chip: e.currentTarget.dataset.chip });
    this.filterItems();
  },

  filterItems() {
    const { chip, items } = this.data;
    this.setData({ shown: chip === '全部' ? items : items.filter((item) => (item.title || '').includes(chip)) });
  },
});
