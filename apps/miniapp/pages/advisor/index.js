// AI 内测入口页（只上传体验版、不合并）：政策解读、条款对照和最近任务。
// 只对接线上已有的 /api/v1/advisor 接口；capabilities 没开放时只显示“暂时没有可用工具”。
const api = require('../../utils/api.js');
const { taskView, internalNotice, allowedKinds } = require('../../utils/advisor.js');
const context = require('../../utils/tool-context.js');

Page({
  data: { loading: true, aiEnabled: false, interpretEnabled: false, clauseEnabled: false, remaining: 0, clauseRemaining: 0,
    aiNotice: '', disclosure: '', context: null, recent: [], recentLoading: true, recentError: '' },
  onLoad(query) { this.setData({ context: context.read(query) }); },
  onShow() { this.visible = true; this.load(); },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; },
  onUnload() { this.onHide(); },
  clearContext() { this.setData({ context: null }); },
  load() {
    const epoch = this.epoch = (this.epoch || 0) + 1;
    wx.setNavigationBarTitle({ title: '工具' });
    this.setData({ loading: true, aiEnabled: false, interpretEnabled: false, clauseEnabled: false, disclosure: '', aiNotice: '' });
    api.advisorCapabilities({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const ai = res.ai_advisor || {};
      const kinds = allowedKinds(ai);
      const aiEnabled = ai.enabled === true && kinds.length > 0;
      const disclosure = ai.disclosure || {};
      if (aiEnabled) wx.setNavigationBarTitle({ title: 'AI 内部测试' });
      this.setData({ loading: false, aiEnabled,
        interpretEnabled: aiEnabled && kinds.includes('interpret'),
        clauseEnabled: aiEnabled && kinds.includes('clause'),
        remaining: (ai.remaining || {}).interpret || 0,
        clauseRemaining: (ai.remaining || {}).clause || 0,
        aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '',
        disclosure: aiEnabled && disclosure.modelLabel && disclosure.filingNo ? `模型：${disclosure.modelLabel}｜备案号：${disclosure.filingNo}` : '' });
      this.kinds = aiEnabled ? kinds : [];
      if (aiEnabled) this.loadRecent(epoch);
    }).catch(() => {
      if (this.visible && epoch === this.epoch) this.setData({ loading: false, aiEnabled: false, disclosure: '', aiNotice: '' });
    });
  },
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
    if (!this.data.aiEnabled) return;
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
