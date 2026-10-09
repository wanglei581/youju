const api = require('../../utils/api.js');
const { loadClients, clientParams, subtitle, highlightWords, conditionQuotes } = require('../../utils/clients.js');
const { todayISO } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { TAG_NOTE } = require('../../utils/conditions.js');
const PAGE_SIZE = 20;

Page({
  data: { items: [], total: 0, loading: false, error: '', client: null, ignoreIndustry: false, subtitle: '', tagNote: TAG_NOTE, states: {} },
  onLoad(query) {
    this.clientId = query.id;
  },
  onShow() {
    this.setData({ today: todayISO() });
    try {
      const client = loadClients().find((c) => c.id === this.clientId);
      if (!client) {
        this.seq = (this.seq || 0) + 1;
        this.setData({ client: null, items: [], states: {}, loading: false, error: '本机客户卡片已删除或不存在，请返回重新选择' }); return;
      }
      // 从详情返回也重新核对截止状态；详情条件只在点开时获取。
      this.setData({ client });
      this.apply(true);
      watchDay(this, (today) => { this.setData({ today }); this.apply(true); });
    } catch (err) {
      this.seq = (this.seq || 0) + 1;
      this.resultGeneration = (this.resultGeneration || 0) + 1;
      this.setData({ client: null, items: [], states: {}, loading: false, error: '本机客户卡片读取失败，请返回重试' });
    }
  },
  onHide() { stopDay(this); },
  onUnload() { stopDay(this); this.seq = (this.seq || 0) + 1; this.resultGeneration = (this.resultGeneration || 0) + 1; },
  toggleIndustry(e) {
    this.setData({ ignoreIndustry: !!e.detail.value }); this.apply(true);
  },
  apply(reset) {
    if (!this.data.client) return;
    const { client, ignoreIndustry, items } = this.data;
    const seq = this.seq = (this.seq || 0) + 1;
    if (reset) this.resultGeneration = (this.resultGeneration || 0) + 1;
    this.setData({ loading: true, error: '', subtitle: subtitle(client, ignoreIndustry),
      ...(reset ? { items: [], states: {}, total: 0 } : {}) });
    api.search({ ...clientParams(client, ignoreIndustry), limit: PAGE_SIZE, offset: reset ? 0 : items.length })
      .then((res) => {
        if (seq !== this.seq) return;
        this.setData({ items: reset ? res.items : items.concat(res.items), total: res.total, loading: false });
      })
      .catch((err) => {
        if (seq !== this.seq) return;
        this.setData({ loading: false, error: err.status === 422 ? '所选筛选项已不可用，请返回编辑地区和产业方向' : '通知未加载，请重试' });
      });
  },
  retry() { this.apply(this.data.items.length === 0); },
  loadMore() { if (!this.data.loading && this.data.items.length < this.data.total) this.apply(false); },
  onReachBottom() { this.loadMore(); },
  toggleConditions(e) {
    const id = e.currentTarget.dataset.id;
    const doc = this.data.items.find((v) => v.id === id);
    if (!doc) return;
    const state = this.data.states[id];
    if (state && (state.loading || state.loaded)) {
      this.setData({ states: { ...this.data.states, [id]: { ...state, open: !state.open } } }); return;
    }
    const client = this.data.client;
    const generation = this.resultGeneration;
    const pending = { open: true, loading: true, loaded: false, rows: [], error: '' };
    this.setData({ states: { ...this.data.states, [id]: pending } });
    // 列表若已带核对条件可直接使用，当前接口通常在详情里提供。
    const promise = doc.apply_conditions ? Promise.resolve(doc) : api.detail(id);
    promise.then((detail) => {
      // 已换条件或结果时，旧详情不写回；分页追加则保留进行中的请求。
      if (generation !== this.resultGeneration || !this.data.client || this.data.client.id !== client.id) return;
      const rows = conditionQuotes(detail).map((quote) => ({ quote, segments: highlightWords(quote, client.keywords) }));
      this.setData({ states: { ...this.data.states, [id]: { ...pending, open: this.data.states[id].open, loading: false, loaded: true, rows } } });
    }).catch(() => {
      if (generation !== this.resultGeneration || !this.data.client || this.data.client.id !== client.id) return;
      this.setData({ states: { ...this.data.states, [id]: { ...pending, open: this.data.states[id].open, loading: false, error: '条件未加载，点此重试' } } });
    });
  },
});
