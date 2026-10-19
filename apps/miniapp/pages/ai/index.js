// AI 首页（D-075 第四节）：底栏第 5 格。第一屏不放空白提问框：两项能力各一行写清做什么、今天还剩几次，
// 下面直接是通知（刚才看的、近 7 天能解读的申报通知），再是我的 AI 记录和联系客服。
// 看得到板块和能提交任务分开：没同意协议的人照样能看通知列表，两项能力换成同意入口。
// AI 对这个人没开着（关闭、不在白名单，utils/ai-access.js 说了算）：直接回首页，不写"暂未开放"（创始人 10/19）。
// 不自动替用户解读任何一份，都由用户自己点（每天次数有限）。
const api = require('../../utils/api.js');
const access = require('../../utils/ai-access.js');
const { taskView, allowedKinds, AI_LABEL, SCOPE } = require('../../utils/advisor.js');
const { disclosureLine } = require('../../utils/ai-interpret.js');
const context = require('../../utils/tool-context.js');
const { ensureConsent } = require('../../utils/consent.js');
const { todayISO } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');

const KIND_ROWS = [
  { kind: 'interpret', name: 'AI 解读', desc: '把一份通知的原句分好类，约半分钟' },
  { kind: 'clause', name: '条款对照', desc: '申报条件和一家企业的情况并排列出' },
];
// 近 7 天能解读的申报通知：由后台筛（ai=1），不在本机从一页里挑（首页那 8 条什么类型都有，挑完可能一条不剩）
const RECENT = { kind: '申报通知', time: '近 7 天', ai: 1, limit: 5, offset: 0, fold: 1 };
const RECENT_ALL = `/pages/search/index?kind=${encodeURIComponent('申报通知')}&time=${encodeURIComponent('近 7 天')}&ai=1`;
const RECORDS = 3;
// 我的 AI 记录里有排队中、处理中的：停在这一页时过 5、10、15 秒再问，之后每 15 秒一次，最多问 15 分钟（第 18 轮）
const RECORD_POLL = [5000, 10000, 15000];
const RECORD_POLL_FOR = 15 * 60 * 1000;
const KEEP = '你填的企业情况保存 7 天，任务和结果保存 30 天。';

const GATE_TEXT = {
  consent: '使用 AI 前，请先阅读并同意《用户协议》和《隐私保护指引》。',
  updated: '《用户协议》和《隐私保护指引》已更新，请阅读并同意新版后再使用 AI。',
  offline: '暂时连不上服务器，请稍后重试。',
  paused: 'AI 暂时不可用，请稍后再试。',
  full: '今天 AI 的总用量满了，明天 0 点恢复；可以先看原文。',
};

// 能力接口的回话对应哪种情况：''（能用）、consent、offline、paused（后台处理不了）、full（全站满了）、off
function gateOf(caps) {
  const ai = (caps && caps.ai_advisor) || {};
  if (ai.enabled === true) {
    if (ai.available === false) return 'paused';
    if (ai.siteFull === true) return 'full';
    return '';
  }
  if (caps && (caps.reason === 'consent' || caps.reason === 'offline')) return caps.reason;
  return 'off';
}

function gateTextOf(gate, caps) {
  if (gate === 'consent' && caps && caps.consentUpdated === true) return GATE_TEXT.updated;
  return GATE_TEXT[gate] || '';
}

// 两项能力：各写各的剩几次；用完的那一项写明天恢复、以前的结果还在
function kindRows(ai) {
  const features = allowedKinds(ai);
  return KIND_ROWS.filter((row) => features.includes(row.kind)).map((row) => {
    const value = Number(ai.remaining && ai.remaining[row.kind]);
    const remaining = Number.isFinite(value) ? Math.max(0, Math.floor(value)) : null;
    const out = remaining === 0;
    return Object.assign({}, row, {
      remaining,
      out,
      outText: out ? `今天的${row.name} 5 次用完了，明天 0 点恢复；以前的结果在下面“我的 AI 记录”里照样能看。` : '',
    });
  });
}

function recordRow(task) {
  const view = taskView(task);
  return {
    id: String(view.id),
    kind: view.kind,
    kindLabel: view.kindLabel,
    title: view.docTitle || '',
    time: view.time ? view.time.slice(0, 10) : '',
    stateLabel: view.stateLabel,
  };
}

Page({
  data: {
    statusBarHeight: 20,
    navHeight: 44,
    aiLabel: AI_LABEL,
    scope: SCOPE,
    keep: KEEP,
    scopeOpen: false,
    // waiting：还没问到后台 AI 开没开；offline：没问到（连不上）；ready：后台确认开着。
    // 确认之前页面上不出现 AI 字样（旧链接直接打开时，可能其实是关着的）
    waiting: true,
    ready: false,
    offline: false,
    capsLoading: true,
    // 这次能力接口回来、能开始新的解读（没有拦下、今天还有次数）才给"AI 解读"按钮；条款对照同理
    interpretOpen: false,
    clauseOpen: false,
    gate: '',
    gateText: '',
    kinds: [],
    disclosure: '',
    lastDoc: null,
    recent: [],
    recentLoading: true,
    recentError: '',
    records: [],
    recordsError: '',
    today: '',
  },

  onLoad() {
    const { statusBarHeight, navHeight } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight });
    // 用着用着后台说 AI 关了、或不在白名单了：回首页。注销（状态被擦掉）时在途的结果作废、清掉页面上的通知和记录。
    // 后台能不能处理变了（admin 心跳停了、又恢复了）：按钮和"暂时不可用"马上跟着变，不用等下次进来（Codex 小程序第 7 轮）
    this.unsubscribe = access.subscribe((state) => {
      if (!state.visible) this.reset();
      if (this.shown && state.confirmed && !state.visible) this.leave();
      if (!state.visible) return;
      // 能力接口上次说后台处理不了、现在恢复了：再问一次能力接口，次数、全站满没满照新的来（第 8 轮）
      if (this.shown && this.capsResult && this.capsResult.gate === 'paused' && access.canOpen()) this.loadCaps(this.seq);
      else this.applyCaps();
    });
  },

  onUnload() {
    if (this.unsubscribe) this.unsubscribe();
    if (api.stopStartup) api.stopStartup(this);
    this.stopTimers();
  },

  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 4 });
    this.shown = true;
    // 连接慢（接口服务在重试）时和其他页面一样写"正在连接，比平时慢一点，请稍等…"（方案第四节，第 15 轮）
    if (api.watchStartup) api.watchStartup(this);
    // 停在这一页跨过零点：今天还剩几次、全站满没满、近 7 天的通知都重新问（第 18 轮）；
    // 我的 AI 记录照旧从进来那一刻算 15 分钟，不因为跨零点重新算起（第 19 轮）
    watchDay(this, () => {
      if (!this.shown) return;
      const since = this.recordSince;
      this.onShow();
      this.recordSince = since;
    });
    clearTimeout(this.recordTimer);
    this.recordPolls = 0;
    this.recordSince = Date.now();
    // 刚才看的那份先不给按钮：后台再核一次还能不能解读（可能下架了、失去了资格），核过再给
    const last = access.lastDoc();
    // 上次进来时的能力、次数和拦下原因都先清掉，显示加载中；能力接口回来之前不给"AI 解读""条款对照"
    this.recentItems = [];
    this.capsResult = null;
    this.setData({
      today: todayISO(), capsLoading: true, kinds: [], gate: '', gateText: '', interpretOpen: false, clauseOpen: false,
      recent: [], recentLoading: true, recentError: '', records: [], recordsError: '', lastDoc: last ? Object.assign({}, last, { ai: null }) : null,
      // 上次没连上：这次重新问的时候写"正在连接"，不留"连不上"（第 17 轮）。上次连上了就不动，免得切回来时闪一下
      ...(this.data.offline ? { waiting: true, offline: false } : {}),
    });
    const seq = (this.seq || 0) + 1;
    this.seq = seq;
    access.ensure().then((state) => {
      if (!this.shown || seq !== this.seq) return;
      if (!state.confirmed) { this.setData({ waiting: false, offline: true, ready: false }); return; }
      if (!state.visible) { this.leave(); return; }
      this.setData({ waiting: false, offline: false, ready: true });
      this.load(seq);
    });
  },

  onHide() {
    this.shown = false;
    if (api.stopStartup) api.stopStartup(this);
    this.stopTimers();
    // 离开以后回来的旧结果不再写页面
    this.seq = (this.seq || 0) + 1;
  },

  // 作废在途的结果，清掉页面上和账号有关的东西（我的 AI 记录、刚才看的那份）
  reset() {
    this.seq = (this.seq || 0) + 1;
    clearTimeout(this.recordTimer);
    this.recentItems = [];
    this.capsResult = null;
    // 页头、说明也收起，回到"还没问到"的样子：注销以后到重新确认之前不出现 AI 字样
    this.setData({
      ready: false, waiting: true, offline: false,
      records: [], recordsError: '', recent: [], recentLoading: true, recentError: '', lastDoc: null, kinds: [], gate: '', interpretOpen: false, clauseOpen: false, capsLoading: true,
    });
  },

  leave() {
    if (this.leaving) return;
    this.leaving = true;
    wx.switchTab({ url: '/pages/home/index', complete: () => { this.leaving = false; } });
  },

  load(seq) {
    this.loadCaps(seq);
    const last = this.data.lastDoc;
    if (last) {
      api.detail(last.id).then((doc) => {
        if (seq !== this.seq) return;
        if (doc && doc.ai && doc.ai.interpret === true) {
          // 文件本身能不能做条款对照也照详情的来
          this.setData({ lastDoc: Object.assign({}, last, { title: doc.title || last.title, ai: { interpret: true, clause: doc.ai.clause === true } }) });
        } else {
          access.forgetDoc(); this.setData({ lastDoc: null });
        }
      }).catch((err) => {
        // 下架或没了（404）：不再显示；连不上：照样列出、不给按钮
        if (seq === this.seq && err && err.status === 404) { access.forgetDoc(); this.setData({ lastDoc: null }); }
      });
    }
    this.setData({ recentLoading: true, recentError: '' });
    api.search(RECENT).then((res) => {
      if (seq !== this.seq) return;
      this.recentItems = ((res && res.items) || []).slice(0, RECENT.limit);
      this.setData({ recentLoading: false });
      this.showRecent();
    }).catch(() => {
      if (seq !== this.seq) return;
      this.setData({ recent: [], recentLoading: false, recentError: '近 7 天的通知没有加载出来' });
    });
    this.loadRecords(seq);
  },

  // 我的 AI 记录：和"我的 → 我的记录"是同一份，这里列最近 3 条 AI 的；一条都没有时不占地方
  loadRecords(seq) {
    api.advisorTasks().then((res) => {
      if (seq !== this.seq) return;
      const items = ((res && res.items) || []).filter((task) => task.kind === 'interpret' || task.kind === 'clause').slice(0, RECORDS);
      this.setData({ records: items.map(recordRow) });
      if (items.some((task) => task.status === 'queued' || task.status === 'running')) this.pollRecords(seq);
    }).catch((err) => {
      if (seq !== this.seq) return;
      // 还没同意协议、没开着（401、403、404）：本来就没有能看的记录，不写"没加载出来"、不给重试（同"我的记录"页，第 10 轮）
      if (err && [401, 403, 404].includes(err.status)) { this.setData({ records: [] }); return; }
      // 没加载出来不当成没有记录：卡片照样留着，写明没加载出来、给重试，"全部"照样能点进去（Codex 小程序第 7 轮）
      this.setData({ records: [], recordsError: '我的 AI 记录没有加载出来' });
    });
  },

  // 还有没处理完的：过一会儿再问一次记录，处理完就显示"已完成"；页面不在前面、换了一次加载就停
  pollRecords(seq) {
    clearTimeout(this.recordTimer);
    const delay = RECORD_POLL[Math.min(this.recordPolls || 0, RECORD_POLL.length - 1)];
    // 下一次要落在 15 分钟以内才安排（第 19 轮）
    if (Date.now() + delay - (this.recordSince || 0) > RECORD_POLL_FOR) return;
    this.recordPolls = (this.recordPolls || 0) + 1;
    this.recordTimer = setTimeout(() => { if (this.shown && seq === this.seq) this.loadRecords(seq); }, delay);
  },

  stopTimers() {
    clearTimeout(this.recordTimer);
    this.recordTimer = null;
    stopDay(this);
  },

  // 两项能力、今天还剩几次、拦下原因：问能力接口（每次都不用缓存）
  loadCaps(seq) {
    this.setData({ capsLoading: true });
    api.advisorCapabilities({ refresh: true }).then((caps) => {
      if (seq !== this.seq) return;
      const ai = (caps && caps.ai_advisor) || {};
      const gate = gateOf(caps);
      // 能力接口说这个人用不了（刚关了 AI、被移出白名单）：再问一次，确认没开着就回首页。
      // 能力接口说的能不能处理和 AI 板块记的对不上（之前问的）：也马上再问一次，各页面跟着变（第 9、11 轮）
      if (gate === 'off') access.refresh();
      else access.reconcile(caps);
      const kinds = ai.enabled === true ? kindRows(ai) : [];
      const open = (kind) => { const row = kinds.find((item) => item.kind === kind); return !gate && !!row && !row.out; };
      // 刚才看的那份、近 7 天那几份上的按钮：账号能用、今天还有次数、后台能处理、全站没满。
      // 没同意协议（consent）时也给（点了进解读页、对照页，先走同意，D-075 第四节）。
      // 这里只记这次能力接口怎么说；再和"后台现在能不能处理"合起来才定按钮和拦下原因（applyCaps）
      this.capsResult = { gate, gateText: gateTextOf(gate, caps), interpret: gate === 'consent' || open('interpret'), clause: gate === 'consent' || open('clause') };
      this.setData({ capsLoading: false, kinds, disclosure: ai.enabled === true ? disclosureLine(ai.disclosure) : '' });
      this.applyCaps();
    });
  },

  // 按钮和拦下原因 = 这次能力接口的结果 + utils/ai-access.js 现在说后台能不能处理，两样谁后变都按最新的算：
  // 后台处理不了时"AI 解读""条款对照"都收起，能用、全站满了的情况都改写"暂时不可用"；恢复了照能力接口的结果放出来
  applyCaps() {
    const caps = this.capsResult;
    if (!caps) return;
    const can = access.canOpen();
    const paused = !can && (caps.gate === '' || caps.gate === 'full');
    this.setData({
      gate: paused ? 'paused' : caps.gate,
      gateText: paused ? GATE_TEXT.paused : caps.gateText,
      interpretOpen: can && caps.interpret,
      clauseOpen: can && caps.clause,
    });
    this.showRecent();
  },

  // 重试：不管刚才是不是没连上，马上再问一次
  // 近 7 天那几份：这次不能开始新的解读时（还没问到、拦下了、用完了）行上不放"AI 解读"，点标题照样看通知
  showRecent() {
    const open = this.data.interpretOpen;
    this.setData({ recent: (this.recentItems || []).map((doc) => (open ? doc : Object.assign({}, doc, { ai: null }))) });
  },

  // 点了马上回到"正在连接"，不留"连不上"；问到之前再点不重复问（第 16 轮）
  retry() {
    if (this.retrying) return;
    this.retrying = true;
    this.setData({ waiting: true, offline: false });
    access.refresh().then(() => {
      this.retrying = false;
      if (this.shown) this.onShow();
    });
  },

  toggleScope() {
    this.setData({ scopeOpen: !this.data.scopeOpen });
  },

  // 没同意协议：走同意流程，同意后回到这一页刷新
  consent() {
    if (this.data.gate !== 'consent') return;
    ensureConsent({ purpose: 'ai' }).then((ok) => { if (ok && this.shown) this.onShow(); });
  },

  findDoc() {
    wx.navigateTo({ url: '/pages/search/index?ai=1' });
  },

  allRecent() {
    wx.navigateTo({ url: RECENT_ALL });
  },

  openDoc(e) {
    wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(e.currentTarget.dataset.id)}` });
  },

  clauseLast() {
    const doc = this.data.lastDoc;
    if (doc && doc.ai && doc.ai.clause === true && this.data.clauseOpen && access.canOpen()) wx.navigateTo({ url: context.clauseUrl({ docId: doc.id, docTitle: doc.title }) });
  },

  openRecord(e) {
    const { id, kind } = e.currentTarget.dataset;
    const page = kind === 'clause' ? 'advisor-clause' : 'advisor-interpret';
    wx.navigateTo({ url: `/pages/${page}/index?taskId=${encodeURIComponent(id)}` });
  },

  allRecords() {
    wx.navigateTo({ url: '/pages/advisor-tasks/index' });
  },

  help() {
    wx.navigateTo({ url: '/pages/help/index' });
  },

  report() {
    wx.navigateTo({ url: '/pages/complaint/index' });
  },
});
