const api = require('../../utils/api.js');
const { todayISO, clockText, dueView, monthDay, weekday, reviewStaleNote } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { readCache, writeCache, cachedLabel } = require('../../utils/cache.js');
const { loginOpen } = require('../../utils/consent.js');
const boost = require('../../utils/boost.js');

const SCOPES = [
  { key: 'apply', label: '查申报', placeholder: '搜申报通知，如"高新技术企业"' },
  { key: 'list', label: '查名单', placeholder: '输入企业全称或信用代码查名单' },
  { key: 'policy', label: '查政策', placeholder: '搜政策文件，如"数字经济"' },
];

// 首页先显示上次存在本机的数据（见 utils/cache.js）；取新的失败就留着上次的，写明"数据未更新"
const HOME_CACHE = 'youjuHomeCacheV1';

// 新手引导（D-070 ④）："三步上手"卡片。点"不再显示"后在本机记一个标记，以后不再出现；没关之前每次打开都显示。
// 不判断是不是老用户；没有奖励、打勾和红点。第三步去工具中心（批量名单比对、客户相关通知表，要先同意协议）；
// 协议没定稿的版本没有工具中心入口，改去不用登录的"我的客户"
const GUIDE_KEY = 'youjuGuideClosedV1';
const GUIDE_FIRST = [
  // 10/18 验收 #21c：企业行政常把公司名输进"查申报"，这里写明查企业上过哪些名单要先点"查名单"
  { key: 'search', name: '搜通知', text: '按关键词、地区、类型找申报通知和政策；查企业上过哪些名单，先点搜索框上面的“查名单”' },
  { key: 'due', name: '看截止和原文', text: '“截止”页列出快到期的通知；通知详情里可以复制政府网站的原文链接，以原文为准', url: '/pages/report/index' },
];
const GUIDE_TOOLS = { key: 'tools', name: '批量查客户（顾问用）', text: '在“工具中心”一次粘贴多家企业，查上过哪些名单、整理相关通知；需先同意协议，每月有免费次数', url: '/pages/advisor/index' };
const GUIDE_CLIENTS = { key: 'clients', name: '批量查客户（顾问用）', text: '在“我的客户”存下客户的地区和产业方向，一次列出和它们相关的通知', url: '/pages/clients/index' };
const guideSteps = (open) => GUIDE_FIRST.concat(open ? GUIDE_TOOLS : GUIDE_CLIENTS);

// 本机读不到标记（读取出错）时照常显示，宁可多显示一次
function guideClosed() {
  try { return Boolean(wx.getStorageSync(GUIDE_KEY)); } catch (e) { return false; }
}

// "7 天内截止"：接口只给核对过的（最多 5 条）；按手机日期重算剩余天数，已过的不放
// 后端没给这个字段（老版本或本机演示数据）时不写，免得误报"今日未更新"
function reviewNoteOf(stats) {
  return stats && Object.prototype.hasOwnProperty.call(stats, 'deadline_reviewed_at') ? reviewStaleNote(stats.deadline_reviewed_at) : '';
}

function soonRows(list) {
  return (list || []).map((d) => {
    const view = dueView({ kind: d.label, source: d.source || 'verified', date: d.date, time: d.time, quote: d.quote });
    if (!view || view.source !== 'verified' || view.days < 0) return null;
    return {
      id: String(d.id), title: d.title, kindText: d.label || '申报截止', days: view.days, tone: view.tone || 'normal',
      // 能不能解读（D-075，接口每行带）：行上的解读按钮据此显示
      ai: d.ai || null,
      dateText: monthDay(d.date), week: `${weekday(d.date)}${d.time ? ` ${d.time}` : ''}`,
    };
  }).filter(Boolean).slice(0, 5);
}

Page({
  data: {
    // 列表行用的当天日期：onShow 时填上；先给空串，第一次渲染时不给组件传 null
    today: '',
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
    reviewNote: '',
    loaded: false,
    error: '',
    // 协议定稿前（D-056）没有关注，不显示"你关注的条件本周有 N 份新文件"（演示数据里有这个数）
    loginOpen: loginOpen(),
    // 新手引导"三步上手"：没点过"不再显示"时显示
    guide: false,
    guideSteps: [],
    // 申报季加量（D-070 ⑤）提前公布的起止日期，和批量工具页同一句；没有这项活动或取不到时为空，不显示
    seasonText: '',
  },

  onLoad() {
    if (api.watchStartup) api.watchStartup(this);
    const { statusBarHeight, navHeight, menuWidth } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight, menuWidth, guide: !guideClosed(), guideSteps: guideSteps(this.data.loginOpen) });
    this.lastShow = Date.now();
    this.reload();
  },

  // "三步上手"的一步：搜通知按当前的搜索范围去搜索页，另两步去对应的页面
  goGuide(e) {
    const step = this.data.guideSteps.find((s) => s.key === e.currentTarget.dataset.key);
    if (!step) return;
    if (step.url) wx.navigateTo({ url: step.url });
    else this.goSearch();
  },

  // "不再显示"：这次先关掉；本机存不下标记时下次打开可能再出现，不报错
  closeGuide() {
    this.setData({ guide: false });
    try { wx.setStorageSync(GUIDE_KEY, 1); } catch (e) { /* 存不下就算了 */ }
  },

  // 取数据：先显示本机存的上次数据；同时向常驻的 admin 要首屏快照（不等接口服务冷启动），
  // 再取接口服务的实时数据。实时的回来了就以它为准；快照只在实时的还没回来时显示。
  reload() {
    // 申报季一行自己取、自己共用（见 loadSeason）：首页数据还在取（如冷启动）时回到首页也照常刷新它
    this.loadSeason();
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
        // 实时请求可能已经先失败、写了"数据未更新"：快照到了就清掉（10/18 验收，Codex 指出）
        this.setData({ error: '' });
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
        else this.setData({ stats: null, items: [], soon: [], soonUnverified: 0, reviewNote: '', error: `数据未更新：${err.message}` });
      })
      .then(() => { this.refreshing = false; this.setData({ loading: false }); });
  },

  // 申报季加量一行：和首页数据分开取，首页不等它；取不到、批量工具没开就不显示，不报错。
  // 协议没定稿的版本没有工具中心入口（批量工具要先同意协议），不取也不显示。refresh：停在首页跨过 0 点时重新取
  // （"将于…开始"变成进行中，过了结束日不再显示）
  loadSeason(refresh) {
    if (!this.data.loginOpen || !api.advisorSeason) return;
    const seq = this.seasonSeq = (this.seasonSeq || 0) + 1;
    const show = (season) => { if (seq === this.seasonSeq) this.setData({ seasonText: boost.seasonLine(season) }); };
    api.advisorSeason({ refresh: Boolean(refresh) }).then(show, () => show(null));
  },

  show({ stats, items, dueSoon, dueSoonUnverifiedTotal }, cachedText) {
    this.dueSoon = dueSoon || [];
    this.setData({
      // fresh：显示的是快照或实时数据（不是本机存的上次数据），这时不再提示"正在连接"
      stats, items: items || [], loaded: true, cachedText, fresh: !cachedText, checkedText: clockText(stats && stats.last_checked_at),
      soon: soonRows(this.dueSoon), soonUnverified: dueSoonUnverifiedTotal || 0,
      reviewNote: reviewNoteOf(stats),
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
    watchDay(this, (day) => {
      this.setData({ today: day, soon: soonRows(this.dueSoon), reviewNote: reviewNoteOf(this.data.stats) });
      this.loadSeason(true);
    });
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
