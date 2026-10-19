// AI 解读（政策解读与材料清单）。直达也要 capabilities 开放才显示；正式版没同意协议时引导同意，连不上时给重试。
const api = require('../../utils/api.js');
const { createSession } = require('../../utils/ai-interpret.js');
const { internalNotice, allowedKinds, aiTitle, gateState, AI_LABEL, OFF_TEXT, GATE_TEXT } = require('../../utils/advisor.js');
const context = require('../../utils/tool-context.js');

Page({
  data: { aiLabel: AI_LABEL, loading: true, outdated: false, aiEnabled: false, gate: 'off', gateText: OFF_TEXT, aiNotice: '', phase: '', progress: '', error: '', title: '', docId: '', taskId: '', checked: false, needsConsent: true, terms: [], result: null, canRetry: false, showTerms: false, clauseEnabled: false, resumable: false },
  onLoad(query = {}) {
    const doc = context.read(query) || {};
    this.session = createSession({ api, storage: wx, docId: doc.docId, title: doc.title, taskId: query.taskId,
      update: (value) => { if (this.visible && this.data.aiEnabled) this.setData(value); } });
  },
  onShow() {
    this.visible = true;
    const epoch = this.epoch = (this.epoch || 0) + 1;
    wx.setNavigationBarTitle({ title: '工具' });
    this.setData({ loading: true, aiEnabled: false });
    api.advisorCapabilities({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const ai = res.ai_advisor || {};
      const aiEnabled = ai.enabled === true && allowedKinds(ai).includes('interpret');
      if (aiEnabled) wx.setNavigationBarTitle({ title: aiTitle('AI 解读') });
      const gate = aiEnabled ? '' : gateState(res);
      this.setData({ loading: false, aiEnabled, gate, gateText: GATE_TEXT[gate] || '', aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '',
        clauseEnabled: aiEnabled && allowedKinds(ai).includes('clause') });
      if (aiEnabled) this.session.show();
    }).catch(() => {
      if (this.visible && epoch === this.epoch) this.setData({ loading: false, aiEnabled: false, gate: 'off', gateText: OFF_TEXT });
    });
  },
  // 正式版：没同意协议的先去同意，回来自动刷新；连不上的重试
  consent() {
    if (this.data.gate !== 'consent') return;
    require('../../utils/consent.js').ensureConsent().then((ok) => { if (ok && this.visible) this.onShow(); });
  },
  reload() { if (this.data.gate === 'offline') this.onShow(); },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; this.session.hide(); },
  onUnload() { this.onHide(); },
  check(e) { this.session.check((e.detail.value || []).includes('agree')); },
  agree() { if (this.data.aiEnabled) this.session.agree(); },
  // 使用须知只有一个"我知道了，开始解读"按钮（10/17）
  start() { if (this.data.aiEnabled) this.session.start(); },
  toggleTerms() { this.setData({ showTerms: !this.data.showTerms }); },
  // 解读结果下面的可选下一步：和我的企业对一下
  clause() {
    if (this.data.aiEnabled && this.data.clauseEnabled && this.data.docId) {
      wx.navigateTo({ url: context.clauseUrl({ docId: this.data.docId, docTitle: this.data.title }) });
    }
  },
  retry() { if (this.data.aiEnabled && this.data.canRetry) this.session.retry(); },
  original() { if (this.data.aiEnabled && this.data.docId) wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(this.data.docId)}` }); },
  feedback() {
    if (this.data.aiEnabled && this.data.docId) wx.navigateTo({ url: `/pages/feedback/index?id=${encodeURIComponent(this.data.docId)}&title=${encodeURIComponent(this.data.title)}` });
  },
  tasks() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/advisor-tasks/index' }); },
  report() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/complaint/index' }); },
});
