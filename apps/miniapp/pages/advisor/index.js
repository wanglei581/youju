const { loginOpen } = require('../../utils/consent.js');
const api = require('../../utils/api.js');
const { taskView, internalNotice, allowedKinds } = require('../../utils/advisor.js');
const pay = require('../../utils/pay.js');
const context = require('../../utils/tool-context.js');

Page({
  data: { loginOpen: loginOpen(), loading: true, error: '', enabled: false, skills: [], context: null,
    recent: [], recentLoading: false, recentError: '', showPassLink: false, passNotice: '',
    aiLoading: true, aiEnabled: false, interpretEnabled: false, clauseEnabled: false,
    remaining: 0, clauseRemaining: 0, aiNotice: '', disclosure: '' },
  onLoad(query) { if (this.data.loginOpen) this.setData({ context: context.read(query) }); },
  onShow() { this.visible = true; this.load(); },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; },
  onUnload() { this.onHide(); },
  clearContext() { this.setData({ context: null }); },
  load() {
    if (!this.data.loginOpen) return;
    const epoch = this.epoch = (this.epoch || 0) + 1;
    this.recentEpoch = null; this.rawRecent = []; this.kinds = [];
    this.setData({ loading: true, error: '', enabled: false, skills: [], showPassLink: false, passNotice: '',
      aiLoading: true, aiEnabled: false, interpretEnabled: false, clauseEnabled: false, disclosure: '', aiNotice: '',
      recent: [], recentError: '', recentLoading: false });
    // 状态、商品和 AI 权限各自更新；任一个失败或迟迟未返回，不阻塞另一个区块。
    api.advisorStatus({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      if (res.enabled !== true) { this.setData({ loading: false }); return; }
      this.passActive = !!(res.pass && res.pass.active);
      this.setData({ loading: false, enabled: true, showPassLink: !this.passActive && !!this.productsAvailable,
        passNotice: pay.toolStatus(res, 'compare').notice, skills: [
          { kind: 'compare', title: '批量名单比对', description: '在已收录的公示/公布名单里查找客户' },
          { kind: 'track', title: '客户相关通知表', description: '按客户省份列出近 90 天发布的申报通知；截止日期只写人工核对过的' },
        ].map((skill) => Object.assign(skill, { remaining: Math.max(0, res.quota[skill.kind].limit - res.quota[skill.kind].used), statusLine: pay.toolStatus(res, skill.kind).line })) });
      this.ensureRecent(epoch);
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ loading: false, error: err.status === 404 ? '工具暂未开放' : err.message }); });
    this.passActive = false; this.productsAvailable = false;
    api.payProducts().then(pay.normalizeProducts, () => []).then((products) => {
      if (!this.visible || epoch !== this.epoch) return;
      this.productsAvailable = products.length > 0;
      this.setData({ showPassLink: this.data.enabled && !this.passActive && this.productsAvailable });
    });
    api.advisorCapabilities({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const ai = (res && res.ai_advisor) || {}; const kinds = allowedKinds(ai);
      const aiEnabled = ai.enabled === true && kinds.length > 0; const disclosure = ai.disclosure || {};
      this.kinds = aiEnabled ? kinds : [];
      this.setData({ aiLoading: false, aiEnabled, interpretEnabled: kinds.includes('interpret'), clauseEnabled: kinds.includes('clause'),
        remaining: (ai.remaining || {}).interpret || 0, clauseRemaining: (ai.remaining || {}).clause || 0,
        aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '',
        disclosure: aiEnabled && disclosure.modelLabel && disclosure.filingNo ? `模型：${disclosure.modelLabel}｜备案号：${disclosure.filingNo}` : '' });
      if (aiEnabled) this.ensureRecent(epoch);
    }).catch(() => { if (this.visible && epoch === this.epoch) this.setData({ aiLoading: false }); });
  },
  ensureRecent(epoch) {
    if (this.recentEpoch !== epoch) { this.recentEpoch = epoch; this.loadRecent(epoch); }
    else this.updateRecent();
  },
  updateRecent() {
    const kinds = (this.data.enabled ? ['compare', 'track'] : []).concat(this.kinds || []);
    const recent = (this.rawRecent || []).filter((task) => kinds.includes(task.kind)).slice()
      .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt) || b.id - a.id).slice(0, 3).map(taskView);
    this.setData({ recent });
  },
  loadRecent(epoch = this.epoch) {
    this.setData({ recentLoading: true, recentError: '' });
    api.advisorTasks().then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      this.rawRecent = res.items || []; this.updateRecent(); this.setData({ recentLoading: false });
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ recentLoading: false, recentError: err.message }); });
  },
  retryRecent() { this.loadRecent(); },
  buy() { wx.navigateTo({ url: '/pages/pass/index' }); },
  form(e) { if (this.data.enabled) wx.navigateTo({ url: `/pages/advisor-form/index?kind=${e.currentTarget.dataset.kind}` }); },
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
    const id = e && e.currentTarget && e.currentTarget.dataset.id;
    const task = id != null && this.data.recent.find((item) => String(item.id) === String(id));
    if (id != null && !task) return;
    if (task && task.kind === 'clause' && this.data.clauseEnabled) {
      wx.navigateTo({ url: `/pages/advisor-clause/index?taskId=${encodeURIComponent(id)}` }); return;
    }
    if (task && task.kind === 'interpret' && this.data.interpretEnabled) {
      wx.navigateTo({ url: `/pages/advisor-interpret/index?taskId=${encodeURIComponent(id)}` }); return;
    }
    wx.navigateTo({ url: `/pages/advisor-tasks/index${id ? '?taskId=' + encodeURIComponent(id) : ''}` });
  },
});
