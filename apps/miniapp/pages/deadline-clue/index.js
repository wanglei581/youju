const api = require('../../utils/api.js');
const SUCCESS = '已收到。核对需要几天，结果在"奖励规则 → 我的线索"里看';
Page({
  data: { ready: false, loading: true, closed: false, error: '', docId: 0, title: '', kinds: ['企业申报截止', '推荐或审核截止', '材料报送截止', '其他'], kindIndex: 0, date: '', time: '', quote: '', sending: false, received: false },
  onLoad(query) {
    let title = '';
    try { title = decodeURIComponent(query.title || ''); } catch (e) { /* 标题不影响文件编号 */ }
    this.setData({ docId: Number(query.id), title });
  },
  onShow() { return this.load(); },
  load() {
    this.setData({ ready: false, loading: true, closed: false, error: '' });
    return api.myRewards().then(() => this.setData({ ready: true, loading: false })).catch((err) => {
      this.setData({ loading: false, closed: err.status === 404, error: err.status === 404 ? '' : err.message });
    });
  },
  kindChange(e) { this.setData({ kindIndex: Number(e.detail.value) }); },
  dateChange(e) { this.setData({ date: e.detail.value }); },
  timeChange(e) { this.setData({ time: e.detail.value }); },
  clearTime() { this.setData({ time: '' }); },
  quoteChange(e) { this.setData({ quote: e.detail.value }); },
  submit() {
    if (!this.data.ready || this.data.loading || this.data.closed || this.data.sending || this.data.received) return Promise.resolve();
    const { docId, date, time, kinds, kindIndex } = this.data;
    const quote = this.data.quote.trim();
    let error = '';
    if (!Number.isSafeInteger(docId) || docId <= 0) error = '文件编号无效';
    else if (!kinds[kindIndex]) error = '请选择日期的性质';
    else if (!date) error = '请选择截止日期';
    else if ([...quote].length < 6 || [...quote].length > 200) error = '原文句子须为 6–200 字';
    if (error) { this.setData({ error }); return Promise.resolve(); }
    this.setData({ sending: true, error: '' });
    return api.submitDeadlineClue({ document_id: docId, kind: kinds[kindIndex], date, time: time || null, quote }).then(() => {
      this.setData({ sending: false, received: true });
      wx.showToast({ title: SUCCESS, icon: 'none', duration: 3500 });
      this.returnTimer = setTimeout(() => wx.navigateBack({ fail: () => wx.redirectTo({ url: '/pages/rewards/index' }) }), 3500);
    }).catch((err) => {
      let message = err.message || '提交失败，请稍后再试';
      if (err.status === 409 && message === '线索暂时收满了，过几天再来') message = '待核对的线索已经比较多，暂时不收新的，过几天再来';
      this.setData({ sending: false, closed: err.status === 404, error: err.status === 404 ? '' : message });
    });
  },
  onUnload() { clearTimeout(this.returnTimer); },
});
