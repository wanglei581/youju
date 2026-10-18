const api = require('../../utils/api.js');
const idempotency = require('../../utils/data-idempotency.js');
const { DATA_KINDS, dataKinds } = require('../../utils/advisor.js');
const { pasteRows, toolConsent, quotaView, quotaStale } = require('../../utils/data-tools.js');
const pay = require('../../utils/pay.js');

// 超出免费量时的说明：有加量体验资格时先推荐体验（免费，D-070；提示下面紧跟"了解加量体验"按钮）；
// 支付在售（offer 有值）时提示可以开通客户工作台，没开放时照旧写"不需要购买"（D-058、D-068）
function limitText(offer, trial) {
  if (trial) return '本月免费次数已用完，下月 1 日恢复；也可以开始 3 天加量体验（免费，每次 500 家以内）';
  return offer ? '本月免费次数已用完，下月 1 日恢复；也可以开通客户工作台 30 天（不限次数，每次 500 家以内）' : '本月免费次数已用完，下月可继续使用；不需要购买';
}

Page({
  data: { kind: 'compare', title: DATA_KINDS.compare, loading: true, error: '', gate: '', enabled: false, agreed: false,
    mode: 'paste', text: '', fileName: '', rowCount: 0, previewed: false, previewing: false, submitting: false, inputError: '', maxRows: 50, remaining: 0, passActive: false, statusLine: '', accepted: false,
    // 加量体验、申报季加量（D-070，都以服务端 /advisor/status 为准，见 utils/boost.js）
    trialAvailable: false, trialActive: false, trialLeft: 0, trialPitch: '', trialCap: '', trialEnded: '', trialHint: '', seasonLine: '',
    // 客户工作台 30 天（只有服务端在售时才有值）：{ price: '29', days: 30 }
    offer: null },
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
      this.loadOffer(caps.pay, serial);
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
  // 在售时取一次价格；取不到就当没在售，不影响免费使用
  loadOffer(entry, serial) {
    if (!(entry && entry.enabled)) { this.setData({ offer: null }); return; }
    api.payProducts().then((res) => {
      if (this.dead || serial !== this.serial) return;
      const product = pay.normalizeProducts(res)[0];
      this.setData({ offer: product ? { price: pay.yuan(product.price), days: product.days } : null });
    }).catch(() => { if (!this.dead && serial === this.serial) this.setData({ offer: null }); });
  },
  buyPass() { wx.navigateTo({ url: '/pages/pass/index' }); },
  // 加量体验的确认页：规则写全，用户点"开始体验"才起算；回到本页时 onShow 重新读额度
  openTrial() { wx.navigateTo({ url: '/pages/advisor-trial/index' }); },
  // 静默刷新额度（服务端说次数或上限变了、或页面上的额度已旧），不清空已填的内容；读到了返回 true
  refreshQuota() {
    const serial = this.serial;
    return api.advisorStatus().then((status) => {
      if (this.dead || serial !== this.serial) return false;
      this.setData(quotaView(status, this.data.kind));
      return true;
    });
  },
  // 被挡住但额度可能已旧（跨天、体验到期）：重读一次再按新的额度走提交。每次点击最多自动重读一次
  // （手机时钟比服务器快时，体验结束前后会一直判断为"已旧"）；读不到就提示
  recheck() {
    if (this.rechecking) return;
    this.rechecking = true;
    this.refreshQuota().then((fresh) => {
      this.rechecking = false;
      if (fresh && !this.dead) { this.rechecked = true; this.submit(); }
    }).catch((err) => {
      this.rechecking = false;
      if (!this.dead) this.setData({ inputError: `未能读取工具额度：${err.message}` });
    });
  },
  // 现在还能不能提交：使用期不限次数；体验期看体验当天还剩几次；其余看本月免费次数
  blocked() { return !this.data.passActive && (this.data.trialActive ? this.data.trialLeft <= 0 : this.data.remaining <= 0); },
  // 额度和行数的提示都从这两个方法出来，记下最近一句（trialHint）：页面上显示的正是这句、且推荐了体验时，
  // 提示下面才出现"了解加量体验"按钮；格式错误、网络错误下面不出现（Codex 审阅）。
  // 体验刚结束（7 天内，trialEnded 有值）的提示先说"加量体验已结束"，不然用户不知道每次 500 家为什么不行了
  limitMessage() {
    if (this.data.trialActive) return this.hint(this.data.trialCap);
    return this.hint(`${this.data.trialEnded ? '加量体验已结束。' : ''}${limitText(this.data.offer, this.data.trialAvailable)}`);
  },
  rowsMessage() {
    const more = this.data.trialAvailable && !this.data.passActive ? '；或开始 3 天加量体验，每次最多 500 家' : '';
    return this.hint(`${this.data.trialEnded ? '加量体验已结束，' : ''}每次最多 ${this.data.maxRows} 家，请减少行数${more}`);
  },
  hint(message) { this.setData({ trialHint: message }); return message; },
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
      if (this.data.rowCount > this.data.maxRows) throw new Error(this.rowsMessage());
      return { kind: this.data.kind, file: this.file };
    }
    const rows = pasteRows(this.data.kind, this.data.text);
    if (!rows.length) throw new Error('请先填写客户');
    if (rows.length > this.data.maxRows) throw new Error(this.rowsMessage());
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
        inputError: res.rowCount > this.data.maxRows ? this.rowsMessage() : '' });
    }).catch((err) => { if (!this.dead && serial === this.serial && epoch === this.previewEpoch) this.setData({ previewing: false, previewed: false, agreed: err.status === 403 ? false : this.data.agreed, inputError: err.message }); });
  },
  submit() {
    if (this.data.submitting || this.data.previewing || this.data.accepted || !this.data.enabled) return;
    if (!this.data.agreed) { this.terms(); return; }
    const rechecked = this.rechecked; this.rechecked = false;
    if (!rechecked && this.blocked() && quotaStale(this.data)) { this.recheck(); return; }
    if (this.blocked() && !this.data.previewed) { this.setData({ inputError: this.limitMessage() }); return; }
    if (!this.data.previewed) { this.setData({ inputError: '请先预览并确认本次输入' }); return; }
    let body;
    try { body = this.body(); } catch (err) { this.setData({ inputError: err.message }); return; }
    let request;
    try { request = idempotency.claim(wx, this.taskOwner, body); this.key = request.key; }
    catch (err) { this.setData({ inputError: err.message }); return; }
    if (!request.existing && this.blocked()) {
      try { idempotency.confirm(wx, request); } catch (ignored) { /* 未发送，不自动重试 */ }
      this.setData({ inputError: this.limitMessage() }); return;
    }
    this.setData({ submitting: true, inputError: '' });
    api.createAdvisorTask(body, this.key).then((task) => {
      try { idempotency.confirm(wx, request); } catch (err) { /* 已确认任务，保留原键也不会重复创建 */ }
      if (this.dead || this.taskOwner !== request.owner) return;
      this.file = null;
      this.setData({ submitting: false, accepted: true, text: '', fileName: '', rowCount: 0 });
      if (this.visible) wx.redirectTo({ url: `/pages/advisor-tasks/index?taskId=${encodeURIComponent(task.id)}`, fail: () => wx.showToast({ title: '已提交，请到“我的记录”查看', icon: 'none' }) });
    }).catch((err) => {
      if (!request.existing && err.status >= 400 && err.status < 500 && err.status !== 408) {
        try { idempotency.confirm(wx, request); } catch (ignored) { /* 存储失败保留旧键 */ }
      }
      if (this.dead || this.taskOwner !== request.owner) return;
      const change = { submitting: false, agreed: err.status === 403 ? false : this.data.agreed,
        inputError: (!err.status || [502, 503, 504].includes(err.status)) ? `${err.message}。提交状态尚未确认；可重试同一内容，或到“我的记录”查看是否已受理。` : err.message };
      // 服务端按免费处理，说明使用期或加量体验刚好到期：先按服务端的说法改，再静默刷新额度那一行
      const ended = ['FREE_MONTHLY_LIMIT', 'FREE_ROW_LIMIT'].includes(err.code) && (this.data.passActive || this.data.trialActive);
      // 加量体验刚到期：提示先说体验已结束（刷新额度后换成带结束时间的一句）
      if (ended && this.data.trialActive) change.trialEnded = '加量体验已结束，已回到免费版';
      const free = ['FREE_MONTHLY_LIMIT', 'FREE_ROW_LIMIT'].includes(err.code) ? { passActive: false, trialActive: false } : {};
      if (err.code === 'FREE_MONTHLY_LIMIT') Object.assign(change, free, { remaining: 0 });
      if (err.code === 'TRIAL_DAILY_LIMIT') Object.assign(change, { trialLeft: 0 });
      if (['FREE_ROW_LIMIT', 'PASS_ROW_LIMIT', 'TRIAL_ROW_LIMIT'].includes(err.code)) {
        const limit = err.detail && err.detail.maxRows;
        Object.assign(change, free, { previewed: false, ...(Number.isInteger(limit) && limit > 0 ? { maxRows: limit } : {}) });
      }
      this.setData(change);
      // 免费次数用完、当天体验次数用完、超过 50 家：换成本页的说法，说清为什么、接下来能做什么
      if (err.code === 'FREE_MONTHLY_LIMIT') this.setData({ inputError: this.limitMessage() });
      if (err.code === 'TRIAL_DAILY_LIMIT' && this.data.trialCap) this.setData({ inputError: this.data.trialCap });
      if (err.code === 'FREE_ROW_LIMIT' && (this.data.trialAvailable || this.data.trialEnded)) this.setData({ inputError: this.rowsMessage() });
      // 额度那一行跟着服务端改（不然上面还写着"今天还能提交 9 次"）
      if (ended || err.code === 'TRIAL_DAILY_LIMIT') this.refreshQuota().catch(() => { /* 下次进入页面会重新读取 */ });
    });
  },
});
