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

const KIND_ROWS = [
  { kind: 'interpret', name: 'AI 解读', desc: '把一份通知的原句分好类，约半分钟' },
  { kind: 'clause', name: '条款对照', desc: '申报条件和一家企业的情况并排列出' },
];
// 近 7 天能解读的申报通知：由后台筛（ai=1），不在本机从一页里挑（首页那 8 条什么类型都有，挑完可能一条不剩）
const RECENT = { kind: '申报通知', time: '近 7 天', ai: 1, limit: 5, offset: 0, fold: 1 };
const RECENT_ALL = `/pages/search/index?kind=${encodeURIComponent('申报通知')}&time=${encodeURIComponent('近 7 天')}&ai=1`;
const RECORDS = 3;
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
    today: '',
  },

  onLoad() {
    const { statusBarHeight, navHeight } = getApp().globalData;
    this.setData({ statusBarHeight, navHeight });
    // 用着用着后台说 AI 关了、或不在白名单了：回首页
    this.unsubscribe = access.subscribe((state) => {
      if (this.shown && state.confirmed && !state.visible) this.leave();
    });
  },

  onUnload() {
    if (this.unsubscribe) this.unsubscribe();
  },

  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 4 });
    this.shown = true;
    // 刚才看的那份先不给按钮：后台再核一次还能不能解读（可能下架了、失去了资格），核过再给
    const last = access.lastDoc();
    // 上次进来时的能力、次数和拦下原因都先清掉，显示加载中；能力接口回来之前不给"AI 解读""条款对照"
    this.recentItems = [];
    this.setData({
      today: todayISO(), capsLoading: true, kinds: [], gate: '', gateText: '', interpretOpen: false, clauseOpen: false,
      recent: [], lastDoc: last ? Object.assign({}, last, { ai: null }) : null,
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
  },

  leave() {
    if (this.leaving) return;
    this.leaving = true;
    wx.switchTab({ url: '/pages/home/index', complete: () => { this.leaving = false; } });
  },

  load(seq) {
    this.setData({ capsLoading: true });
    api.advisorCapabilities({ refresh: true }).then((caps) => {
      if (seq !== this.seq) return;
      const ai = (caps && caps.ai_advisor) || {};
      const gate = gateOf(caps);
      // 能力接口说这个人用不了（刚关了 AI、被移出白名单）：再问一次，确认没开着就回首页
      if (gate === 'off') access.refresh();
      const kinds = ai.enabled === true ? kindRows(ai) : [];
      const open = (kind) => { const row = kinds.find((item) => item.kind === kind); return !gate && !!row && !row.out; };
      this.setData({
        capsLoading: false,
        gate,
        gateText: gateTextOf(gate, caps),
        kinds,
        // 刚才看的那份、近 7 天那几份上的按钮：账号能用、今天还有次数、后台能处理、全站没满。
        // 没同意协议（consent）时照样给：点了进解读页、对照页，先走同意（D-075 第四节）
        interpretOpen: gate === 'consent' || open('interpret'),
        clauseOpen: gate === 'consent' || open('clause'),
        disclosure: ai.enabled === true ? disclosureLine(ai.disclosure) : '',
      });
      this.showRecent();
    });
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
    // 我的 AI 记录：和"我的 → 我的记录"是同一份，这里列最近 3 条 AI 的
    api.advisorTasks().then((res) => {
      if (seq !== this.seq) return;
      const items = ((res && res.items) || []).filter((task) => task.kind === 'interpret' || task.kind === 'clause');
      this.setData({ records: items.slice(0, RECORDS).map(recordRow) });
    }).catch(() => {
      if (seq === this.seq) this.setData({ records: [] });
    });
  },

  // 重试：不管刚才是不是没连上，马上再问一次
  // 近 7 天那几份：这次不能开始新的解读时（还没问到、拦下了、用完了）行上不放"AI 解读"，点标题照样看通知
  showRecent() {
    const open = this.data.interpretOpen;
    this.setData({ recent: (this.recentItems || []).map((doc) => (open ? doc : Object.assign({}, doc, { ai: null }))) });
  },

  retry() {
    access.refresh().then(() => { if (this.shown) this.onShow(); });
  },

  toggleScope() {
    this.setData({ scopeOpen: !this.data.scopeOpen });
  },

  // 没同意协议：走同意流程，同意后回到这一页刷新
  consent() {
    if (this.data.gate !== 'consent') return;
    ensureConsent().then((ok) => { if (ok && this.shown) this.onShow(); });
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
    if (doc && doc.ai && doc.ai.clause === true && this.data.clauseOpen) wx.navigateTo({ url: context.clauseUrl({ docId: doc.id, docTitle: doc.title }) });
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
