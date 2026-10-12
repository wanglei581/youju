const api = require('../../utils/api.js');
const { todayISO, clockText, dueView, monthDay, weekday } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { readCache, writeCache, cachedLabel } = require('../../utils/cache.js');
const { loginOpen } = require('../../utils/consent.js');

const SCOPES = [
  { key: 'apply', label: '查申报', placeholder: '搜申报通知，如"高新技术企业"' },
  { key: 'list', label: '查名单', placeholder: '输入企业全称或信用代码查名单' },
  { key: 'policy', label: '查政策', placeholder: '搜政策文件，如"数字经济"' },
];

// 首页先显示上次存在本机的数据（见 utils/cache.js）；取新的失败就留着上次的，写明"数据未更新"
const HOME_CACHE = 'youjuHomeCacheV1';

// "7 天内截止"：接口只给核对过的（最多 5 条）；按手机日期重算剩余天数，已过的不放
function soonRows(list) {
  return (list || []).map((d) => {
    const view = dueView({ kind: d.label, source: d.source || 'verified', date: d.date, time: d.time, quote: d.quote });
    if (!view || view.source !== 'verified' || view.days < 0) return null;
    return {
      id: String(d.id), title: d.title, kindText: d.label || '申报截止', days: view.days, tone: view.tone || 'normal',
      dateText: monthDay(d.date), week: `${weekday(d.date)}${d.time ? ` ${d.time}` : ''}`,
    };
  }).filter(Boolean).slice(0, 5);
}

Page({
  data: {
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
    scopes: SCOPES,
    scope: 0,
    hot: ['高新技术企业', '专精特新', '科技型中小企业'],
    stats: null,
    // 最近一次成功检查的时间（北京时间，如"10月6日 06:30"）；本周 0 份时写在"本周暂无新文件"后面
    checkedText: '',
    items: [],
    // 显示的是上次存在本机的数据时，写明是什么时候的（取到新数据后清空）
    cachedText: '',
    // 7 天内截止（只放核对过的）和 7 天内有候选、还没核对的份数
    soon: [],
    soonUnverified: 0,
    loaded: false,
    error: '',
    // 协议定稿前（D-056）没有关注，不显示"你关注的条件本周有 N 份新文件"（演示数据里有这个数）
    loginOpen: loginOpen(),
  },

  onLoad() {
    if (api.watchStartup) api.watchStartup(this);
    const { statusBarHeight, navHeight, menuWidth } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight, menuWidth });
    this.lastShow = Date.now();
    this.reload();
  },

  // 取数据：先显示本机存的上次数据；同时向常驻的 admin 要首屏快照（不等接口服务冷启动），
  // 再取接口服务的实时数据。实时的回来了就以它为准；快照只在实时的还没回来时显示。
  reload() {
    if (this.refreshing) return;
    this.refreshing = true;
    const seq = this.homeSeq = (this.homeSeq || 0) + 1;
    let live = false;
    let fresh = false;
    if (!this.data.loaded) {
      const cached = readCache(HOME_CACHE);
      if (cached) this.show(cached.data, cachedLabel(cached.at));
    }
    this.setData({ loading: true, error: '' });
    const save = (data) => writeCache(HOME_CACHE, {
      stats: data.stats, items: data.items, dueSoon: data.dueSoon || [], dueSoonUnverifiedTotal: data.dueSoonUnverifiedTotal || 0,
    });
    if (api.snapshotOnce) {
      api.snapshotOnce().then((snap) => {
        if (!snap || live || seq !== this.homeSeq) return;
        fresh = true;
        this.show(snap.home, '');
        save(snap.home);
      });
    }
    api.home()
      .then((data) => {
        live = true;
        this.show(data, '');
        save(data);
      })
      .catch((err) => {
        // 已经显示着快照（一分钟内的数据）就不报错；显示着本机存的上次数据就留着，只在上面写明没更新
        if (fresh) return;
        if (this.data.loaded) this.setData({ error: `数据未更新：${err.message}` });
        else this.setData({ stats: null, items: [], soon: [], soonUnverified: 0, error: `数据未更新：${err.message}` });
      })
      .then(() => { this.refreshing = false; this.setData({ loading: false }); });
  },

  show({ stats, items, dueSoon, dueSoonUnverifiedTotal }, cachedText) {
    this.dueSoon = dueSoon || [];
    this.setData({
      // fresh：显示的是快照或实时数据（不是本机存的上次数据），这时不再提示"正在连接"
      stats, items: items || [], loaded: true, cachedText, fresh: !cachedText, checkedText: clockText(stats && stats.last_checked_at),
      soon: soonRows(this.dueSoon), soonUnverified: dueSoonUnverifiedTotal || 0,
    });
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
    const stamp = Date.now();
    if (this.lastShow === undefined || stamp - this.lastShow >= 1000) {
      this.reload();
      this.lastShow = stamp;
    }
    this.setData({ today: todayISO() });
    watchDay(this, (day) => this.setData({ today: day, soon: soonRows(this.dueSoon) }));
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

  goSearch(e) {
    const q = (e && e.currentTarget.dataset.q) || '';
    const kind = { apply: '申报通知', policy: '政策' }[SCOPES[this.data.scope].key] || 'all';
    wx.navigateTo({ url: `/pages/search/index?q=${encodeURIComponent(q)}&kind=${encodeURIComponent(kind)}` });
  },

  // 卡片底部：到搜索页看近 7 天的申报通知（搜索没有"本周"这个时间段，所以不写本周的份数）
  goMore() {
    wx.navigateTo({ url: `/pages/search/index?time=${encodeURIComponent('近 7 天')}&kind=${encodeURIComponent('申报通知')}` });
  },

  goEntry(e) {
    wx.navigateTo({ url: e.currentTarget.dataset.url });
  },

  openDoc(e) {
    wx.navigateTo({ url: `/pages/detail/index?id=${e.currentTarget.dataset.id}` });
  },

  goFollow() {
    wx.switchTab({ url: '/pages/follow/index' });
  },

  // 覆盖说明：已收录的地区、栏目、最近更新时间和还没收录的省份都在"关于有据"
  goCoverage() {
    wx.navigateTo({ url: '/pages/about/index' });
  },
});
