const api = require('../../utils/api.js');
const payTerms = require('../../content/pay-terms.js');
const { KINDS, pasteRows, consent } = require('../../utils/advisor.js');
const pay = require('../../utils/pay.js');

Page({
  data: { kind: 'compare', loading: true, error: '', inputError: '', agreed: false, mode: 'paste', text: '', fileName: '', rowCount: 0, previewing: false, submitting: false, remaining: 0, maxRows: 50, pay: { visible: false } },
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
  onShow() {
    this.visible = true;
    this.load();
    // 支付回调发生在页面隐藏期间，或轮询因隐藏而暂停：回到页面后重新查订单。
    if (this.payResume && this.poller) { this.payResume = false; this.poller.start(this.payOnce); }
  },
  onHide() {
    this.visible = false;
    if (this.poller && this.poller.isActive()) { this.poller.stop(); this.payResume = true; }
  },
  onUnload() {
    this.visible = false;
    this.payResume = false;
    if (this.poller) this.poller.stop();
  },
  load() {
    this.setData({ loading: true, error: '' });
    this.loadProducts();
    Promise.all([api.advisorStatus({ refresh: true }), consent()]).then(([res, agreed]) => {
      if (!res.enabled) throw new Error('工具暂未开放');
      const quota = res.quota[this.data.kind];
      this.setData({ loading: false, agreed, remaining: Math.max(0, quota.limit - quota.used), maxRows: quota.maxRows });
      this.renderPay();
    }).catch((err) => this.setData({ loading: false, error: err.message }));
  },
  terms() { wx.navigateTo({ url: '/pages/advisor-terms/index' }); },
  mode(e) {
    if (this.data.submitting || this.data.previewing || this.payBusy()) return;
    this.file = null;
    this.setData({ mode: e.currentTarget.dataset.mode, text: '', fileName: '', rowCount: 0, inputError: '' });
    this.refreshKey();
    this.renderPay();
  },
  input(e) {
    if (this.data.submitting) return;
    this.setData({ text: e.detail.value, inputError: '' });
    this.refreshKey();
    try { this.setData({ rowCount: pasteRows(this.data.kind, e.detail.value).length }); }
    catch (err) { this.setData({ rowCount: 0, inputError: err.message }); }
    this.renderPay();
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
      .catch((err) => this.setData({ previewing: false, rowCount: 0, agreed: err.status === 403 ? false : this.data.agreed, inputError: err.message }))
      .then(() => this.renderPay());
  },
  submit() {
    if (this.data.submitting || this.data.previewing || this.payBusy()) return;
    if (!this.data.agreed) { this.terms(); return; }
    if (!this.data.rowCount || this.data.rowCount > this.data.maxRows) { this.setData({ inputError: `请提交 1–${this.data.maxRows} 行客户` }); return; }
    let body;
    try { body = this.data.mode === 'file' ? { kind: this.data.kind, file: this.file } : { kind: this.data.kind, rows: pasteRows(this.data.kind, this.data.text) }; }
    catch (err) { this.setData({ inputError: err.message }); return; }
    this.refreshKey();
    this.setData({ submitting: true, inputError: '' });
    api.createAdvisorTask(body, this.key).then((task) => {
      wx.redirectTo({ url: `/pages/advisor-tasks/index?taskId=${encodeURIComponent(task.id)}`, fail: () => { this.setData({ submitting: false }); wx.showToast({ title: '已提交，请到我的任务查看', icon: 'none' }); } });
    }).catch((err) => this.setData({ submitting: false, agreed: err.status === 403 ? false : this.data.agreed, inputError: err.message }));
  },
  // ---- 付费比对（名单比对按次包）：只有接口返回可售商品时才显示 ----
  loadProducts() {
    // 跟进表不出售；支付进行中不刷新，避免付费区块在确认结果前消失。
    if (this.data.kind !== 'compare' || this.payBusy()) return;
    const serial = this.productSerial = (this.productSerial || 0) + 1;
    api.payProducts().then(pay.normalizeProducts, () => []).then((products) => {
      if (serial !== this.productSerial) return;
      this.products = products;
      this.renderPay();
    });
  },
  payBusy() { return !!(this.payState && this.payState.busy); },
  renderPay(next) {
    if (next) this.payState = Object.assign({}, this.payState, next);
    const state = this.payState || {};
    this.setData({ pay: pay.payView({
      products: this.products, kind: this.data.kind, mode: this.data.mode, rowCount: this.data.rowCount,
      maxRows: this.data.maxRows, remaining: this.data.remaining,
      busy: state.busy, selected: state.selected, message: state.message, error: state.error,
      termsFinal: payTerms.final, termsAgreed: !!state.termsAgreed,
    }) });
  },
  // 《付费服务协议》：定稿（final）且用户勾选后，付费按钮才可用；每次进入页面重新勾选。
  togglePayTerms(e) { this.renderPay({ termsAgreed: (e.detail.value || []).includes('agree') }); },
  payTermsDoc() { wx.navigateTo({ url: '/pages/agreement/index?type=pay' }); },
  buy(e) {
    if (this.payBusy() || this.data.submitting || this.data.previewing) return;
    if (!this.data.agreed) { this.terms(); return; }
    if (!payTerms.final || !(this.payState || {}).termsAgreed) {
      this.renderPay({ message: payTerms.final ? pay.TEXT.termsRequired : pay.TEXT.termsPending, error: true });
      return;
    }
    const product = pay.findProduct(this.products, e.currentTarget.dataset.id);
    let rows = [];
    try { rows = this.data.mode === 'paste' ? pasteRows('compare', this.data.text) : []; }
    catch (err) { this.renderPay({ message: err.message, error: true }); return; }
    const problem = pay.canBuy(product, { mode: this.data.mode, rowCount: rows.length });
    if (problem) { this.renderPay({ message: problem, error: true }); return; }
    if (typeof wx.requestVirtualPayment !== 'function') { this.renderPay({ message: pay.TEXT.unsupported, error: true }); return; }
    wx.showModal(Object.assign({}, pay.confirmModal(product, rows.length), { success: (res) => {
      if (res.confirm) this.startPay(product, rows);
    } }));
  },
  startPay(product, rows) {
    if (this.payBusy()) return;
    if (this.poller) this.poller.stop();
    this.payResume = false; this.payContext = null;
    this.renderPay({ busy: true, selected: product.productId, message: pay.TEXT.ordering, error: false });
    new Promise((resolve, reject) => {
      const failed = () => reject(new Error(pay.TEXT.login));
      wx.login({ success: (res) => (res && res.code ? resolve(res.code) : failed()), fail: failed });
    }).then((code) => api.createPayOrder({ product_id: product.productId, rows, code })).then((order) => {
      this.poller = pay.createPoller({
        fetch: () => api.payOrder(order.orderId),
        onDone: (result) => this.payDone(result),
        timers: { set: setTimeout, clear: clearTimeout },
        now: () => Date.now(),
      });
      wx.requestVirtualPayment(Object.assign(pay.paymentParams(order), {
        success: () => this.afterPay({ paid: true, cancelled: false }),
        fail: (err) => this.afterPay({ paid: false, cancelled: pay.isCancel(err) }),
      }));
    }).catch((err) => {
      if (err.status === 403) this.setData({ agreed: false });
      this.renderPay({ busy: false, message: pay.orderError(err), error: true });
    });
  },
  // 微信回调不算支付结果：成功、失败、取消都去查服务端订单；取消只核对一次。
  afterPay(context) {
    this.payContext = context;
    this.payOnce = !!context.cancelled;
    this.renderPay({ message: context.cancelled ? pay.TEXT.cancelled : pay.TEXT.confirming, error: false });
    if (!this.visible) { this.payResume = true; return; }
    this.poller.start(this.payOnce);
  },
  payDone(result) {
    this.payResume = false;
    const view = pay.resultView(result, this.payContext || {});
    if (view.taskId === null) { this.renderPay({ busy: false, message: view.message, error: view.error }); return; }
    this.renderPay({ message: view.message, error: false });
    wx.redirectTo({ url: `/pages/advisor-tasks/index?taskId=${encodeURIComponent(view.taskId)}`, fail: () => {
      this.renderPay({ busy: false, message: pay.TEXT.redirect, error: false });
    } });
  },
});
