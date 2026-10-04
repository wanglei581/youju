const api = require('../../utils/api.js');
const clause = require('../../utils/ai-clause.js');
const context = require('../../utils/tool-context.js');
const { todayISO } = require('../../utils/format.js');

Page({
  data: { loading: true, aiEnabled: false, phase: 'form', error: '', title: '', docId: '', taskId: '',
    checked: false, needsConsent: false, terms: [], result: null, canRetry: false,
    form: clause.emptyForm(), errors: {}, provinceIndex: 0, provinces: ['暂不填写'].concat(clause.PROVINCES),
    qualificationOptions: [], today: '' },
  onLoad(query = {}) {
    const doc = context.read(query) || {};
    this.setData({ docId: doc.docId || '', title: doc.title || '', taskId: query.taskId || '',
      today: todayISO(), qualificationOptions: clause.QUALIFICATIONS.map((name) => ({ name, checked: false })) });
    if (query.taskId) this.startSession({ taskId: query.taskId });
  },
  startSession(options = {}) {
    this.session = clause.createSession({ api, storage: wx, docId: this.data.docId, title: this.data.title, ...options,
      update: (value) => { if (this.visible && this.data.aiEnabled) this.setData(value); } });
  },
  onShow() {
    this.visible = true;
    const epoch = this.epoch = (this.epoch || 0) + 1;
    wx.setNavigationBarTitle({ title: '工具' });
    this.setData({ loading: true, aiEnabled: false, today: todayISO() });
    api.advisorCapabilities({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      const ai = res.ai_advisor || {};
      const aiEnabled = ai.enabled === true && (ai.features || []).includes('clause');
      wx.setNavigationBarTitle({ title: aiEnabled ? '条款对照' : '工具' });
      this.setData({ loading: false, aiEnabled });
      if (aiEnabled && this.session) this.session.show();
    }).catch(() => {
      if (this.visible && epoch === this.epoch) this.setData({ loading: false, aiEnabled: false });
    });
  },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; if (this.session) this.session.hide(); },
  onUnload() { this.onHide(); },
  input(e) {
    const field = e.currentTarget.dataset.field;
    if (!Object.prototype.hasOwnProperty.call(this.data.form, field) || field === 'qualification') return;
    this.setData({ form: { ...this.data.form, [field]: e.detail.value }, errors: {} });
  },
  region(e) {
    const provinceIndex = Number(e.detail.value);
    this.setData({ provinceIndex, form: { ...this.data.form, region: clause.PROVINCES[provinceIndex - 1] || '' }, errors: {} });
  },
  clear(e) { this.input({ currentTarget: e.currentTarget, detail: { value: '' } }); },
  qualifications(e) {
    const names = e.detail.value || [];
    const qualification = clause.QUALIFICATIONS.filter((name) => names.includes(name)).map((name) =>
      this.data.form.qualification.find((item) => item.name === name) || { name, valid_until: null });
    this.setData({ form: { ...this.data.form, qualification }, errors: {},
      qualificationOptions: clause.QUALIFICATIONS.map((name) => ({ name, checked: names.includes(name) })) });
  },
  validity(e) {
    const index = Number(e.currentTarget.dataset.index);
    this.setData({ form: { ...this.data.form, qualification: this.data.form.qualification.map((item, i) =>
      i === index ? { ...item, valid_until: e.detail.value || null } : item) }, errors: {} });
  },
  clearValidity(e) { this.validity({ currentTarget: e.currentTarget, detail: { value: '' } }); },
  submit() {
    if (!this.data.aiEnabled || this.data.phase !== 'form' || !this.visible) return;
    const today = todayISO(); const errors = clause.validateForm(this.data.form, today);
    this.setData({ errors, today });
    if (Object.keys(errors).length) return;
    this.startSession({ company: clause.toCompany(this.data.form, today) });
    this.session.show();
  },
  check(e) { if (this.session) this.session.check((e.detail.value || []).includes('agree')); },
  agree() { if (this.data.aiEnabled && this.session) this.session.agree(); },
  retry() {
    if (!this.data.aiEnabled || !this.data.canRetry) return;
    this.session.hide(); this.session = null;
    this.setData({ phase: 'form', needsConsent: false, taskId: '', error: '', canRetry: false, result: null });
  },
  original() { if (this.data.aiEnabled && this.data.docId) wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(this.data.docId)}` }); },
  feedback() {
    if (this.data.aiEnabled && this.data.docId) wx.navigateTo({ url: `/pages/feedback/index?id=${encodeURIComponent(this.data.docId)}&title=${encodeURIComponent(this.data.title)}` });
  },
  tasks() { wx.navigateTo({ url: '/pages/advisor-tasks/index' }); },
});
