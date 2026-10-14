// 我的 AI 任务。只列当前账号有权限的政策解读、条款对照任务，
// 可查看结果、取消排队、删除；数据工具（Excel 结果）的后端没有上线，这里不处理。
const api = require('../../utils/api.js');
const { taskView, internalNotice, allowedKinds, aiTitle, gateState, AI_LABEL, FAB_TEXT, OFF_TEXT, GATE_TEXT } = require('../../utils/advisor.js');

Page({
  data: { aiLabel: AI_LABEL, fabText: FAB_TEXT, gate: 'off', gateText: OFF_TEXT, loading: true, error: '', actionError: '', items: [], busyId: null, aiEnabled: false, aiNotice: '', kinds: [] },
  onShow() { this.visible = true; this.delay = 3000; this.load(); },
  onHide() { this.stop(); },
  onUnload() { this.stop(); },
  stop() { this.visible = false; this.epoch = (this.epoch || 0) + 1; clearTimeout(this.timer); },
  allowed(items, kinds = this.data.kinds) { return (items || []).filter((task) => kinds.includes(task.kind)); },
  load() {
    clearTimeout(this.timer);
    const epoch = this.epoch = (this.epoch || 0) + 1;
    this.setData({ loading: true, error: '' });
    api.advisorCapabilities().then((caps) => {
      if (!this.visible || epoch !== this.epoch) return null;
      const ai = (caps && caps.ai_advisor) || {};
      const kinds = allowedKinds(ai);
      const aiEnabled = ai.enabled === true && kinds.length > 0;
      if (aiEnabled) wx.setNavigationBarTitle({ title: aiTitle('AI 任务') });
      // 权限关闭时先清空已有列表，避免请求失败后仍显示此前的 AI 任务。
      const gate = aiEnabled ? '' : gateState(caps);
      this.setData({ aiEnabled, kinds, gate, gateText: GATE_TEXT[gate] || '', aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '', items: this.allowed(this.data.items, kinds) });
      if (!aiEnabled) { this.setData({ loading: false, items: [] }); return null; }
      return api.advisorTasks().then((res) => {
        if (!this.visible || epoch !== this.epoch) return;
        this.setData({ loading: false, items: this.allowed(res.items).map(taskView) });
        this.schedule(epoch);
      });
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ loading: false, error: err.message }); });
  },
  // 正式版：没同意协议的先去同意，回来自动刷新；连不上的重试
  consent() {
    if (this.data.gate !== 'consent') return;
    require('../../utils/consent.js').ensureConsent().then((ok) => { if (ok && this.visible) this.load(); });
  },
  reload() { if (this.data.gate === 'offline') this.load(); },
  search() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/search/index' }); },
  schedule(epoch) {
    if (!this.visible || !this.data.items.some((item) => !item.terminal)) return;
    this.timer = setTimeout(() => this.poll(epoch), this.delay);
    this.delay = Math.min(15000, this.delay + 3000);
  },
  poll(epoch) {
    if (!this.visible || epoch !== this.epoch) return;
    const active = this.data.items.filter((item) => !item.terminal);
    Promise.all(active.map((item) => api.advisorTask(item.id))).then((tasks) => {
      if (!this.visible || epoch !== this.epoch) return;
      const updates = {}; tasks.forEach((task) => { updates[task.id] = taskView(task); });
      this.setData({ items: this.data.items.map((item) => updates[item.id] || item), error: '' });
      this.schedule(epoch);
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ error: err.message }); });
  },
  cancel(e) { this.mutate(e, api.cancelAdvisorTask, '取消排队任务', '取消后将退回今天的次数。'); },
  remove(e) { this.mutate(e, api.deleteAdvisorTask, '删除任务', '删除任务和结果后不能恢复。'); },
  mutate(e, call, title, content) {
    if (!this.data.aiEnabled || this.data.busyId !== null) return;
    const id = e.currentTarget.dataset.id;
    wx.showModal({ title, content, success: (res) => {
      if (!res.confirm || this.data.busyId !== null) return;
      this.setData({ busyId: id, actionError: '' });
      call(id).then(() => {
        this.setData({ busyId: null });
        if (this.visible) this.load();
      }).catch((err) => this.setData({ busyId: null, actionError: err.message }));
    } });
  },
  openTask(e) {
    const id = e.currentTarget.dataset.id;
    const task = this.data.items.find((item) => String(item.id) === String(id));
    if (!this.data.aiEnabled || !task || !this.data.kinds.includes(task.kind)) return;
    if (task.kind === 'clause') wx.navigateTo({ url: `/pages/advisor-clause/index?taskId=${encodeURIComponent(id)}` });
    else if (task.kind === 'interpret') wx.navigateTo({ url: `/pages/advisor-interpret/index?taskId=${encodeURIComponent(id)}` });
  },
});
