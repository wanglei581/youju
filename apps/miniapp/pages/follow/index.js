const api = require('../../utils/api.js');
const legal = require('../../content/legal.js');
const { todayISO } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { requestOnce } = require('../../utils/subscribe.js');
const { ensureConsent, syncConsent } = require('../../utils/consent.js');
const { describe } = require('../../utils/conditions.js');

// 三种状态：demo 没连接口时的演示数据；gated 未登录（或协议未定稿）；live 真实的关注
Page({
  data: { mode: 'demo', weekStart: '', newTotal: 0, follows: [], changes: [], changesError: '', remindLeft: null, managing: false, loading: false, error: '', legalFinal: legal.final },

  onHide() {
    stopDay(this);
  },

  onUnload() {
    stopDay(this);
  },

  onShow() {
    this.retried = false;
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
    // 换了设备、本机没有同意记录时，先向服务端核对一次
    syncConsent().then((ok) => {
      if (!ok) {
        this.setData({ mode: 'gated', follows: [], changes: [], changesError: '', newTotal: 0 });
        return;
      }
      this.loadLive();
    });
  },

  loadLive() {
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
        this.loadReminders();
        return this.loadChanges(follows);
      })
      .catch((err) => {
        if (err.status === 403) { this.onDenied(); return; }
        this.setData({ loading: false, error: `加载失败：${err.message}` });
      });
  },

  // 每个关注还能提醒几次（还没发出的一次性订阅授权）；提醒用掉后提示再订阅（验收 B2、B5）。
  // 加载失败时不显示次数，不影响关注列表
  loadReminders() {
    // 先清掉旧的次数（加载失败时不显示过时的次数）；只采用最后一次查询的结果
    const seq = (this.remindSeq || 0) + 1;
    this.remindSeq = seq;
    this.setData({ remindLeft: null });
    api.mySubscriptions()
      .then((res) => {
        if (seq !== this.remindSeq) return;
        const left = {};
        res.items.forEach((s) => { if (s.kind === 'newDocs' && s.followId) left[s.followId] = (left[s.followId] || 0) + 1; });
        this.setData({ remindLeft: left });
      })
      .catch(() => {});
  },

  // 服务端返回 403：没有同意记录或协议已更新（本机记录已由请求层清掉）。先向服务端重新核对一次；
  // 仍然 403 就回到"登录后使用"，不再重试（避免服务端和小程序的协议版本不一致时反复请求）
  onDenied() {
    if (!this.retried) {
      this.retried = true;
      this.setData({ loading: false });
      this.load();
      return;
    }
    this.setData({ mode: 'gated', loading: false, follows: [], changes: [], changesError: '', newTotal: 0 });
  },

  // 新文件：取有新文件的前 3 个关注，每个最多 5 份，合并去重后按发布日期排
  loadChanges(follows) {
    const withNew = follows.filter((f) => f.newCount > 0).slice(0, 3);
    let failed = null;
    let denied = false;
    return Promise.all(withNew.map((f) => api.followNew(f.id, { limit: 5 })
      .then((r) => r.items.map((d) => Object.assign({ label: f.name }, d)))
      .catch((err) => { if (err.status === 403) denied = true; else failed = err; return []; })))
      .then((lists) => {
        // 有一个 403 就说明同意已失效
        if (denied) { this.onDenied(); return; }
        const seen = {};
        const changes = [];
        [].concat(...lists)
          .sort((a, b) => (b.date || '').localeCompare(a.date || ''))
          .forEach((d) => { if (!seen[d.id]) { seen[d.id] = true; changes.push(d); } });
        this.setData({ changes: changes.slice(0, 10), changesError: failed ? `新文件没有加载出来：${failed.message}` : '' });
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
      if (!ok) return;
      wx.showToast({ title: '已订阅 1 次，有新文件时微信提醒你', icon: 'none' });
      this.loadReminders();
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
