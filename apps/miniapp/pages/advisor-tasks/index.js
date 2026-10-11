const { loginOpen } = require('../../utils/consent.js');
const api = require('../../utils/api.js');
const { taskView, openResult, internalNotice, allowedKinds } = require('../../utils/advisor.js');

Page({
  data: { loginOpen: loginOpen(), loading: true, error: '', actionError: '', items: [], busyId: null, singleTask: false, demo: false, aiEnabled: false, aiNotice: '', kinds: [] },
  onLoad(query = {}) {
    if (!this.data.loginOpen) return;
    this.taskId = query.taskId || null;
    this.setData({ singleTask: !!this.taskId });
    if (this.taskId) wx.setNavigationBarTitle({ title: '任务详情' });
  },
  onShow() { this.visible = true; this.delay = 3000; this.setData({ demo: !api.isLive() }); this.load(); },
  onHide() { this.stop(); },
  onUnload() { this.stop(); },
  stop() { this.visible = false; this.epoch = (this.epoch || 0) + 1; clearTimeout(this.timer); this.timer = null; },
  load() {
    if (!this.data.loginOpen) return;
    clearTimeout(this.timer); this.timer = null;
    const epoch = this.epoch = (this.epoch || 0) + 1;
    this.setData({ loading: true, error: '' });
    this.rawItems = [];
    this.setData({ aiEnabled: false, aiNotice: '', kinds: [], items: [] });
    api.advisorCapabilities({ refresh: true }).then((caps) => {
      if (!this.visible || epoch !== this.epoch) return;
      const ai = (caps && caps.ai_advisor) || {}; const kinds = allowedKinds(ai);
      const aiEnabled = ai.enabled === true && kinds.length > 0;
      this.setData({ aiEnabled, kinds, aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '' });
      this.updateItems(); this.schedule(epoch);
    }).catch(() => {});
    const request = this.taskId ? api.advisorTask(this.taskId).then((task) => ({ items: [task] })) : api.advisorTasks();
    request.then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      this.rawItems = res.items || [];
      this.updateItems(); this.setData({ loading: false });
      this.schedule(epoch);
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ loading: false, error: err.message }); });
  },
  allowed(items, kinds = this.data.kinds) {
    return (items || []).filter((task) => ['compare', 'track'].includes(task.kind) || kinds.includes(task.kind));
  },
  updateItems() { this.setData({ items: this.allowed(this.rawItems).map(taskView) }); },
  schedule(epoch) {
    if (this.timer != null) return;
    if (!this.visible || !this.data.items.some((item) => !item.terminal)) return;
    this.timer = setTimeout(() => { this.timer = null; this.poll(epoch); }, this.delay);
    this.delay = Math.min(15000, this.delay + 3000);
  },
  poll(epoch) {
    if (!this.visible || epoch !== this.epoch) return;
    const active = this.data.items.filter((item) => !item.terminal);
    Promise.all(active.map((item) => api.advisorTask(item.id))).then((tasks) => {
      if (!this.visible || epoch !== this.epoch) return;
      const updates = {}; tasks.forEach((task) => { updates[task.id] = taskView(task); });
      this.rawItems = this.rawItems.map((item) => updates[item.id] || item);
      this.updateItems(); this.setData({ error: '' });
      this.schedule(epoch);
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ error: err.message }); });
  },
  form() { wx.navigateTo({ url: '/pages/advisor/index' }); },
  lists() { wx.navigateTo({ url: '/pages/lists/index' }); },
  radar() { wx.navigateTo({ url: '/pages/report/index' }); },
  allTasks() { wx.redirectTo({ url: '/pages/advisor-tasks/index' }); },
  cancel(e) {
    const task = this.data.items.find((item) => String(item.id) === String(e.currentTarget.dataset.id));
    this.mutate(e, api.cancelAdvisorTask, '取消排队任务', task && task.kind === 'compare' ? '取消后将退回任务提交月份的次数。' : task && task.kind === 'track' ? '取消后将退回任务提交当天的次数。' : '取消后将退回今天的次数。');
  },
  remove(e) {
    const task = this.data.items.find((item) => String(item.id) === String(e.currentTarget.dataset.id));
    this.mutate(e, api.deleteAdvisorTask, '删除任务', task && ['interpret', 'clause'].includes(task.kind) ? '删除任务和结果后不能恢复。' : '删除任务后不能恢复；已导出的本机文件可在“文件”页清空。');
  },
  mutate(e, call, title, content) {
    if (this.data.busyId !== null) return;
    const id = e.currentTarget.dataset.id;
    if (!this.data.items.some((task) => String(task.id) === String(id))) return;
    wx.showModal({ title, content, success: (res) => {
      if (!res.confirm || this.data.busyId !== null) return;
      this.setData({ busyId: id, actionError: '' });
      call(id).then(() => {
        this.setData({ busyId: null });
        if (call === api.deleteAdvisorTask && this.taskId) {
          this.taskId = null;
          this.setData({ singleTask: false });
          wx.setNavigationBarTitle({ title: '我的任务' });
        }
        if (this.visible) this.load();
      })
        .catch((err) => this.setData({ busyId: null, actionError: err.message }));
    } });
  },
  open(e) {
    if (this.data.busyId !== null) return;
    const id = e.currentTarget.dataset.id;
    const task = this.data.items.find((item) => String(item.id) === String(id));
    if (!task) return;
    if (['interpret', 'clause'].includes(task.kind)) { this.openTask(e); return; }
    this.setData({ busyId: id, actionError: '' });
    openResult(id, { task: this.data.items.find((item) => String(item.id) === String(id)) }).then(() => this.setData({ busyId: null }))
      .catch((err) => this.setData({ busyId: null, actionError: err.message }));
  },
  openTask(e) {
    if (this.data.busyId !== null) return;
    const id = e.currentTarget.dataset.id;
    const task = this.data.items.find((item) => String(item.id) === String(id));
    if (!this.data.aiEnabled || !task || !this.data.kinds.includes(task.kind)) return;
    if (task.kind === 'clause') wx.navigateTo({ url: `/pages/advisor-clause/index?taskId=${encodeURIComponent(id)}` });
    else if (task.kind === 'interpret') wx.navigateTo({ url: `/pages/advisor-interpret/index?taskId=${encodeURIComponent(id)}` });
  },
});
