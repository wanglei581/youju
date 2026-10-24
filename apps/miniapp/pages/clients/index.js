const api = require('../../utils/api.js');
const { regionSelection, areaParts, areaLabel, uncoveredCities, uncoveredDistricts, applyAreaChoice } = require('../../utils/regions.js');
const { loadCoverage } = require('../../utils/coverage.js');
const { previewDocs } = require('../../utils/exporter.js');
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
    cityOptions: [], cityRegion: [], cityError: '', cityCoverageNote: '', districtLabels: [], picked: {}, error: '', coverageError: '', placeholder: '客户 1',
    // 相关通知
    rows: [], shown: [], only: '', counts: {}, clientErrors: {}, loading: false, loaded: false, loadError: '', truncated: false, today: '',
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
    const sig = JSON.stringify([today, clients.map((c) => [c.id, c.name, c.regions, c.cities, c.districts, c.industries])]);
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
      this.setData({ rows: [], shown: [], counts: {}, clientErrors: {}, loading: false, loaded: true, loadError: '', truncated: false, only: '' });
      return;
    }
    const only = clients.some((c) => c.id === this.data.only) ? this.data.only : '';
    this.setData({ loading: true, loadError: '', only });
    const groups = queryGroups(clients);
    // 有区县的条件要先确认收录范围（还在取或上次没取到就再取一次）；未确认的那组不查，不改为全市或全省
    const ready = groups.some((g) => g.params.districts) && !this.coverage
      ? loadCoverage().then((c) => { this.coverage = c; }, () => { this.coverage = null; }) : Promise.resolve();
    ready.then(() => {
      if (seq !== this.seq) return undefined;
      const tasks = groups.map((g) => {
        const blocked = g.params.districts ? this.districtBlock(g.params.districts.split(',')) : '';
        if (blocked) return () => Promise.resolve({ ids: g.ids, failed: true, cityError: blocked });
        return () => api.search({ ...g.params, limit: PER_QUERY, offset: 0 })
          .then((res) => ({ ids: g.ids, items: res.items || [], total: res.total || 0 }))
          .catch((err) => ({ ids: g.ids, failed: true, cityError: /城市|区县/.test(err.message || '') ? err.message : '' }));
      });
      return runLimited(tasks, PARALLEL).then((results) => {
        if (seq !== this.seq) return;
        const ok = results.filter((r) => !r.failed);
        const failed = results.length - ok.length;
        const cityNote = [...new Set(results.map((r) => r.cityError).filter(Boolean))].join('；');
        const rows = mergeRelated(clients, ok);
        const counts = {};
        const clientErrors = {};
        results.filter((r) => r.failed).forEach((r) => r.ids.forEach((id) => {
          clientErrors[id] = r.cityError || '相关通知加载失败，请重试';
        }));
        rows.forEach((row) => row.ids.forEach((id) => { counts[id] = (counts[id] || 0) + 1; }));
        this.setData({
          rows, counts, clientErrors, loading: false, loaded: true,
          truncated: ok.some((r) => r.total > r.items.length) || rows.length > MAX_DOCS,
          loadError: cityNote ? `${cityNote}。未查询的城市、区县不会改为全市或全省，未查到不代表没有通知。` : failed === results.length ? '相关通知没有加载出来' : (failed ? `有 ${failed} 组客户的通知没有加载出来` : ''),
        });
        this.refreshShown();
      });
    });
  },
  // 区县只在收录范围里有这个省、市、区县时才查；没取到收录范围也不查
  districtBlock(keys) {
    const missing = uncoveredDistricts(keys, this.coverage);
    if (!missing.length) return '';
    return this.coverage && Array.isArray(this.coverage.districts)
      ? `${missing.map(areaLabel).join('、')}暂未收录区县来源` : '区县收录范围没有取到，暂不能确认区县是否已收录';
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
    previewDocs(shown.map((r) => r.id), { label: client ? `我的客户 · ${client.displayName}` : '我的客户 · 全部' });
  },

  loadCities() {
    loadCoverage().then((c) => {
      this.coverage = c;
      const cityNote = c && Array.isArray(c.cities) ? (c.cities.length ? `只已收录 ${c.cities.length} 个城市的部分来源，不代表全市完整覆盖。` : '当前尚未接入市级来源；城市可以保存，相关通知暂不可查询。') : '';
      const districtNote = c && Array.isArray(c.districts) ? (c.districts.length ? `只已收录 ${c.districts.length} 个区县的部分来源。` : '当前尚未接入区县来源；区县可以保存，相关通知暂不可查询，不会改为全市或全省。') : '';
      this.setData({ cityCoverageNote: `${cityNote}${districtNote}`, cityOptions: (c && c.cities || []).map((v) => ({ key: `${v.province}/${v.city}`, label: `${v.province}·${v.city}` })),
        coverageError: c ? '' : '已收录城市、区县未加载，可重试；省级地区仍可选择' });
      if (this.data.editing) this.refreshPicked();
    }).catch(() => this.setData({ coverageError: '已收录城市、区县未加载，可重试；省级地区仍可选择' }));
  },
  edit(e) {
    const id = e && e.currentTarget.dataset.id;
    const current = this.data.clients.find((c) => c.id === id);
    if (!current && this.data.clients.length >= MAX_CLIENTS) {
      wx.showToast({ title: '最多 20 家，更多请用工具中心批量处理', icon: 'none' }); return;
    }
    const form = normalize(current || {});
    this.setData({ editing: true, form, error: '', placeholder: `客户 ${current ? this.data.clients.indexOf(current) + 1 : this.data.clients.length + 1}` });
    this.refreshPicked();
    wx.pageScrollTo({ scrollTop: 0, duration: 0 });
  },
  refreshPicked() {
    const picked = {};
    ['regions', 'cities', 'industries'].forEach((group) => this.data.form[group].forEach((v) => { picked[`${group}:${v}`] = true; }));
    const { cities, districts } = this.data.form;
    const missing = uncoveredCities(cities, this.coverage);
    const notes = [];
    if (missing.length) notes.push(`${missing.map(areaLabel).join('、')}暂未收录市级来源。保留此条件后不会改为全省查询。`);
    if (districts.length && this.coverage !== undefined) {
      const lost = uncoveredDistricts(districts, this.coverage);
      if (lost.length) {
        notes.push(this.coverage && Array.isArray(this.coverage.districts)
          ? `${lost.map(areaLabel).join('、')}暂未收录区县来源。保留此条件后不会改为全市或全省查询。`
          : '区县收录范围没有取到，查询前会再核对；不会改为全市或全省查询。');
      }
    }
    this.setData({ picked, districtLabels: districts.map((key) => ({ key, label: areaLabel(key) })), cityError: notes.join('') });
  },
  nameInput(e) { this.setData({ form: { ...this.data.form, name: e.detail.value } }); },
  pick(e) {
    const { group, value } = e.currentTarget.dataset;
    const allowed = group === 'regions' ? PROVINCES : group === 'industries' ? INDUSTRIES : this.data.cityOptions.map((v) => v.key);
    if (!['regions', 'cities', 'industries'].includes(group) || !allowed.includes(value)) return;
    const current = this.data.form[group];
    // 加一个城市和在选择器里选全市一样：替换该市已选区县和全省
    if (group === 'cities' && !current.includes(value)) {
      const { form } = this.data;
      const next = applyAreaChoice({ regions: form.regions, cities: form.cities, districts: form.districts }, { ...areaParts(value), district: '' });
      this.setData({ form: normalize({ ...form, regions: next.regions, cities: next.cities, districts: next.districts }) }); this.refreshPicked();
      return;
    }
    const next = current.includes(value) ? current.filter((v) => v !== value) : current.concat(value);
    const form = { ...this.data.form, [group]: next };
    // 选全省替换该省已选的城市和区县
    if (group === 'regions') {
      form.cities = form.cities.filter((c) => !next.includes(c.split('/')[0]));
      form.districts = form.districts.filter((d) => !next.includes(d.split('/')[0]));
    }
    this.setData({ form: normalize(form) }); this.refreshPicked();
  },
  chooseCity(e) {
    try {
      const choice = regionSelection(e.detail.value, this.coverage);
      // 省份一栏选“全部”：不当作清空已选地区，提示改选具体省份或不选地区
      if (!choice.province) { this.setData({ cityError: '没有选具体省份；不选地区就是不限地区' }); return; }
      const { form } = this.data;
      const next = applyAreaChoice({ regions: form.regions, cities: form.cities, districts: form.districts }, choice);
      this.setData({ form: normalize({ ...form, regions: next.regions, cities: next.cities, districts: next.districts }), cityRegion: e.detail.value });
      this.refreshPicked();
      if (choice.note) {
        const kept = this.data.cityError || '';
        this.setData({ cityError: kept.includes(choice.note) ? kept : `${kept}${kept ? ' ' : ''}${choice.note}` });
      }
    } catch (err) { this.setData({ cityError: err.message }); }
  },
  removeCity(e) {
    this.setData({ form: { ...this.data.form, cities: this.data.form.cities.filter((v) => v !== e.currentTarget.dataset.value) } });
    this.refreshPicked();
  },
  removeDistrict(e) {
    this.setData({ form: { ...this.data.form, districts: this.data.form.districts.filter((v) => v !== e.currentTarget.dataset.value) } });
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
