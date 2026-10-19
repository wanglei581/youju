// 统一工具中心：数据工具与 AI 分别按能力授权，免费额度不依赖付费开关。
const api = require('../../utils/api.js');
const access = require('../../utils/ai-access.js');
const { taskView, internalNotice, allowedKinds, dataKinds, allKinds, gateState, remainingOf, AI_LABEL, FAB_TEXT, OFF_TEXT, GATE_TEXT, SCOPE, gateText } = require('../../utils/advisor.js');
const context = require('../../utils/tool-context.js');
const boost = require('../../utils/boost.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');

Page({
  data: { toolsEnabled: false, dataEnabled: false, compareEnabled: false, trackEnabled: false, dataStatus: '', dataError: '', seasonNote: '', aiGate: 'off', aiGateText: OFF_TEXT, aiLabel: AI_LABEL, fabText: FAB_TEXT, scope: SCOPE, gate: 'off', gateText: OFF_TEXT, loading: true, aiEnabled: false, interpretEnabled: false, clauseEnabled: false, aiPaused: false, aiFull: false, remaining: null, clauseRemaining: null, interpretOut: false, clauseOut: false,
    aiNotice: '', disclosure: '', context: null, recent: [], recentLoading: true, recentError: '', showScope: false,
    // 批量工具卡片（D-076）：在售时放在最上面——本月免费还剩几次、两档价格、"了解批量工具"；买过的写使用期到哪天。
    // 没在售时为 null，不显示，正式版开卖前看不到
    payCard: null },
  onLoad(query) { this.setData({ context: context.read(query) }); },
  // 每次显示都重新问能力接口：在途的、缓存的答复都不用（可能是离开以前、0 点以前问的，AI 暂时不可用、满了跟着变，第 23 轮）
  onShow() {
    this.visible = true;
    if (api.forgetCapabilities) api.forgetCapabilities();
    // 停在这一页跨过零点：今天还剩几次、全站满没满重新问（工具中心不会自己开始任务，第 24 轮）
    watchDay(this, () => { if (this.visible) { if (api.forgetCapabilities) api.forgetCapabilities(); this.load(); } });
    this.load();
  },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; stopDay(this); },
  onUnload() { this.onHide(); },
  clearContext() { this.setData({ context: null }); },
  toggleScope() { this.setData({ showScope: !this.data.showScope }); },
  load() {
    const epoch = this.epoch = (this.epoch || 0) + 1;
    wx.setNavigationBarTitle({ title: '工具中心' });
    this.setData({ loading: true, toolsEnabled: false, dataEnabled: false, aiEnabled: false, interpretEnabled: false, clauseEnabled: false, disclosure: '', aiNotice: '', recent: [], dataStatus: '', dataError: '', payCard: null });
    this.payOn = false; this.offer = null; this.quota = null; this.quotaDone = false;
    api.advisorCapabilities({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const ai = res.ai_advisor || {};
      const kinds = allowedKinds(ai);
      const aiEnabled = ai.enabled === true && kinds.length > 0;
      const data = dataKinds(res.data_tools);
      const toolsEnabled = aiEnabled || data.length > 0;
      const disclosure = ai.disclosure || {};
      const aiGate = aiEnabled ? '' : (res.reason === 'consent' || res.reason === 'offline' ? res.reason : gateState(res));
      const gate = toolsEnabled ? '' : (res.reason === 'consent' || res.reason === 'offline' ? res.reason : gateState(res));
      this.setData({ loading: false, aiGate, aiGateText: gateText(aiGate, res), toolsEnabled, dataEnabled: data.length > 0, compareEnabled: data.includes('compare'), trackEnabled: data.includes('track'), aiEnabled, gate, gateText: gateText(gate, res),
        interpretEnabled: aiEnabled && kinds.includes('interpret'),
        clauseEnabled: aiEnabled && kinds.includes('clause'),
        // D-075：后台处理不了、全站今天满了时不给开始的按钮，写明原因
        aiPaused: aiEnabled && ai.available === false,
        aiFull: aiEnabled && ai.siteFull === true,
        // 今天还剩几次：后台没给就不写；用完了写明天恢复、不给开始的按钮（第 24 轮）
        remaining: remainingOf(ai, 'interpret'),
        clauseRemaining: remainingOf(ai, 'clause'),
        interpretOut: remainingOf(ai, 'interpret') === 0,
        clauseOut: remainingOf(ai, 'clause') === 0,
        aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '',
        disclosure: aiEnabled && disclosure.modelLabel && disclosure.filingNo ? `模型：${disclosure.modelLabel}｜备案号：${disclosure.filingNo}` : '' });
      this.kinds = allKinds(res);
      // 在售（服务端说对这个人、这个小程序包在卖）才取价格；批量工具本身没开时不提购买
      this.payOn = data.length > 0 && !!(res.pay && res.pay.enabled === true);
      if (toolsEnabled) this.loadRecent(epoch);
      if (data.length) this.loadQuota(epoch);
      if (this.payOn) this.loadOffer(epoch);
    }).catch(() => {
      if (this.visible && epoch === this.epoch) this.setData({ loading: false, toolsEnabled: false, dataEnabled: false, aiEnabled: false, gate: 'offline', gateText: GATE_TEXT.offline, disclosure: '', aiNotice: '' });
    });
  },
  loadQuota(epoch = this.epoch) {
    // 额度请求单独编号：点"刷新额度"后旧请求晚到，不覆盖新的额度和使用期（Codex 审阅）
    const serial = this.quotaSerial = (this.quotaSerial || 0) + 1;
    const current = () => this.visible && epoch === this.epoch && serial === this.quotaSerial;
    this.setData({ dataError: '' });
    api.advisorStatus().then((res) => {
      if (!current()) return;
      const quota = res.quota || {};
      if (!res.enabled || !['compare', 'track'].every((kind) => quota[kind] && Number.isFinite(quota[kind].limit) && Number.isFinite(quota[kind].used) && Number.isFinite(quota[kind].maxRows))) throw new Error('额度信息不完整，请重试');
      const remaining = (kind) => Math.max(0, ((quota[kind] || {}).limit || 0) - ((quota[kind] || {}).used || 0));
      // 加量体验中（D-070 ③）显示体验的每天次数；申报季加量的起止日期另起一行
      const trial = boost.trialView(res.trial);
      const free = `本月比对剩余 ${remaining('compare')}/${quota.compare.limit} 次、每次最多 ${quota.compare.maxRows} 家；通知表剩余 ${remaining('track')}/${quota.track.limit} 次、每次最多 ${quota.track.maxRows} 家`;
      this.quota = { compare: remaining('compare'), track: remaining('track'), rows: { compare: quota.compare.maxRows, track: quota.track.maxRows },
        pass: res.pass && res.pass.active ? res.pass : null, trial };
      // 在售时不再写"不需要购买"（买得到却说不用买，会让人以为没有付费功能）；没在售时照旧
      this.setData({ seasonNote: boost.seasonLine(res.season), dataStatus: this.quota.pass ? `批量工具使用期内，比对每次最多 ${quota.compare.maxRows} 家，通知表每次最多 ${quota.track.maxRows} 家，不限提交次数`
        : trial.trialActive ? boost.trialLine(trial)
          : this.payOn ? free : `${free}。免费额度不需要购买` });
      this.quotaDone = true;
      this.renderPayCard();
    }).catch((err) => {
      if (!current()) return;
      this.setData({ dataError: `额度未能更新：${err.message}` });
      // 读不到额度时卡片只写价格，不写免费次数；刷新失败也丢掉上一次的额度和使用期，不显示过时的次数（Codex 第二轮）
      this.quota = null; this.quotaDone = true; this.renderPayCard();
    });
  },
  // 在售时取一次两档价格；取不到就不显示卡片（不写"暂未开放"之类，D-074）
  loadOffer(epoch = this.epoch) {
    const pay = require('../../utils/pay.js');
    api.payProducts().then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const products = pay.normalizeProducts(res);
      this.offer = products.length ? { line: pay.tiersLine(products) } : null;
      this.renderPayCard();
    }).catch(() => { if (this.visible && epoch === this.epoch) { this.offer = null; this.renderPayCard(); } });
  },
  // 额度有了结果（读到或读不到）才画，免得卡片先没有免费次数、再补上；使用期内写到期日和续买，
  // 免费次数用完时第一句写"已用完"
  renderPayCard() {
    if (!this.payOn) { this.setData({ payCard: null }); return; }
    if (!this.quotaDone) return;
    const pay = require('../../utils/pay.js');
    const quota = this.quota;
    // 每次多少家按额度接口里两项各自的上限写，两项一样时只写一次（Codex 第三轮：不写死、不只看比对）
    const rows = quota && (quota.rows.compare === quota.rows.track ? `每次最多 ${quota.rows.compare} 家`
      : `比对每次最多 ${quota.rows.compare} 家、通知表每次最多 ${quota.rows.track} 家`);
    if (quota && quota.pass) {
      const day = pay.monthDay(quota.pass.expiresAt);
      this.setData({ payCard: { usedUp: false, quotaLine: `批量工具使用期${day ? `到 ${day}` : '内'}：批量名单比对、客户相关通知表不限次数，${rows}`,
        // 取不到价格时只写到期日，不放买不了的"续买"按钮（Codex 第二轮）
        offerLine: this.offer ? `再买一次接着往后顺延：${this.offer.line}，一次付清，到期不再扣钱` : '', button: this.offer ? '续买批量工具' : '' } });
      return;
    }
    if (!this.offer) { this.setData({ payCard: null }); return; }
    const usedUp = !!quota && !quota.trial.trialActive && quota.compare === 0 && quota.track === 0;
    // 只用完一项时那一项写"已用完"，不写"还剩 0 次"
    const left = (name, count) => (count === 0 ? `${name}已用完` : `${name}还剩 ${count} 次`);
    const quotaLine = !quota ? '' : quota.trial.trialActive ? boost.trialLine(quota.trial)
      : usedUp ? '本月免费次数已用完，下月 1 日恢复'
        : `本月免费：${left('比对', quota.compare)}、${left('通知表', quota.track)}，${rows}`;
    this.setData({ payCard: { usedUp, quotaLine, offerLine: `要一次处理更多家：${this.offer.line}，不限次数，每次 500 家以内；一次付清，到期不再扣钱`, button: '了解批量工具' } });
  },
  buyPass() { if (this.data.payCard && this.data.payCard.button) wx.navigateTo({ url: '/pages/pass/index' }); },
  refreshQuota() { this.loadQuota(); },
  dataTool(e) {
    const kind = e.currentTarget.dataset.kind;
    if ((this.kinds || []).includes(kind) && ['compare', 'track'].includes(kind)) wx.navigateTo({ url: `/pages/advisor-form/index?kind=${kind}` });
  },
  // 正式版：没同意协议的先去同意，回来自动刷新；连不上的重试
  consent() {
    if (this.data.gate !== 'consent' && this.data.aiGate !== 'consent') return;
    if (this.consenting) return;
    this.consenting = true;
    // 先确认 AI 对这个人开没开（直接打开这一页时可能还没问过）：开着就写明同意以后 AI 也能用、只能读完两份全文再同意；
    // 关着、没问到时照旧（第 15、16 轮）
    access.ensure().then((state) => {
      const options = state.visible === true && state.confirmed === true ? { purpose: 'tools' } : undefined;
      return require('../../utils/consent.js').ensureConsent(options);
    }).then((ok) => {
      this.consenting = false;
      if (ok && this.visible) this.load();
    });
  },
  reload() { if (this.data.gate === 'offline') this.load(); },
  search() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/search/index' }); },
  report() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/complaint/index' }); },
  loadRecent(epoch = this.epoch) {
    const kinds = this.kinds || [];
    this.setData({ recentLoading: true, recentError: '' });
    api.advisorTasks().then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const recent = (res.items || []).filter((task) => kinds.includes(task.kind)).slice()
        .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt) || b.id - a.id).slice(0, 3).map(taskView);
      this.setData({ recentLoading: false, recent });
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ recentLoading: false, recentError: err.message }); });
  },
  retryRecent() { this.loadRecent(); },
  interpret() {
    if (!this.data.interpretEnabled || this.data.interpretOut || !this.data.context || !this.data.context.docId) return;
    const { docId, title } = this.data.context;
    wx.navigateTo({ url: `/pages/advisor-interpret/index?docId=${encodeURIComponent(docId)}&title=${encodeURIComponent(title)}` });
  },
  clause() {
    if (!this.data.clauseEnabled || this.data.clauseOut || !this.data.context || !this.data.context.docId) return;
    const { docId, title } = this.data.context;
    wx.navigateTo({ url: `/pages/advisor-clause/index?docId=${encodeURIComponent(docId)}&title=${encodeURIComponent(title)}` });
  },
  tasks(e) {
    if (!this.data.toolsEnabled) return;
    const id = e && e.currentTarget && e.currentTarget.dataset.id;
    const task = id != null && this.data.recent.find((item) => String(item.id) === String(id));
    if (task && task.kind === 'clause' && this.data.clauseEnabled) {
      wx.navigateTo({ url: `/pages/advisor-clause/index?taskId=${encodeURIComponent(id)}` }); return;
    }
    if (task && task.kind === 'interpret' && this.data.interpretEnabled) {
      wx.navigateTo({ url: `/pages/advisor-interpret/index?taskId=${encodeURIComponent(id)}` }); return;
    }
    wx.navigateTo({ url: '/pages/advisor-tasks/index' });
  },
});
