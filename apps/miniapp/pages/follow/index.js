const api = require('../../utils/api.js');
const legal = require('../../content/legal.js');
const { todayISO } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { requestOnce } = require('../../utils/subscribe.js');
const { ensureConsent, agreedHere } = require('../../utils/consent.js');

const KIND_LABEL = { all: '全部类型', 申报通知: '申报通知', 名单: '名单与公示', 政策: '政策' };

// 关注条件的一句话说明，如"山东、北京 · 申报通知 · 近 30 天"
function describe(c) {
  const parts = [];
  if (c.q) parts.push(`"${c.q}"`);
  const regions = (c.regions || '').split(',').filter((r) => r && r !== '不限地区');
  parts.push(regions.length ? regions.join('、') : '全国');
  parts.push(KIND_LABEL[c.kind] || '全部类型');
  if (c.time) parts.push(c.time);
  return parts.join(' · ');
}

// 三种状态：demo 没连接口时的演示数据；gated 未登录（或协议未定稿）；live 真实的关注
Page({
  data: { mode: 'demo', weekStart: '', newTotal: 0, follows: [], changes: [], managing: false, loading: false, error: '', legalFinal: legal.final },

  onHide() {
    stopDay(this);
  },

  onUnload() {
    stopDay(this);
  },

  onShow() {
    this.setData({ today: todayISO() });
    watchDay(this, (day) => this.setData({ today: day }));
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 1 });
    this.load();
  },

  load() {
    if (!api.isLive()) {
      api.follow().then((res) => this.setData({
        mode: 'demo',
        weekStart: res.week_start,
        newTotal: res.new_total,
        follows: res.subs.map((s, i) => ({ id: `demo${i}`, name: s.name, desc: s.desc, newCount: s.n, tone: s.tone, icon: s.icon })),
        changes: res.changes,
      }));
      return;
    }
    if (!legal.final || !agreedHere()) {
      this.setData({ mode: 'gated', follows: [], changes: [], newTotal: 0 });
      return;
    }
    this.setData({ mode: 'live', loading: true, error: '' });
    api.myFollows()
      .then((res) => {
        const follows = res.items.map((f) => ({
          id: f.id,
          name: f.name,
          desc: describe(f.conditions),
          newCount: f.newCount,
          tone: f.conditions.kind === '名单' ? 'orange' : 'blue',
          icon: f.conditions.kind === '名单' ? 'bulletpoint' : 'task-checked',
        }));
        this.setData({ follows, newTotal: follows.reduce((n, f) => n + f.newCount, 0), loading: false });
        return this.loadChanges(follows);
      })
      .catch((err) => this.setData({ loading: false, error: `加载失败：${err.message}` }));
  },

  // 新文件：取有新文件的前 3 个关注，每个最多 5 份，合并去重后按发布日期排
  loadChanges(follows) {
    const withNew = follows.filter((f) => f.newCount > 0).slice(0, 3);
    return Promise.all(withNew.map((f) => api.followNew(f.id, { limit: 5 })
      .then((r) => r.items.map((d) => Object.assign({ label: f.name }, d)))
      .catch(() => [])))
      .then((lists) => {
        const seen = {};
        const changes = [];
        [].concat(...lists)
          .sort((a, b) => (b.date || '').localeCompare(a.date || ''))
          .forEach((d) => { if (!seen[d.id]) { seen[d.id] = true; changes.push(d); } });
        this.setData({ changes: changes.slice(0, 10) });
      });
  },

  enable() {
    ensureConsent().then((ok) => { if (ok) this.load(); });
  },

  openFollow(e) {
    if (this.data.managing) return;
    if (this.data.mode !== 'live') {
      wx.navigateTo({ url: '/pages/search/index' });
      return;
    }
    const { id, name } = e.currentTarget.dataset;
    wx.navigateTo({ url: `/pages/follow-new/index?id=${id}&name=${encodeURIComponent(name)}` });
  },

  // 一次性订阅：每点一次，这个关注有新文件时用微信提醒一次
  remind(e) {
    if (this.data.mode !== 'live') {
      wx.showToast({ title: '演示数据不能订阅', icon: 'none' });
      return;
    }
    requestOnce('newDocs', { followId: e.currentTarget.dataset.id }).then((ok) => {
      if (ok) wx.showToast({ title: '已订阅 1 次，有新文件时微信提醒你', icon: 'none' });
    });
  },

  toggleManage() {
    this.setData({ managing: !this.data.managing });
  },

  remove(e) {
    const { id, name } = e.currentTarget.dataset;
    if (this.data.mode !== 'live') return;
    wx.showModal({
      title: '取消关注',
      content: `不再关注"${name}"？这个关注的微信提醒也会一起取消。`,
      confirmText: '取消关注',
      confirmColor: '#E5484D',
      cancelText: '再想想',
      success: (res) => {
        if (!res.confirm) return;
        api.deleteFollow(id)
          .then(() => this.load())
          .catch((err) => wx.showToast({ title: err.message || '操作失败，请稍后再试', icon: 'none' }));
      },
    });
  },

  addSub() {
    wx.navigateTo({ url: '/pages/search/index' });
  },
});
