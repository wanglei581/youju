const { loadCoverage } = require('../../utils/coverage.js');
const { PROVINCES, INDUSTRIES, MAX_CLIENTS, normalize, loadClients, saveClients, subtitle } = require('../../utils/clients.js');

Page({
  data: { clients: [], editing: false, form: {}, provinces: PROVINCES, industries: INDUSTRIES,
    cityOptions: [], picked: {}, keywordInput: '', error: '', coverageError: '', placeholder: '客户 1' },

  onLoad() { this.loadCities(); },
  onShow() {
    try {
      this.setData({ clients: loadClients().map((c, i) => ({ ...c, displayName: c.name || `客户 ${i + 1}`, summary: subtitle(c, false) })), error: '' });
    } catch (err) { this.setData({ error: err.message || '本机记录读取失败，请稍后重试' }); }
  },
  loadCities() {
    loadCoverage().then((c) => {
      this.setData({ cityOptions: (c && c.cities || []).map((v) => ({ key: `${v.province}/${v.city}`, label: `${v.province}·${v.city}` })),
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
    this.setData({ editing: true, form, keywordInput: '', error: '', placeholder: `客户 ${current ? this.data.clients.indexOf(current) + 1 : this.data.clients.length + 1}` });
    this.refreshPicked();
    wx.pageScrollTo({ scrollTop: 0, duration: 0 });
  },
  refreshPicked() {
    const picked = {};
    ['regions', 'cities', 'industries'].forEach((group) => this.data.form[group].forEach((v) => { picked[`${group}:${v}`] = true; }));
    this.setData({ picked });
  },
  nameInput(e) { this.setData({ form: { ...this.data.form, name: e.detail.value } }); },
  keywordInput(e) { this.setData({ keywordInput: e.detail.value }); },
  pick(e) {
    const { group, value } = e.currentTarget.dataset;
    const allowed = group === 'regions' ? PROVINCES : group === 'industries' ? INDUSTRIES : this.data.cityOptions.map((v) => v.key);
    if (!['regions', 'cities', 'industries'].includes(group) || !allowed.includes(value)) return;
    const current = this.data.form[group];
    const next = current.includes(value) ? current.filter((v) => v !== value) : current.concat(value);
    this.setData({ form: { ...this.data.form, [group]: next } }); this.refreshPicked();
  },
  addKeyword() {
    const word = this.data.keywordInput.trim();
    if (!word) return true;
    const keywords = this.data.form.keywords;
    if (Array.from(word).length > 12 || (!keywords.includes(word) && keywords.length >= 5)) {
      this.setData({ error: '关键词最多 5 个，每个不超过 12 字' }); return false;
    }
    this.setData({ form: { ...this.data.form, keywords: keywords.includes(word) ? keywords : keywords.concat(word) }, keywordInput: '', error: '' });
    return true;
  },
  removeKeyword(e) {
    this.setData({ form: { ...this.data.form, keywords: this.data.form.keywords.filter((v) => v !== e.currentTarget.dataset.value) } });
  },
  removeCity(e) {
    this.setData({ form: { ...this.data.form, cities: this.data.form.cities.filter((v) => v !== e.currentTarget.dataset.value) } });
    this.refreshPicked();
  },
  save() {
    if (!this.addKeyword()) return;
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
  search(e) { wx.navigateTo({ url: `/pages/client-results/index?id=${encodeURIComponent(e.currentTarget.dataset.id)}` }); },
});
