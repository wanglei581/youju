const api = require('../../utils/api.js');
const { regionSelection, uncoveredCities } = require('../../utils/regions.js');
const { loadCoverage } = require('../../utils/coverage.js');
const { exportDocs } = require('../../utils/exporter.js');
const { todayISO } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const {
  PROVINCES, INDUSTRIES, MAX_CLIENTS, PER_QUERY, MAX_DOCS, normalize, loadClients, saveClients, displayName, cardSummary,
  queryGroups, mergeRelated,
} = require('../../utils/clients.js');

// 我的客户（D-061）：原"按客户筛通知"升级。两个页签：
// - 相关通知：所有客户的近 90 天申报通知合并，一条通知一行，写明涉及哪几家客户、什么时候截止，可以导出；
// - 客户卡片：新增、编辑、删除（最多 20 家，只存在本机）。
// 条件相同的客户只查一次；同时最多发 4 个请求。
const PARALLEL = 4;

function runLimited(tasks, limit) {
  const results = new Array(tasks.length);
  let next = 0;
  const worker = () => {
    if (next >= tasks.length) return Promise.resolve();
    const index = next;
    next += 1;
    return tasks[index]().then((value) => { results[index] = value; }).then(worker);
  };
  return Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker)).then(() => results);
}

Page({
  tools() { wx.navigateTo({ url: '/pages/advisor/index' }); },
  data: {
    tab: 'docs', clients: [], editing: false, form: {}, provinces: PROVINCES, industries: INDUSTRIES,
    cityOptions: [], cityRegion: [], cityError: '', cityCoverageNote: '', picked: {}, error: '', coverageError: '', placeholder: '客户 1',
    // 相关通知
    rows: [], shown: [], only: '', counts: {}, loading: false, loaded: false, loadError: '', truncated: false, today: '',
  },

  onLoad(query) {
    if (query && query.tab === 'cards') this.setData({ tab: 'cards' });
    this.loadCities();
  },
  onShow() {
    const today = todayISO();
    let clients;
    try {
      clients = loadClients().map((c, i) => ({ ...c, displayName: displayName(c, i), summary: cardSummary(c) }));
      this.setData({ clients, error: '', today });
    } catch (err) {
      this.setData({ error: err.message || '本机记录读取失败，请稍后重试' });
      return;
    }
    // 从详情返回时客户和日期都没变，就不重新检索
    const sig = JSON.stringify([today, clients.map((c) => [c.id, c.name, c.regions, c.cities, c.industries])]);
    if (sig !== this.sig) { this.sig = sig; this.loadDocs(); }
    watchDay(this, () => { this.sig = ''; this.onShow(); });
  },
  onHide() { stopDay(this); },
  onUnload() { stopDay(this); this.seq = (this.seq || 0) + 1; },

  pickTab(e) { this.setData({ tab: e.currentTarget.dataset.tab }); },

  loadDocs() {
    const { clients } = this.data;
    const seq = this.seq = (this.seq || 0) + 1;
    if (!clients.length) {
      this.setData({ rows: [], shown: [], counts: {}, loading: false, loaded: true, loadError: '', truncated: false, only: '' });
      return;
    }
    const only = clients.some((c) => c.id === this.data.only) ? this.data.only : '';
    this.setData({ loading: true, loadError: '', only });
    const groups = queryGroups(clients);
    const tasks = groups.map((g) => () => api.search({ ...g.params, limit: PER_QUERY, offset: 0 })
      .then((res) => ({ ids: g.ids, items: res.items || [], total: res.total || 0 }))
      .catch((err) => ({ ids: g.ids, failed: true, cityError: /城市/.test(err.message || '') ? err.message : '' })));
    runLimited(tasks, PARALLEL).then((results) => {
      if (seq !== this.seq) return;
      const ok = results.filter((r) => !r.failed);
      const failed = results.length - ok.length;
      const cityNote = [...new Set(results.map((r) => r.cityError).filter(Boolean))].join('；');
      const rows = mergeRelated(clients, ok);
      const counts = {};
      rows.forEach((row) => row.ids.forEach((id) => { counts[id] = (counts[id] || 0) + 1; }));
      this.setData({
        rows, counts, loading: false, loaded: true,
        truncated: ok.some((r) => r.total > r.items.length) || rows.length > MAX_DOCS,
        loadError: cityNote ? `${cityNote}。未查询的城市不会改为全省，未查到不代表没有通知。` : failed === results.length ? '相关通知没有加载出来' : (failed ? `有 ${failed} 组客户的通知没有加载出来` : ''),
      });
      this.refreshShown();
    });
  },
  reload() { this.loadDocs(); },

  // 只看某一家客户；再点一次或点"全部客户"看全部
  pickOnly(e) {
    const id = e.currentTarget.dataset.id || '';
    this.setData({ only: this.data.only === id ? '' : id, tab: 'docs' });
    this.refreshShown();
  },
  // 客户卡片上的"看相关通知"：总是只看这一家（不像上面的筛选条那样再点一次取消）
  showClient(e) {
    this.setData({ only: e.currentTarget.dataset.id, tab: 'docs' });
    this.refreshShown();
  },
  refreshShown() {
    const { rows, only } = this.data;
    const list = only ? rows.filter((r) => r.ids.includes(only)) : rows;
    this.setData({ shown: list.slice(0, MAX_DOCS) });
  },
  exportShown() {
    const { shown, only, clients } = this.data;
    const client = clients.find((c) => c.id === only);
    // 名称只用在本机的导出记录里，不发给服务端
    exportDocs(shown.map((r) => r.id), { label: client ? `我的客户 · ${client.displayName}` : '我的客户 · 全部' });
  },

  loadCities() {
    loadCoverage().then((c) => {
      this.coverage = c;
      this.setData({ cityCoverageNote: c && Array.isArray(c.cities) ? (c.cities.length ? `只已收录 ${c.cities.length} 个城市的部分来源，不代表全市完整覆盖。` : '当前尚未接入市级来源；城市可以保存，相关通知暂不可查询。') : '', cityOptions: (c && c.cities || []).map((v) => ({ key: `${v.province}/${v.city}`, label: `${v.province}·${v.city}` })),
        coverageError: c ? '' : '已收录城市未加载，可重试；省级地区仍可选择' });
    }).catch(() => this.setData({ coverageError: '已收录城市未加载，可重试；省级地区仍可选择' }));
  },
  edit(e) {
    const id = e && e.currentTarget.dataset.id;
    const current = this.data.clients.find((c) => c.id === id);
    if (!current && this.data.clients.length >= MAX_CLIENTS) {
      wx.showToast({ title: '最多保存 20 个客户', icon: 'none' }); return;
    }
    const form = normalize(current || {});
    this.setData({ editing: true, form, error: '', placeholder: `客户 ${current ? this.data.clients.indexOf(current) + 1 : this.data.clients.length + 1}` });
    this.refreshPicked();
    wx.pageScrollTo({ scrollTop: 0, duration: 0 });
  },
  refreshPicked() {
    const picked = {};
    ['regions', 'cities', 'industries'].forEach((group) => this.data.form[group].forEach((v) => { picked[`${group}:${v}`] = true; }));
    const missing = uncoveredCities(this.data.form.cities, this.coverage);
    this.setData({ picked, cityError: missing.length ? `${missing.join('、')}暂未收录市级来源。保留此条件后不会改为全省查询。` : '' });
  },
  nameInput(e) { this.setData({ form: { ...this.data.form, name: e.detail.value } }); },
  pick(e) {
    const { group, value } = e.currentTarget.dataset;
    const allowed = group === 'regions' ? PROVINCES : group === 'industries' ? INDUSTRIES : this.data.cityOptions.map((v) => v.key);
    if (!['regions', 'cities', 'industries'].includes(group) || !allowed.includes(value)) return;
    const current = this.data.form[group];
    const next = current.includes(value) ? current.filter((v) => v !== value) : current.concat(value);
    const form = { ...this.data.form, [group]: next };
    if (group === 'regions') form.cities = form.cities.filter((c) => !next.includes(c.split('/')[0]));
    this.setData({ form: normalize(form) }); this.refreshPicked();
  },
  chooseCity(e) {
    try {
      const choice = regionSelection(e.detail.value, this.coverage);
      const form = { ...this.data.form };
      if (choice.city) form.cities = [...new Set(form.cities.concat(`${choice.province}/${choice.city}`))];
      else { form.regions = [...new Set(form.regions.concat(choice.province))]; form.cities = form.cities.filter((c) => c.split('/')[0] !== choice.province); }
      this.setData({ form: normalize(form), cityRegion: e.detail.value });
      this.refreshPicked();
      if (choice.note) this.setData({ cityError: choice.note });
    } catch (err) { this.setData({ cityError: err.message }); }
  },
  removeCity(e) {
    this.setData({ form: { ...this.data.form, cities: this.data.form.cities.filter((v) => v !== e.currentTarget.dataset.value) } });
    this.refreshPicked();
  },
  save() {
    try {
      // 保存时重新读本机记录，避免另一个页面删除后被旧列表覆盖。
      const clients = loadClients();
      const form = normalize(this.data.form);
      const index = clients.findIndex((c) => c.id === form.id);
      if (index >= 0) clients[index] = form;
      else {
        form.id = `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 9)}`;
        clients.push(form);
      }
      saveClients(clients);
      this.setData({ editing: false, error: '' }); this.onShow();
    } catch (err) { this.setData({ error: err.message || '本机保存失败，请检查手机存储后重试' }); }
  },
  cancel() { this.setData({ editing: false, error: '' }); },
  remove(e) {
    const id = e.currentTarget.dataset.id;
    wx.showModal({ title: '删除本机客户卡片', content: '删除后无法恢复。', confirmText: '删除', success: (r) => {
      if (!r.confirm) return;
      try { saveClients(loadClients().filter((c) => c.id !== id)); this.onShow(); }
      catch (err) { this.setData({ error: err.message || '本机删除失败，请重试' }); }
    } });
  },
});
