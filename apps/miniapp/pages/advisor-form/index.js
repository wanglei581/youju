const api = require('../../utils/api.js');
const { KINDS, pasteRows, consent } = require('../../utils/advisor.js');

Page({
  data: { kind: 'compare', loading: true, error: '', inputError: '', agreed: false, mode: 'paste', text: '', fileName: '', rowCount: 0, previewing: false, submitting: false, remaining: 0, maxRows: 50 },
  onLoad(query) {
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
  load() {
    this.setData({ loading: true, error: '' });
    Promise.all([api.advisorStatus(), consent()]).then(([res, agreed]) => {
      const quota = res.quota[this.data.kind];
      this.setData({ loading: false, agreed, remaining: Math.max(0, quota.limit - quota.used), maxRows: quota.maxRows });
    }).catch((err) => this.setData({ loading: false, error: err.message }));
  },
  terms() { wx.navigateTo({ url: '/pages/advisor-terms/index' }); },
  mode(e) {
    if (this.data.submitting || this.data.previewing) return;
    this.file = null;
    this.setData({ mode: e.currentTarget.dataset.mode, text: '', fileName: '', rowCount: 0, inputError: '' });
    this.refreshKey();
  },
  input(e) {
    if (this.data.submitting) return;
    this.setData({ text: e.detail.value, inputError: '' });
    this.refreshKey();
    try { this.setData({ rowCount: pasteRows(this.data.kind, e.detail.value).length }); }
    catch (err) { this.setData({ rowCount: 0, inputError: err.message }); }
  },
  choose() {
    if (this.data.previewing || this.data.submitting) return;
    if (!this.data.agreed) { this.terms(); return; }
    wx.chooseMessageFile({ count: 1, type: 'file', extension: ['xlsx', 'csv'], success: (res) => {
      const selected = res.tempFiles[0];
      if (!selected) return;
      this.file = null;
      this.setData({ rowCount: 0, fileName: '', inputError: '' });
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
      .catch((err) => this.setData({ previewing: false, rowCount: 0, agreed: err.status === 403 ? false : this.data.agreed, inputError: err.message }));
  },
  submit() {
    if (this.data.submitting || this.data.previewing) return;
    if (!this.data.agreed) { this.terms(); return; }
    if (!this.data.rowCount || this.data.rowCount > this.data.maxRows) { this.setData({ inputError: `请提交 1–${this.data.maxRows} 行客户` }); return; }
    let body;
    try { body = this.data.mode === 'file' ? { kind: this.data.kind, file: this.file } : { kind: this.data.kind, rows: pasteRows(this.data.kind, this.data.text) }; }
    catch (err) { this.setData({ inputError: err.message }); return; }
    this.refreshKey();
    this.setData({ submitting: true, inputError: '' });
    api.createAdvisorTask(body, this.key).then(() => {
      wx.redirectTo({ url: '/pages/advisor-tasks/index', fail: () => { this.setData({ submitting: false }); wx.showToast({ title: '已提交，请到我的任务查看', icon: 'none' }); } });
    }).catch((err) => this.setData({ submitting: false, agreed: err.status === 403 ? false : this.data.agreed, inputError: err.message }));
  },
});
