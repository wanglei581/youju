const { items } = require('../../content/standards.js');
const { copyLink } = require('../../utils/copy.js');
// 日期和文号进入本机键，换版时旧勾选不套到新条件上。
const STORAGE_KEY = 'youjuStandardsChecks20261008V1';
const checkKey = (standard, index) => `${standard.key}:${standard.document.number}:${index}`;

Page({
  data: { tabs: items.map((v) => ({ key: v.key, name: v.name })), active: items[0].key,
    standard: items[0], rows: [], error: '' },
  onLoad(query) {
    this.checks = {};
    this.setData({ active: items.some((v) => v.key === query.key) ? query.key : items[0].key });
    this.restore();
  },
  restore() {
    try {
      const saved = wx.getStorageSync(STORAGE_KEY);
      if (saved && (typeof saved !== 'object' || Array.isArray(saved))) throw new Error('invalid');
      this.checks = saved || {};
      this.setData({ error: '' });
    } catch (err) { this.setData({ error: '本机勾选未读取，请重试后再勾选' }); }
    this.showStandard();
  },
  showStandard() {
    const standard = items.find((v) => v.key === this.data.active);
    this.setData({ standard, rows: standard.conditions.map((v, index) => ({ ...v, checked: this.checks[checkKey(standard, index)] === true })) });
  },
  pickTab(e) {
    const active = e.currentTarget.dataset.key;
    if (!items.some((v) => v.key === active)) return;
    this.setData({ active }); this.showStandard();
    wx.pageScrollTo({ scrollTop: 0, duration: 0 });
  },
  toggle(e) {
    if (this.data.error) return;
    const index = e.currentTarget.dataset.index;
    if (!this.data.rows[index]) return;
    const key = checkKey(this.data.standard, index);
    const next = { ...this.checks };
    if (next[key]) delete next[key]; else next[key] = true;
    this.persist(next);
  },
  persist(next) {
    try {
      wx.setStorageSync(STORAGE_KEY, next);
      this.checks = next; this.showStandard();
    } catch (err) { wx.showToast({ title: '本机保存失败，请检查手机存储后重试', icon: 'none' }); }
  },
  clearChecks() {
    if (this.data.error) return;
    wx.showModal({ title: '清空勾选', content: '清空本机所有资质的勾选记录，原文仍可阅读。', confirmText: '清空', success: (r) => { if (r.confirm) this.persist({}); } });
  },
  copyOriginal() { copyLink(this.data.standard.document.url); },
});
