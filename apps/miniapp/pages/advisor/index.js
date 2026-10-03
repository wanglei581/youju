const api = require('../../utils/api.js');
const { taskView } = require('../../utils/advisor.js');
const context = require('../../utils/tool-context.js');

Page({
  data: { loading: true, error: '', enabled: false, skills: [], context: null, recent: [], recentLoading: true, recentError: '' },
  onLoad(query) { this.setData({ context: context.read(query) }); },
  onShow() { this.visible = true; this.load(); },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; },
  onUnload() { this.onHide(); },
  clearContext() { this.setData({ context: null }); },
  load() {
    const epoch = this.epoch = (this.epoch || 0) + 1;
    this.setData({ loading: true, error: '' });
    api.advisorStatus({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      if (res.enabled !== true) { this.setData({ loading: false, enabled: false, skills: [] }); return; }
      this.setData({ loading: false, enabled: true, skills: [
        { kind: 'compare', title: '名单比对', description: '在已收录的公示/公布名单里查找客户' },
        { kind: 'track', title: '在报项目跟进表', description: '按客户省份列出截止日期已核对、尚未截止的申报通知' },
      ].map((skill) => Object.assign(skill, { remaining: Math.max(0, res.quota[skill.kind].limit - res.quota[skill.kind].used), maxRows: res.quota[skill.kind].maxRows })) });
      if (res.enabled) this.loadRecent(epoch);
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ loading: false, enabled: false, error: err.status === 404 ? '工具暂未开放' : err.message }); });
  },
  loadRecent(epoch = this.epoch) {
    this.setData({ recentLoading: true, recentError: '' });
    api.advisorTasks().then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const recent = res.items.slice().sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt) || b.id - a.id).slice(0, 3).map(taskView);
      this.setData({ recentLoading: false, recent });
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ recentLoading: false, recentError: err.message }); });
  },
  retryRecent() { this.loadRecent(); },
  form(e) { wx.navigateTo({ url: `/pages/advisor-form/index?kind=${e.currentTarget.dataset.kind}` }); },
  tasks(e) {
    const id = e && e.currentTarget.dataset.id;
    wx.navigateTo({ url: `/pages/advisor-tasks/index${id ? '?taskId=' + encodeURIComponent(id) : ''}` });
  },
});
