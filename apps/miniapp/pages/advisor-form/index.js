const { loginOpen } = require('../../utils/consent.js');
const api = require('../../utils/api.js');
const { KINDS, pasteRows, consent } = require('../../utils/advisor.js');
const pay = require('../../utils/pay.js');

Page({
  data: { loginOpen: loginOpen(), kind: 'compare', loading: true, error: '', inputError: '', agreed: false, mode: 'paste', text: '', fileName: '', rowCount: 0, previewing: false, submitting: false, remaining: 0, maxRows: 50, passActive: false, quotaReady: false, statusLine: '', limitHint: null },
  onLoad(query) {
    if (!this.data.loginOpen) return;
    const kind = query.kind === 'track' ? 'track' : 'compare';
    this.setData({ kind, title: KINDS[kind] });
    wx.setNavigationBarTitle({ title: KINDS[kind] });
    this.refreshKey();
  },
  refreshKey() {
    const content = JSON.stringify({ kind: this.data.kind, input: this.data.mode === 'file' ? this.file || null : this.data.text });
    if (content === this.keyContent) return;
    this.keyContent = content;
    this.keySerial = (this.keySerial || 0) + 1;
    // 内容改变才换编号；同一内容超时重试仍沿用原编号。
    this.key = `advisor-${Date.now()}-${Math.random().toString(36).slice(2)}-${this.keySerial}`;
  },
  onShow() { this.load(); },
  onUnload() { this.productSerial = (this.productSerial || 0) + 1; },
  load() {
    if (!this.data.loginOpen) return;
    this.setData({ loading: true, error: '' });
    this.loadProducts();
    Promise.all([api.advisorStatus({ refresh: true }), consent()]).then(([res, agreed]) => {
      if (!res.enabled) throw new Error('工具暂未开放');
      const quota = res.quota[this.data.kind];
      this.setData({ loading: false, agreed, remaining: Math.max(0, quota.limit - quota.used), maxRows: quota.maxRows, quotaReady: true, passActive: !!(res.pass && res.pass.active), statusLine: pay.toolStatus(res, this.data.kind).line });
      this.renderLimit('');
    }).catch((err) => this.setData({ loading: false, error: err.message }));
  },
  terms() { wx.navigateTo({ url: '/pages/advisor-terms/index' }); },
  mode(e) {
    if (this.data.submitting || this.data.previewing) return;
    // 再点一次已经选中的那一种：不动，免得把填好的名单清空
    if (e.currentTarget.dataset.mode === this.data.mode) return;
    this.file = null;
    this.setData({ mode: e.currentTarget.dataset.mode, text: '', fileName: '', rowCount: 0, inputError: '' });
    this.refreshKey();
    this.renderLimit('');
  },
  input(e) {
    if (this.data.submitting) return;
    this.setData({ text: e.detail.value, inputError: '' });
    this.refreshKey();
    try { this.setData({ rowCount: pasteRows(this.data.kind, e.detail.value).length }); }
    catch (err) { this.setData({ rowCount: 0, inputError: err.message }); }
    this.renderLimit('');
  },
  choose() {
    if (this.data.previewing || this.data.submitting) return;
    if (!this.data.agreed) { this.terms(); return; }
    wx.chooseMessageFile({ count: 1, type: 'file', extension: ['xlsx', 'csv'], success: (res) => {
      const selected = res.tempFiles[0];
      if (!selected) return;
      this.file = null;
      this.setData({ rowCount: 0, fileName: '', inputError: '' });
      this.renderLimit('');
      if (!/\.(xlsx|csv)$/i.test(selected.name)) { this.setData({ inputError: '只支持 .xlsx 和 .csv 文件' }); return; }
      if (selected.size > 60 * 1024) { this.setData({ inputError: '文件不能超过 60 KB' }); return; }
      this.setData({ previewing: true, fileName: selected.name });
      wx.getFileSystemManager().readFile({ filePath: selected.path, encoding: 'base64', success: (file) => {
        this.file = { name: selected.name, content_base64: file.data };
        this.refreshKey();
        this.preview();
      }, fail: () => this.setData({ previewing: false, inputError: '文件没能读取，请重新选择' }) });
    }, fail: (err) => {
      if (!/cancel/.test(err.errMsg || '')) this.setData({ inputError: '文件没能选择，请重试' });
    } });
  },
  preview() {
    if (!this.file) { this.choose(); return; }
    this.setData({ previewing: true, inputError: '' });
    api.advisorPreview({ kind: this.data.kind, file: this.file }).then((res) => this.setData({ previewing: false, rowCount: res.rowCount }))
      .catch((err) => this.setData({ previewing: false, rowCount: 0, agreed: err.status === 403 ? false : this.data.agreed, inputError: err.message }))
      .then(() => this.renderLimit());
  },
  submit() {
    if (this.data.submitting || this.data.previewing) return;
    if (!this.data.agreed) { this.terms(); return; }
    this.renderLimit();
    if (this.data.limitHint) return;
    if (!this.data.rowCount || this.data.rowCount > this.data.maxRows) { this.setData({ inputError: `请提交 1–${this.data.maxRows} 行客户` }); return; }
    let body;
    try { body = this.data.mode === 'file' ? { kind: this.data.kind, file: this.file } : { kind: this.data.kind, rows: pasteRows(this.data.kind, this.data.text) }; }
    catch (err) { this.setData({ inputError: err.message }); return; }
    this.refreshKey();
    this.setData({ submitting: true, inputError: '' });
    api.createAdvisorTask(body, this.key).then((task) => {
      wx.redirectTo({ url: `/pages/advisor-tasks/index?taskId=${encodeURIComponent(task.id)}`, fail: () => { this.setData({ submitting: false }); wx.showToast({ title: '已提交，请到我的任务查看', icon: 'none' }); } });
    }).catch((err) => {
      this.setData({ submitting: false, agreed: err.status === 403 ? false : this.data.agreed, inputError: ['FREE_ROW_LIMIT', 'FREE_MONTHLY_LIMIT'].includes(err.code) ? '' : err.message });
      if (['FREE_ROW_LIMIT', 'FREE_MONTHLY_LIMIT'].includes(err.code)) this.setData({ passActive: false, maxRows: 50, remaining: err.code === 'FREE_MONTHLY_LIMIT' ? 0 : this.data.remaining });
      this.renderLimit(err.code || '');
    });
  },
  buy() { wx.navigateTo({ url: '/pages/pass/index' }); },
  loadProducts() {
    if (typeof api.payProducts !== 'function') return;
    const serial = this.productSerial = (this.productSerial || 0) + 1;
    api.payProducts().then(pay.normalizeProducts, () => []).then((products) => {
      if (serial !== this.productSerial) return;
      this.product = products[0] || null;
      this.renderLimit();
    });
  },
  renderLimit(code = this.limitCode || '') {
    this.limitCode = code;
    this.setData({ limitHint: pay.limitView({ ...this.data, code, product: this.product }) });
  },
});
