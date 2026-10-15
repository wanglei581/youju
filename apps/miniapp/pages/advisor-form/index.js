const api = require('../../utils/api.js');
const idempotency = require('../../utils/data-idempotency.js');
const { DATA_KINDS, dataKinds } = require('../../utils/advisor.js');
const { pasteRows, toolConsent, quotaView } = require('../../utils/data-tools.js');

Page({
  data: { kind: 'compare', title: DATA_KINDS.compare, loading: true, error: '', gate: '', enabled: false, agreed: false,
    mode: 'paste', text: '', fileName: '', rowCount: 0, previewed: false, previewing: false, submitting: false, inputError: '', maxRows: 50, remaining: 0, passActive: false, statusLine: '', accepted: false },
  onLoad(query) {
    const kind = query.kind === 'track' ? 'track' : 'compare';
    this.setData({ kind, title: DATA_KINDS[kind] });
    wx.setNavigationBarTitle({ title: DATA_KINDS[kind] });
    this.refreshKey();
  },
  onShow() { this.visible = true; this.load(); },
  onHide() { this.visible = false; },
  onUnload() { this.visible = false; this.dead = true; this.file = null; this.serial = (this.serial || 0) + 1; },
  load() {
    const serial = this.serial = (this.serial || 0) + 1;
    this.previewEpoch = (this.previewEpoch || 0) + 1;
    this.setData({ loading: true, error: '', enabled: false, previewing: false, previewed: false });
    api.advisorCapabilities({ refresh: true }).then((caps) => {
      if (this.dead || serial !== this.serial) return;
      if (!dataKinds(caps.data_tools).includes(this.data.kind)) {
        this.setData({ loading: false, gate: caps.reason || 'off', error: caps.reason === 'consent' ? '请先阅读并同意用户协议和隐私保护指引' : caps.reason === 'offline' ? '暂时连不上服务器，请重试' : '这项工具暂时不可用，请稍后重试' });
        return;
      }
      return Promise.all([api.advisorStatus(), toolConsent({ identity: true })]).then(([status, agreement]) => {
        if (this.dead || serial !== this.serial) return;
        const agreed = agreement.agreed;
        if (this.taskOwner && this.taskOwner !== agreement.owner) {
          this.file = null;
          this.fileEpoch = (this.fileEpoch || 0) + 1;
          this.pendingFilePreview = false;
          this.setData({ text: '', fileName: '', rowCount: 0, submitting: false, accepted: false });
        }
        this.taskOwner = agreement.owner;
        if (this.taskOwner) idempotency.syncOwner(wx, this.taskOwner);
        this.setData({ ...quotaView(status, this.data.kind), loading: false, enabled: true, agreed, gate: '', error: '' });
        this.resumeFilePreview();
      });
    }).catch((err) => { if (!this.dead && serial === this.serial) this.setData({ loading: false, error: err.message, gate: err.status === 403 ? 'consent' : '' }); });
  },
  consent() { require('../../utils/consent.js').ensureConsent().then((ok) => { if (ok && !this.dead) this.load(); }); },
  terms() { wx.navigateTo({ url: '/pages/advisor-terms/index' }); },
  tasks() { wx.redirectTo({ url: '/pages/advisor-tasks/index' }); },
  refreshKey() {
    this.key = `data-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  },
  mode(e) {
    if (this.data.submitting || this.data.previewing || this.data.accepted) return;
    this.fileEpoch = (this.fileEpoch || 0) + 1;
    this.pendingFilePreview = false;
    this.file = null;
    this.setData({ mode: e.currentTarget.dataset.mode, text: '', fileName: '', rowCount: 0, previewed: false, inputError: '' });
    this.refreshKey();
  },
  input(e) {
    if (this.data.submitting || this.data.previewing || this.data.accepted) return;
    this.setData({ text: e.detail.value, previewed: false, inputError: '' });
    this.refreshKey();
    try { this.setData({ rowCount: pasteRows(this.data.kind, e.detail.value).length }); }
    catch (err) { this.setData({ rowCount: 0, inputError: err.message }); }
  },
  choose() {
    if (this.data.previewing || this.data.submitting || this.data.accepted || !this.data.enabled) return;
    if (!this.data.agreed) { this.terms(); return; }
    if (typeof wx.chooseMessageFile !== 'function') { this.setData({ inputError: '当前微信版本不支持选文件，请升级微信或使用粘贴文本' }); return; }
    const fileEpoch = this.fileEpoch = (this.fileEpoch || 0) + 1;
    wx.chooseMessageFile({ count: 1, type: 'file', extension: ['xlsx', 'csv'], success: (res) => {
      if (this.dead || fileEpoch !== this.fileEpoch) return;
      const selected = res.tempFiles && res.tempFiles[0];
      if (!selected) return;
      this.file = null;
      this.setData({ rowCount: 0, fileName: '', previewed: false, inputError: '' });
      if (!/\.(xlsx|csv)$/i.test(selected.name) || selected.name.length > 255) { this.setData({ inputError: '请选择名称不超过 255 字的 XLSX 或 CSV 文件' }); return; }
      if (!Number.isFinite(selected.size) || selected.size <= 0 || selected.size > 60 * 1024) { this.setData({ inputError: '请选择 60 KB 以内的非空文件' }); return; }
      this.setData({ previewing: true, fileName: selected.name });
      wx.getFileSystemManager().readFile({ filePath: selected.path, encoding: 'base64', success: (file) => {
        if (this.dead || fileEpoch !== this.fileEpoch) return;
        if (typeof file.data !== 'string' || file.data.length > 81920) { this.setData({ previewing: false, inputError: '文件不能超过 60 KB' }); return; }
        this.file = { name: selected.name, content_base64: file.data };
        this.refreshKey();
        this.setData({ previewing: false });
        this.pendingFilePreview = true;
        this.resumeFilePreview();
      }, fail: () => { if (!this.dead) this.setData({ previewing: false, inputError: '文件没能读取，请重新选择' }); } });
    }, fail: (err) => { if (!this.dead && !/cancel/i.test(err.errMsg || '')) this.setData({ inputError: '文件没能选择，请重试' }); } });
  },
  resumeFilePreview() {
    if (this.pendingFilePreview && this.data.enabled && this.data.agreed && !this.dead) {
      this.pendingFilePreview = false;
      this.preview();
    }
  },
  body() {
    if (this.data.mode === 'file') {
      if (!this.file) throw new Error('请先选择文件');
      if (this.data.rowCount > this.data.maxRows) throw new Error(`每次最多 ${this.data.maxRows} 家，请减少行数`);
      return { kind: this.data.kind, file: this.file };
    }
    const rows = pasteRows(this.data.kind, this.data.text);
    if (!rows.length) throw new Error('请先填写客户');
    if (rows.length > this.data.maxRows) throw new Error(`每次最多 ${this.data.maxRows} 家，请减少行数`);
    return { kind: this.data.kind, rows };
  },
  preview() {
    if (this.data.previewing || this.data.submitting || this.data.accepted || !this.data.enabled) return;
    if (!this.data.agreed) { this.terms(); return; }
    let body;
    try { body = this.body(); } catch (err) { this.setData({ inputError: err.message }); return; }
    const serial = this.serial;
    const epoch = this.previewEpoch = (this.previewEpoch || 0) + 1;
    this.setData({ previewing: true, previewed: false, inputError: '' });
    api.advisorPreview(body).then((res) => {
      if (this.dead || serial !== this.serial || epoch !== this.previewEpoch) return;
      if (!Number.isInteger(res.rowCount) || res.rowCount < 1) throw new Error('没有可提交的客户行，请检查内容');
      this.setData({ previewing: false, rowCount: res.rowCount, previewed: res.rowCount <= this.data.maxRows,
        inputError: res.rowCount > this.data.maxRows ? `每次最多 ${this.data.maxRows} 家，请减少行数` : '' });
    }).catch((err) => { if (!this.dead && serial === this.serial && epoch === this.previewEpoch) this.setData({ previewing: false, previewed: false, agreed: err.status === 403 ? false : this.data.agreed, inputError: err.message }); });
  },
  submit() {
    if (this.data.submitting || this.data.previewing || this.data.accepted || !this.data.enabled) return;
    if (!this.data.agreed) { this.terms(); return; }
    if (!this.data.passActive && this.data.remaining <= 0 && !this.data.previewed) { this.setData({ inputError: '本月免费次数已用完，下月可继续使用；不需要购买' }); return; }
    if (!this.data.previewed) { this.setData({ inputError: '请先预览并确认本次输入' }); return; }
    let body;
    try { body = this.body(); } catch (err) { this.setData({ inputError: err.message }); return; }
    let request;
    try { request = idempotency.claim(wx, this.taskOwner, body); this.key = request.key; }
    catch (err) { this.setData({ inputError: err.message }); return; }
    if (!request.existing && !this.data.passActive && this.data.remaining <= 0) {
      try { idempotency.confirm(wx, request); } catch (ignored) { /* 未发送，不自动重试 */ }
      this.setData({ inputError: '本月免费次数已用完，下月可继续使用；不需要购买' }); return;
    }
    this.setData({ submitting: true, inputError: '' });
    api.createAdvisorTask(body, this.key).then((task) => {
      try { idempotency.confirm(wx, request); } catch (err) { /* 已确认任务，保留原键也不会重复创建 */ }
      if (this.dead || this.taskOwner !== request.owner) return;
      this.file = null;
      this.setData({ submitting: false, accepted: true, text: '', fileName: '', rowCount: 0 });
      if (this.visible) wx.redirectTo({ url: `/pages/advisor-tasks/index?taskId=${encodeURIComponent(task.id)}`, fail: () => wx.showToast({ title: '已提交，请到任务中心查看', icon: 'none' }) });
    }).catch((err) => {
      if (!request.existing && err.status >= 400 && err.status < 500 && err.status !== 408) {
        try { idempotency.confirm(wx, request); } catch (ignored) { /* 存储失败保留旧键 */ }
      }
      if (this.dead || this.taskOwner !== request.owner) return;
      const change = { submitting: false, agreed: err.status === 403 ? false : this.data.agreed,
        inputError: (!err.status || [502, 503, 504].includes(err.status)) ? `${err.message}。提交状态尚未确认；可重试同一内容，或到任务中心查看是否已受理。` : err.message };
      if (err.code === 'FREE_MONTHLY_LIMIT') Object.assign(change, { remaining: 0, passActive: false, inputError: '本月免费次数已用完，下月可继续使用；不需要购买' });
      if (['FREE_ROW_LIMIT', 'PASS_ROW_LIMIT'].includes(err.code)) {
        const limit = err.detail && err.detail.maxRows;
        Object.assign(change, { previewed: false, ...(Number.isInteger(limit) && limit > 0 ? { maxRows: limit } : {}), ...(err.code === 'FREE_ROW_LIMIT' ? { passActive: false } : {}) });
      }
      this.setData(change);
    });
  },
});
