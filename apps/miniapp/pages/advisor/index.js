// 统一工具中心：数据工具与 AI 分别按能力授权，免费额度不依赖付费开关。
const api = require('../../utils/api.js');
const { taskView, internalNotice, allowedKinds, dataKinds, allKinds, aiTitle, gateState, AI_LABEL, FAB_TEXT, OFF_TEXT, GATE_TEXT, SCOPE, gateText } = require('../../utils/advisor.js');
const context = require('../../utils/tool-context.js');
const boost = require('../../utils/boost.js');

Page({
  data: { toolsEnabled: false, dataEnabled: false, compareEnabled: false, trackEnabled: false, dataStatus: '', dataError: '', seasonNote: '', aiGate: 'off', aiGateText: OFF_TEXT, aiLabel: AI_LABEL, fabText: FAB_TEXT, scope: SCOPE, gate: 'off', gateText: OFF_TEXT, loading: true, aiEnabled: false, interpretEnabled: false, clauseEnabled: false, remaining: 0, clauseRemaining: 0,
    aiNotice: '', disclosure: '', context: null, recent: [], recentLoading: true, recentError: '', showScope: false },
  onLoad(query) { this.setData({ context: context.read(query) }); },
  onShow() { this.visible = true; this.load(); },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; },
  onUnload() { this.onHide(); },
  clearContext() { this.setData({ context: null }); },
  toggleScope() { this.setData({ showScope: !this.data.showScope }); },
  load() {
    const epoch = this.epoch = (this.epoch || 0) + 1;
    wx.setNavigationBarTitle({ title: '工具中心' });
    this.setData({ loading: true, toolsEnabled: false, dataEnabled: false, aiEnabled: false, interpretEnabled: false, clauseEnabled: false, disclosure: '', aiNotice: '', recent: [], dataStatus: '', dataError: '' });
    api.advisorCapabilities({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const ai = res.ai_advisor || {};
      const kinds = allowedKinds(ai);
      const aiEnabled = ai.enabled === true && kinds.length > 0;
      const data = dataKinds(res.data_tools);
      const toolsEnabled = aiEnabled || data.length > 0;
      const disclosure = ai.disclosure || {};
      if (aiEnabled && !data.length) wx.setNavigationBarTitle({ title: aiTitle('AI 工具') });
      const aiGate = aiEnabled ? '' : (res.reason === 'consent' || res.reason === 'offline' ? res.reason : gateState(res));
      const gate = toolsEnabled ? '' : (res.reason === 'consent' || res.reason === 'offline' ? res.reason : gateState(res));
      this.setData({ loading: false, aiGate, aiGateText: gateText(aiGate, res), toolsEnabled, dataEnabled: data.length > 0, compareEnabled: data.includes('compare'), trackEnabled: data.includes('track'), aiEnabled, gate, gateText: gateText(gate, res),
        interpretEnabled: aiEnabled && kinds.includes('interpret'),
        clauseEnabled: aiEnabled && kinds.includes('clause'),
        remaining: (ai.remaining || {}).interpret || 0,
        clauseRemaining: (ai.remaining || {}).clause || 0,
        aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '',
        disclosure: aiEnabled && disclosure.modelLabel && disclosure.filingNo ? `模型：${disclosure.modelLabel}｜备案号：${disclosure.filingNo}` : '' });
      this.kinds = allKinds(res);
      if (toolsEnabled) this.loadRecent(epoch);
      if (data.length) this.loadQuota(epoch);
    }).catch(() => {
      if (this.visible && epoch === this.epoch) this.setData({ loading: false, toolsEnabled: false, dataEnabled: false, aiEnabled: false, gate: 'offline', gateText: GATE_TEXT.offline, disclosure: '', aiNotice: '' });
    });
  },
  loadQuota(epoch = this.epoch) {
    this.setData({ dataError: '' });
    api.advisorStatus().then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const quota = res.quota || {};
      if (!res.enabled || !['compare', 'track'].every((kind) => quota[kind] && Number.isFinite(quota[kind].limit) && Number.isFinite(quota[kind].used) && Number.isFinite(quota[kind].maxRows))) throw new Error('额度信息不完整，请重试');
      const remaining = (kind) => Math.max(0, ((quota[kind] || {}).limit || 0) - ((quota[kind] || {}).used || 0));
      // 加量体验中（D-070 ③）显示体验的每天次数；申报季加量的起止日期另起一行
      const trial = boost.trialView(res.trial);
      this.setData({ seasonNote: boost.seasonLine(res.season), dataStatus: res.pass && res.pass.active ? `批量工具使用期内，比对每次最多 ${quota.compare.maxRows} 家，通知表每次最多 ${quota.track.maxRows} 家，不限提交次数`
        : trial.trialActive ? boost.trialLine(trial)
          : `本月比对剩余 ${remaining('compare')}/${quota.compare.limit} 次、每次最多 ${quota.compare.maxRows} 家；通知表剩余 ${remaining('track')}/${quota.track.limit} 次、每次最多 ${quota.track.maxRows} 家。免费额度不需要购买` });
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ dataError: `额度未能更新：${err.message}` }); });
  },
  refreshQuota() { this.loadQuota(); },
  dataTool(e) {
    const kind = e.currentTarget.dataset.kind;
    if ((this.kinds || []).includes(kind) && ['compare', 'track'].includes(kind)) wx.navigateTo({ url: `/pages/advisor-form/index?kind=${kind}` });
  },
  // 正式版：没同意协议的先去同意，回来自动刷新；连不上的重试
  consent() {
    if (this.data.gate !== 'consent' && this.data.aiGate !== 'consent') return;
    require('../../utils/consent.js').ensureConsent().then((ok) => { if (ok && this.visible) this.load(); });
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
    if (!this.data.interpretEnabled || !this.data.context || !this.data.context.docId) return;
    const { docId, title } = this.data.context;
    wx.navigateTo({ url: `/pages/advisor-interpret/index?docId=${encodeURIComponent(docId)}&title=${encodeURIComponent(title)}` });
  },
  clause() {
    if (!this.data.clauseEnabled || !this.data.context || !this.data.context.docId) return;
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
