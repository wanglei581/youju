// AI 内测：政策解读与材料清单（只上传体验版、不合并）。直达也要 capabilities 开放才显示。
const api = require('../../utils/api.js');
const { createSession } = require('../../utils/ai-interpret.js');
const { internalNotice, allowedKinds, aiTitle, AI_LABEL } = require('../../utils/advisor.js');
const context = require('../../utils/tool-context.js');

Page({
  data: { aiLabel: AI_LABEL, loading: true, aiEnabled: false, aiNotice: '', phase: '', error: '', title: '', docId: '', taskId: '', checked: false, needsConsent: true, terms: [], result: null, canRetry: false },
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
      if (aiEnabled) wx.setNavigationBarTitle({ title: aiTitle('政策解读') });
      this.setData({ loading: false, aiEnabled, aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '' });
      if (aiEnabled) this.session.show();
    }).catch(() => {
      if (this.visible && epoch === this.epoch) this.setData({ loading: false, aiEnabled: false });
    });
  },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; this.session.hide(); },
  onUnload() { this.onHide(); },
  check(e) { this.session.check((e.detail.value || []).includes('agree')); },
  agree() { if (this.data.aiEnabled) this.session.agree(); },
  retry() { if (this.data.aiEnabled && this.data.canRetry) this.session.retry(); },
  original() { if (this.data.aiEnabled && this.data.docId) wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(this.data.docId)}` }); },
  feedback() {
    if (this.data.aiEnabled && this.data.docId) wx.navigateTo({ url: `/pages/feedback/index?id=${encodeURIComponent(this.data.docId)}&title=${encodeURIComponent(this.data.title)}` });
  },
  tasks() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/advisor-tasks/index' }); },
});
