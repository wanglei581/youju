const api = require('../../utils/api.js');
const { taskView, openResult } = require('../../utils/advisor.js');

Page({
  data: { loading: true, error: '', actionError: '', items: [], busyId: null },
  onShow() { this.visible = true; this.delay = 3000; this.load(); },
  onHide() { this.stop(); },
  onUnload() { this.stop(); },
  stop() { this.visible = false; this.epoch = (this.epoch || 0) + 1; clearTimeout(this.timer); },
  load() {
    clearTimeout(this.timer);
    const epoch = this.epoch = (this.epoch || 0) + 1;
    this.setData({ loading: true, error: '' });
    api.advisorTasks().then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      this.setData({ loading: false, items: res.items.map(taskView) });
      this.schedule(epoch);
    }).catch((err) => { if (this.visible && epoch === this.epoch) this.setData({ loading: false, error: err.message }); });
  },
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
  form() { wx.navigateTo({ url: '/pages/advisor/index' }); },
  cancel(e) { this.mutate(e, api.cancelAdvisorTask, '取消排队任务', '取消后将退回今天的次数。'); },
  remove(e) { this.mutate(e, api.deleteAdvisorTask, '删除任务', '删除任务和结果文件后不能恢复。'); },
  mutate(e, call, title, content) {
    if (this.data.busyId !== null) return;
    const id = e.currentTarget.dataset.id;
    wx.showModal({ title, content, success: (res) => {
      if (!res.confirm || this.data.busyId !== null) return;
      this.setData({ busyId: id, actionError: '' });
      call(id).then(() => { this.setData({ busyId: null }); if (this.visible) this.load(); })
        .catch((err) => this.setData({ busyId: null, actionError: err.message }));
    } });
  },
  open(e) {
    if (this.data.busyId !== null) return;
    const id = e.currentTarget.dataset.id;
    this.setData({ busyId: id, actionError: '' });
    openResult(id).then(() => this.setData({ busyId: null }))
      .catch((err) => this.setData({ busyId: null, actionError: err.message }));
  },
});
