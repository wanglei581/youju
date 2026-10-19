// 条款对照。直达也要 capabilities 开放且含 clause 才显示；正式版没同意协议时引导同意，连不上时给重试。
const api = require('../../utils/api.js');
const { regionSelection, regionValue } = require('../../utils/regions.js');
const clause = require('../../utils/ai-clause.js');
const { internalNotice, allowedKinds, aiTitle, gateState, AI_LABEL, OFF_TEXT, GATE_TEXT } = require('../../utils/advisor.js');
const context = require('../../utils/tool-context.js');
const { todayISO } = require('../../utils/format.js');

Page({
  data: { aiLabel: AI_LABEL, loading: true, outdated: false, aiEnabled: false, gate: 'off', gateText: OFF_TEXT, aiNotice: '', phase: 'form', progress: '', error: '', title: '', docId: '', taskId: '',
    checked: false, needsConsent: false, terms: [], result: null, canRetry: false, resumable: false, showTerms: false, showMore: false,
    form: clause.emptyForm(), errors: {}, provinceIndex: 0, provinces: ['暂不填写'].concat(clause.PROVINCES),
    qualificationOptions: [], today: '', cityRegion: [] },
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
      const aiEnabled = ai.enabled === true && allowedKinds(ai).includes('clause');
      if (aiEnabled) wx.setNavigationBarTitle({ title: aiTitle('条款对照') });
      const gate = aiEnabled ? '' : gateState(res);
      this.setData({ loading: false, aiEnabled, gate, gateText: GATE_TEXT[gate] || '', aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '' });
      if (aiEnabled && this.session) this.session.show();
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
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; if (this.session) this.session.hide(); },
  onUnload() { this.onHide(); },
  input(e) {
    const field = e.currentTarget.dataset.field;
    if (!Object.prototype.hasOwnProperty.call(this.data.form, field) || field === 'qualification') return;
    const form = { ...this.data.form, [field]: e.detail.value };
    // 清空城市时区县一起清掉：区县离不开所属城市
    if (field === 'city' && !e.detail.value) form.district = '';
    this.setData({ form, errors: {} });
  },
  // 换省份清掉城市和区县；省市区选择器重新打开时停在这个省
  region(e) {
    const provinceIndex = Number(e.detail.value);
    const region = clause.PROVINCES[provinceIndex - 1] || '';
    this.setData({ provinceIndex, cityRegion: regionValue(region), form: { ...this.data.form, region, city: '', district: '' }, errors: {} });
  },
  // 省份一栏选“全部”是暂不填写，城市一栏“全部”只填省份，区县一栏“全部”填到城市；直辖市未选区县只填省份
  chooseCity(e) {
    try {
      const selected = regionSelection(e.detail.value);
      this.setData({ cityRegion: e.detail.value, provinceIndex: clause.PROVINCES.indexOf(selected.province) + 1,
        form: { ...this.data.form, region: selected.province, city: selected.city, district: selected.district }, errors: {} });
    } catch (err) { this.setData({ errors: { city: err.message } }); }
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
  // 使用须知只有一个"我知道了，开始对照"按钮（10/17）
  start() { if (this.data.aiEnabled && this.session) this.session.start(); },
  toggleTerms() { this.setData({ showTerms: !this.data.showTerms }); },
  // 研发费用、已有资质默认收起；已经填了的不收起，免得看不到
  toggleMore() {
    const form = this.data.form;
    const filled = !!(form.rdAmount || form.rdYear || (form.qualification || []).length);
    this.setData({ showMore: filled ? true : !this.data.showMore });
  },
  retry() {
    if (!this.data.aiEnabled || !this.data.canRetry) return;
    // 上次是没连上：接着用原来的任务或请求编号，不用重新填写，也不会重复扣次数
    if (this.session && this.session.resumable()) { this.session.retry(); return; }
    this.session.hide(); this.session = null;
    this.setData({ phase: 'form', needsConsent: false, taskId: '', error: '', canRetry: false, result: null });
  },
  original() { if (this.data.aiEnabled && this.data.docId) wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(this.data.docId)}` }); },
  feedback() {
    if (this.data.aiEnabled && this.data.docId) wx.navigateTo({ url: `/pages/feedback/index?id=${encodeURIComponent(this.data.docId)}&title=${encodeURIComponent(this.data.title)}` });
  },
  tasks() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/advisor-tasks/index' }); },
  report() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/complaint/index' }); },
});
