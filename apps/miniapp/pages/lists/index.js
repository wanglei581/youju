const api = require('../../utils/api.js');
const { todayISO, cnDate } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { readCache, writeCache, cachedLabel } = require('../../utils/cache.js');

// 最近发布的名单和比对范围存一份到本机，下次打开先显示（utils/cache.js）
const LISTS_CACHE = 'youjuListsCacheV1';
const SCOPE_CACHE = 'youjuListScopeV1';

const CHIPS = ['全部', '科技型中小企业', '专精特新'];

// 转发（D-070）：查到了名单才在卡片标题和路径里带企业名称（名单上的都是企业和机构）；没查、正在查、
// 没查到、查询出错（如输入像个人姓名被服务端拒绝）时只转发这一页，不带输入的内容。
// 名称里连续的空白并成一个；超过 60 个字的不带（企业全称和统一社会信用代码都远短于这个长度），打开时也不自动查
const NAME_MAX = 60;
const shareName = (value) => String(value || '').replace(/\s+/g, ' ').trim();
// 网址参数解码：微信把路径里的参数原样（没解码）交给 onLoad，这里解码一次；坏的编码不报错，原样当作文字
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
    lookupChanged: false,
    lookupMode: 'identities',
    identityResult: null,
    selectedIdentity: null,
    lookupRevision: '',
    identityPage: 1,
    matchPage: 1,
    nextIdentityCursor: null,
    nextMatchCursor: null,
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
    this.visible = false;
    this.lookupSeq = (this.lookupSeq || 0) + 1;
    this.needsLookupRefresh = !!this.data.queried;
    this.clearLookup();
    if (api.stopStartup) api.stopStartup(this);
    stopDay(this);
  },

  onUnload() {
    this.visible = false;
    this.lookupSeq = (this.lookupSeq || 0) + 1;
    if (api.stopStartup) api.stopStartup(this);
    stopDay(this);
  },

  onShow() {
    this.visible = true;
    if (this.needsLookupRefresh) { this.needsLookupRefresh = false; this.query(); }
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
    const name = shareName(decode(query && query.q));
    if (name.replace(/\s/g, '').length >= 2 && name.length <= NAME_MAX) {
      this.setData({ name });
      this.query();
    }
  },

  // 转发给好友和群（D-070）：不开朋友圈，不带任何奖励
  onShareAppMessage() {
    const { queried, result, looking, lookupError } = this.data;
    const found = !looking && !lookupError && result && result.items && result.items.length > 0;
    const name = shareName(queried);
    if (!found || !name || name.length > NAME_MAX) return { title: '查企业上过哪些名单', path: '/pages/lists/index' };
    return { title: `查名单：${name}`, path: `/pages/lists/index?q=${encodeURIComponent(name)}` };
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
      // 实时请求先失败、快照随后到达时，也清掉先写上的错误提示（10/18 验收，Codex 指出）
      this.setData({ loading: false, weekTotal: res.week_total, total: res.total, items: res.items, cachedText, staleError: false, error: '' });
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

  // 从转发卡片打开（左上角是"首页"）时直接回首页；平常返回上一页，返回不了也回首页
  back() {
    if (this.data.firstPage) { wx.switchTab({ url: '/pages/home/index' }); return; }
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  onInput(e) {
    this.lookupSeq = (this.lookupSeq || 0) + 1;
    this.clearLookup();
    this.setData({ name: e.detail.value, queried: '', lookupError: '', lookupChanged: false });
  },

  clearLookup() {
    this.identityCursors = ['']; this.identityIndex = 0;
    this.matchCursors = ['']; this.matchIndex = 0;
    this.setData({ result: null, identityResult: null, selectedIdentity: null, looking: false,
      lookupRevision: '', nextIdentityCursor: null, nextMatchCursor: null, identityPage: 1, matchPage: 1 });
  },

  query() {
    const name = this.data.name.trim();
    if (name.replace(/\s+/g, '').length < 2) {
      wx.showToast({ title: '请输入企业全称或统一社会信用代码', icon: 'none' });
      return Promise.resolve();
    }
    this.lookupSeq = (this.lookupSeq || 0) + 1;
    this.clearLookup();
    this.setData({ queried: name, lookupError: '', lookupChanged: false, demo: !api.isLive() });
    return this.loadIdentities();
  },

  // 游标视作不透明值，只沿当前版本前进，页面不拼接不同请求的名单。
  checkLookupPage(res, revision, cursors, index) {
    if (!res || typeof res.revision !== 'string' || !res.revision) throw new Error('查询结果格式有误，请重新查询');
    if (revision && res.revision !== revision) throw Object.assign(new Error('查询数据已更新'), { status: 409 });
    const next = res.nextCursor == null ? null : res.nextCursor;
    if (next !== null && (typeof next !== 'string' || !next || cursors.slice(0, index + 1).includes(next))) throw new Error('查询分页信息有误，请重新查询');
    return next;
  },

  lookupFailed(err) {
    const changed = err.status === 409;
    if (changed || [403, 404, 410].includes(err.status)) this.clearLookup();
    this.setData({ looking: false, lookupChanged: changed,
      lookupError: changed ? '名单数据已更新，旧结果已清空。请重新查询并选择身份。' : err.message || '查询未完成，请重试' });
  },

  loadIdentities() {
    const seq = this.lookupSeq = (this.lookupSeq || 0) + 1;
    const current = () => seq === this.lookupSeq && this.visible !== false;
    const revision = this.data.lookupRevision;
    const cursor = this.identityCursors[this.identityIndex];
    this.setData({ lookupMode: 'identities', looking: true, lookupError: '', lookupChanged: false,
      result: null, identityResult: null, selectedIdentity: null, nextIdentityCursor: null, nextMatchCursor: null });
    return api.lookupIdentities(this.data.queried, { cursor, revision }).then((res) => {
      if (!current()) return;
      const next = this.checkLookupPage(res, revision, this.identityCursors, this.identityIndex);
      if (!Array.isArray(res.identities) || res.identities.some(i => !i || !['code', 'unverified'].includes(i.kind) || typeof i.key !== 'string' || !i.key || typeof i.name !== 'string' || i.kind === 'code' && typeof i.creditCode !== 'string')) throw new Error('查询身份格式有误，请重新查询');
      const since = res.coverage && res.coverage.since;
      this.setData({ looking: false, identityResult: res, lookupRevision: res.revision,
        nextIdentityCursor: next, identityPage: this.identityIndex + 1, coverageSince: since ? cnDate(since) : '' });
      if (res.identityState === 'single' && res.identities.length === 1 && next === null) return this.selectIdentity(res.identities[0]);
    }).catch((err) => { if (current()) this.lookupFailed(err); });
  },

  pickIdentity(e) {
    if (this.data.looking || !this.data.identityResult) return;
    const identity = this.data.identityResult.identities[Number(e.currentTarget.dataset.index)];
    if (identity) return this.selectIdentity(identity);
  },

  selectIdentity(identity) {
    this.matchCursors = ['']; this.matchIndex = 0;
    this.setData({ selectedIdentity: identity, result: null, matchPage: 1, nextMatchCursor: null });
    return this.loadMatches();
  },

  loadMatches() {
    const identity = this.data.selectedIdentity;
    if (!identity) return Promise.resolve();
    const seq = this.lookupSeq = (this.lookupSeq || 0) + 1;
    const current = () => seq === this.lookupSeq && this.visible !== false;
    const revision = this.data.lookupRevision;
    const cursor = this.matchCursors[this.matchIndex];
    this.setData({ lookupMode: 'matches', looking: true, lookupError: '', lookupChanged: false, result: null, nextMatchCursor: null });
    return api.lookupPage(this.data.queried, identity.key, { cursor, revision }).then((res) => {
      if (!current()) return;
      const next = this.checkLookupPage(res, revision, this.matchCursors, this.matchIndex);
      if (!res.identity || res.identity.key !== identity.key || !Array.isArray(res.items)) throw new Error('查询身份不一致，请重新查询');
      const since = res.coverage && res.coverage.since;
      this.setData({ looking: false, result: res, selectedIdentity: res.identity, lookupRevision: res.revision,
        nextMatchCursor: next, matchPage: this.matchIndex + 1, coverageSince: since ? cnDate(since) : '' });
    }).catch((err) => { if (current()) this.lookupFailed(err); });
  },

  nextIdentity() {
    if (this.data.looking || this.data.lookupError || this.data.nextIdentityCursor === null) return;
    this.identityCursors = this.identityCursors.slice(0, this.identityIndex + 1);
    this.identityCursors.push(this.data.nextIdentityCursor); this.identityIndex += 1;
    return this.loadIdentities();
  },
  previousIdentity() {
    if (this.data.looking || this.data.lookupError || this.identityIndex < 1) return;
    this.identityIndex -= 1; return this.loadIdentities();
  },
  nextMatch() {
    if (this.data.looking || this.data.lookupError || this.data.nextMatchCursor === null) return;
    this.matchCursors = this.matchCursors.slice(0, this.matchIndex + 1);
    this.matchCursors.push(this.data.nextMatchCursor); this.matchIndex += 1;
    return this.loadMatches();
  },
  previousMatch() {
    if (this.data.looking || this.data.lookupError || this.matchIndex < 1) return;
    this.matchIndex -= 1; return this.loadMatches();
  },
  changeIdentity() {
    if (this.data.looking) return;
    return this.loadIdentities();
  },
  retryLookup() {
    if (this.data.looking) return;
    if (this.data.lookupChanged || !this.data.lookupRevision) return this.query();
    return this.data.lookupMode === 'matches' ? this.loadMatches() : this.loadIdentities();
  },

  locate(e) {
    if (this.data.looking || this.data.lookupError || !this.data.result) return;
    const item = this.data.result.items[Number(e.currentTarget.dataset.index)];
    const entry = item && item.matched && item.matched.entryId;
    if (entry && /^\d+$/.test(String(entry))) wx.navigateTo({ url: `/pages/reader/index?id=${encodeURIComponent(item.id)}&entry=${encodeURIComponent(entry)}` });
  },

  evidence(e) {
    const id = e.currentTarget.dataset.id;
    if (id) wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(id)}` });
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
