const api = require('../../utils/api.js');
const { taskViews, blockDownload, internalNotice, allowedKinds, allKinds, aiTitle, gateState, AI_LABEL, FAB_TEXT, OFF_TEXT, GATE_TEXT } = require('../../utils/advisor.js');

Page({
  data: { toolsEnabled: false, downloadingId: null, aiLabel: AI_LABEL, fabText: FAB_TEXT, gate: 'off', gateText: OFF_TEXT, loading: true, error: '', actionError: '', items: [], busyId: null, aiEnabled: false, aiNotice: '', kinds: [] },
  onShow() { this.dead = false; this.visible = true; this.delay = 3000; this.load(); },
  onHide() { this.stop(); },
  onUnload() { this.dead = true; this.stop(); },
  stop() { this.visible = false; this.epoch = (this.epoch || 0) + 1; clearTimeout(this.timer); },
  allowed(items, kinds = this.data.kinds) { return (items || []).filter((task) => kinds.includes(task.kind)); },
  load() {
    clearTimeout(this.timer);
    const epoch = this.epoch = (this.epoch || 0) + 1;
    this.setData({ loading: true, error: '' });
    api.advisorCapabilities({ refresh: true }).then((caps) => {
      if (!this.visible || epoch !== this.epoch) return null;
      const ai = (caps && caps.ai_advisor) || {};
      const kinds = allKinds(caps);
      const aiEnabled = allowedKinds(ai).length > 0;
      const toolsEnabled = kinds.length > 0;
      wx.setNavigationBarTitle({ title: '任务中心' });
      // 权限关闭时先清空已有列表，避免请求失败后仍显示此前的 AI 任务。
      const gate = toolsEnabled ? '' : (caps.reason === 'consent' || caps.reason === 'offline' ? caps.reason : gateState(caps));
      this.setData({ toolsEnabled, aiEnabled, kinds, gate, gateText: GATE_TEXT[gate] || '', aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '', items: this.allowed(this.data.items, kinds) });
      if (!toolsEnabled) { this.setData({ loading: false, items: [] }); return null; }
      return api.advisorTasks().then((res) => {
        if (!this.visible || epoch !== this.epoch) return;
        this.setData({ loading: false, items: this.allowed(taskViews(res.items || [])) });
        this.schedule(epoch);
      });
    }).catch((err) => {
      if (!this.visible || epoch !== this.epoch) return;
      this.setData({ loading: false, error: err.message, ...([401, 403, 404].includes(err.status) ? { items: [], toolsEnabled: false } : {}) });
    });
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
    api.advisorTasks().then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      this.setData({ items: this.allowed(taskViews(res.items || [])), error: '' });
      this.schedule(epoch);
    }).catch((err) => {
      if (!this.visible || epoch !== this.epoch) return;
      if ([401, 403, 404].includes(err.status)) this.setData({ items: [], toolsEnabled: false });
      this.setData({ error: err.message });
    });
  },
  tools() { wx.navigateTo({ url: '/pages/advisor/index' }); },
  again(e) {
    const kind = e.currentTarget.dataset.kind;
    if (this.data.kinds.includes(kind) && ['compare', 'track'].includes(kind)) wx.navigateTo({ url: `/pages/advisor-form/index?kind=${kind}` });
  },
  download(e) {
    const id = e.currentTarget.dataset.id;
    const task = this.data.items.find((item) => String(item.id) === String(id));
    if (!this.data.toolsEnabled || !task || !task.downloadable || this.data.busyId !== null) return;
    const epoch = this.epoch;
    this.setData({ busyId: id, downloadingId: id, actionError: '' });
    require('../../utils/data-tools.js').openResult(id, { task, current: () => this.visible && epoch === this.epoch })
      .catch((err) => {
        if (err.status === 410) blockDownload(task, err.message);
        if (!this.visible || epoch !== this.epoch || err.cancelled) return;
        if (err.status === 410) this.setData({ items: this.data.items.map((item) => item.id === task.id ? { ...item, downloadable: false, resultNote: err.message } : item) });
        if ([401, 403, 404].includes(err.status)) this.setData({ items: this.data.items.filter((item) => item.id !== task.id) });
        this.setData({ actionError: err.message });
      }).then(() => { if (!this.dead) this.setData({ busyId: null, downloadingId: null }); });
  },
  cancel(e) { this.mutate(e, api.cancelAdvisorTask, '取消排队任务', '取消排队任务后退回本次占用的额度；已经开始处理的任务不能取消。'); },
  remove(e) { this.mutate(e, api.deleteAdvisorTask, '删除任务', '删除任务和结果后不能恢复。'); },
  mutate(e, call, title, content) {
    if (!this.data.toolsEnabled || this.data.busyId !== null) return;
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
    if (!this.data.toolsEnabled || !task || !this.data.kinds.includes(task.kind)) return;
    if (task.kind === 'clause') wx.navigateTo({ url: `/pages/advisor-clause/index?taskId=${encodeURIComponent(id)}` });
    else if (task.kind === 'interpret') wx.navigateTo({ url: `/pages/advisor-interpret/index?taskId=${encodeURIComponent(id)}` });
  },
});
