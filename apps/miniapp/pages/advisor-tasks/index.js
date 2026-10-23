const api = require('../../utils/api.js');
const access = require('../../utils/ai-access.js');
const { taskViews, blockDownload, internalNotice, allowedKinds, dataKinds, allKinds, gateState, AI_LABEL, OFF_TEXT, gateText } = require('../../utils/advisor.js');

// 记录是同一份（D-075），从哪进来先筛好哪一种（10/22 第 0 步）：AI 首页、解读页、对照页带 type=ai，
// 批量工具页带 type=tools，"我的 → 我的记录"不带（全部）；上面一排可以切回全部。
// AI 和批量工具都能用时才有这一排、每条才标类型；只有一种时照旧列全部（AI 关着时页面上没有 AI 字样）
const FILTERS = [{ key: 'all', label: '全部' }, { key: 'ai', label: 'AI' }, { key: 'tools', label: '批量工具' }];
const TYPES = ['ai', 'tools'];

// 每种各有几条：筛选那一排写条数，当前这一种一条都没有时按这一种写空的说明
function countsOf(items) {
  return { all: items.length, ai: items.filter((item) => item.recordType === 'ai').length, tools: items.filter((item) => item.recordType === 'tools').length };
}

Page({
  data: { toolsEnabled: false, downloadingId: null, aiLabel: AI_LABEL, gate: 'off', gateText: OFF_TEXT, loading: true, error: '', actionError: '', items: [], busyId: null, aiEnabled: false, dataEnabled: false, aiNotice: '', kinds: [],
    filters: FILTERS, filter: 'all', mixed: false, current: 'all', counts: { all: 0, ai: 0, tools: 0 }, focusId: '' },
  // 带了 taskId（批量工具页点了某一条、提交名单后跳过来）：把那一条标出来
  onLoad(query = {}) { this.setData({ filter: TYPES.includes(query.type) ? query.type : 'all', focusId: query.taskId ? String(query.taskId) : '' }); },
  // 每次显示都重新问能力接口：在途的、缓存的答复都不用（第 23 轮）
  onShow() { this.dead = false; this.visible = true; this.delay = 3000; if (api.forgetCapabilities) api.forgetCapabilities(); this.load(); },
  onHide() { this.stop(); },
  onUnload() { this.dead = true; this.stop(); },
  stop() { this.visible = false; this.epoch = (this.epoch || 0) + 1; clearTimeout(this.timer); },
  allowed(items, kinds = this.data.kinds) { return (items || []).filter((task) => kinds.includes(task.kind)); },
  // 列表变了都经过这里：顺带算每种几条
  setItems(items, extra = {}) { this.setData(Object.assign({ items, counts: countsOf(items) }, extra)); },
  load() {
    clearTimeout(this.timer);
    const epoch = this.epoch = (this.epoch || 0) + 1;
    this.setData({ loading: true, error: '' });
    api.advisorCapabilities({ refresh: true }).then((caps) => {
      if (!this.visible || epoch !== this.epoch) return null;
      const ai = (caps && caps.ai_advisor) || {};
      const kinds = allKinds(caps);
      const aiEnabled = allowedKinds(ai).length > 0;
      const dataEnabled = dataKinds(caps && caps.data_tools).length > 0;
      const toolsEnabled = kinds.length > 0;
      const mixed = aiEnabled && dataEnabled;
      wx.setNavigationBarTitle({ title: '我的记录' });
      // 权限关闭时先清空已有列表，避免请求失败后仍显示此前的 AI 任务。
      const gate = toolsEnabled ? '' : (caps.reason === 'consent' || caps.reason === 'offline' ? caps.reason : gateState(caps));
      this.setItems(this.allowed(this.data.items, kinds), { toolsEnabled, aiEnabled, dataEnabled, mixed, current: mixed ? this.data.filter : 'all', kinds, gate, gateText: gateText(gate, caps), aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '' });
      if (!toolsEnabled) { this.setItems([], { loading: false }); return null; }
      return api.advisorTasks().then((res) => {
        if (!this.visible || epoch !== this.epoch) return;
        this.setItems(this.allowed(taskViews(res.items || [])), { loading: false });
        this.schedule(epoch);
      });
    }).catch((err) => {
      if (!this.visible || epoch !== this.epoch) return;
      if ([401, 403, 404].includes(err.status)) this.setItems([], { loading: false, error: err.message, toolsEnabled: false });
      else this.setData({ loading: false, error: err.message });
    });
  },
  // 切换上面那一排：只换显示哪些，不重新请求
  pick(e) {
    const key = e.currentTarget.dataset.key;
    if (!this.data.mixed || !FILTERS.some((item) => item.key === key)) return;
    this.setData({ filter: key, current: key });
  },
  // 正式版：没同意协议的先去同意，回来自动刷新；连不上的重试
  consent() {
    if (this.data.gate !== 'consent') return;
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
  schedule(epoch) {
    if (!this.visible || !this.data.items.some((item) => !item.terminal)) return;
    this.timer = setTimeout(() => this.poll(epoch), this.delay);
    this.delay = Math.min(15000, this.delay + 3000);
  },
  poll(epoch) {
    if (!this.visible || epoch !== this.epoch) return;
    api.advisorTasks().then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      this.setItems(this.allowed(taskViews(res.items || [])), { error: '' });
      this.schedule(epoch);
    }).catch((err) => {
      if (!this.visible || epoch !== this.epoch) return;
      if ([401, 403, 404].includes(err.status)) this.setItems([], { toolsEnabled: false });
      this.setData({ error: err.message });
    });
  },
  // 空的时候按入口指回去：AI 的回底栏「AI」（AI 首页），批量工具的回批量工具页（从那一页来的就返回，不再叠一层）
  aiHome() { if (this.data.aiEnabled) wx.switchTab({ url: '/pages/ai/index' }); },
  tools() {
    const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : [];
    const prev = pages.length > 1 ? pages[pages.length - 2] : null;
    if (prev && prev.route === 'pages/advisor/index') wx.navigateBack();
    else wx.navigateTo({ url: '/pages/advisor/index' });
  },
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
        if (err.status === 410) this.setItems(this.data.items.map((item) => item.id === task.id ? { ...item, downloadable: false, resultNote: err.message } : item));
        if ([401, 403, 404].includes(err.status)) this.setItems(this.data.items.filter((item) => item.id !== task.id));
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
