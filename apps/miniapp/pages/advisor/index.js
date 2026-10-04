const api = require('../../utils/api.js');
const { taskView } = require('../../utils/advisor.js');
const context = require('../../utils/tool-context.js');

Page({
  data: { loading: true, error: '', enabled: false, aiEnabled: false, dataEnabled: false, remaining: 0, disclosure: '', skills: [], context: null, recent: [], recentLoading: true, recentError: '' },
  onLoad(query) { this.setData({ context: context.read(query) }); },
  onShow() { this.visible = true; this.load(); },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; },
  onUnload() { this.onHide(); },
  clearContext() { this.setData({ context: null }); },
  load() {
    const epoch = this.epoch = (this.epoch || 0) + 1;
    wx.setNavigationBarTitle({ title: '工具' });
    this.setData({ loading: true, error: '', aiEnabled: false, disclosure: '' });
    api.advisorCapabilities({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const dataEnabled = res.data_tools.enabled === true;
      const aiEnabled = res.ai_advisor.enabled === true;
      const disclosure = res.ai_advisor.disclosure || {};
      wx.setNavigationBarTitle({ title: aiEnabled ? 'AI 顾问' : '工具' });
      this.setData({ loading: false, enabled: dataEnabled || aiEnabled, dataEnabled, aiEnabled,
        remaining: (res.ai_advisor.remaining || {}).interpret || 0,
        disclosure: aiEnabled && disclosure.modelLabel && disclosure.filingNo ? `模型：${disclosure.modelLabel}｜备案号：${disclosure.filingNo}` : '',
        skills: dataEnabled ? [
          { kind: 'compare', title: '名单比对', description: '在已收录的公示/公布名单里查找客户' },
          { kind: 'track', title: '在报项目跟进表', description: '按客户省份列出截止日期已核对、尚未截止的申报通知' },
        ].filter((skill) => (res.data_tools.features || []).includes(skill.kind)).map((skill) => Object.assign(skill, {
          remaining: (res.data_tools.remaining || {})[skill.kind] || 0, maxRows: 50,
        })) : [] });
      if (dataEnabled || aiEnabled) this.loadRecent(epoch);
    }).catch(() => {
      if (this.visible && epoch === this.epoch) {
        wx.setNavigationBarTitle({ title: '工具' });
        this.setData({ loading: false, enabled: false, aiEnabled: false, dataEnabled: false, disclosure: '', skills: [] });
      }
    });
  },
  loadRecent(epoch = this.epoch) {
    this.setData({ recentLoading: true, recentError: '' });
    api.advisorTasks().then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const recent = res.items.filter((task) => task.kind !== 'interpret' || this.data.aiEnabled).slice().sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt) || b.id - a.id).slice(0, 3).map(taskView);
      this.setData({ recentLoading: false, recent });
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ recentLoading: false, recentError: err.message }); });
  },
  retryRecent() { this.loadRecent(); },
  interpret() {
    if (!this.data.aiEnabled || !this.data.context || !this.data.context.docId) return;
    const { docId, title } = this.data.context;
    wx.navigateTo({ url: `/pages/advisor-interpret/index?docId=${encodeURIComponent(docId)}&title=${encodeURIComponent(title)}` });
  },
  form(e) { wx.navigateTo({ url: `/pages/advisor-form/index?kind=${e.currentTarget.dataset.kind}` }); },
  tasks(e) {
    const id = e && e.currentTarget.dataset.id;
    const task = this.data.recent.find((item) => String(item.id) === String(id));
    if (this.data.aiEnabled && task && task.kind === 'interpret') {
      wx.navigateTo({ url: `/pages/advisor-interpret/index?taskId=${encodeURIComponent(id)}` }); return;
    }
    wx.navigateTo({ url: `/pages/advisor-tasks/index${id ? '?taskId=' + encodeURIComponent(id) : ''}` });
  },
});
