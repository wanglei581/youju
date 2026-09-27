const api = require('../../utils/api.js');
const { dueTone } = require('../../utils/format.js');

const RANGES = [
  { key: 7, label: '7 天内' },
  { key: 14, label: '14 天内' },
  { key: 30, label: '30 天内' },
  { key: 0, label: '全部' },
];

// 2026 年 10 月 1 日是周四；日历按周一开头
function monthCells(dueDays) {
  const cells = [{ blank: true }, { blank: true }, { blank: true }];
  for (let day = 1; day <= 31; day += 1) {
    const weekend = (3 + day - 1) % 7 >= 5;
    cells.push({ day, due: dueDays.includes(day), weekend });
  }
  return cells;
}

Page({
  data: {
    ranges: RANGES,
    range: 30,
    counts: {},
    today: '',
    items: [],
    shown: [],
    cells: [],
    sel: 0,
    selLabel: '',
    reminded: {},
  },

  onLoad() {
    api.calendar().then(({ today, items }) => {
      const counts = {};
      RANGES.forEach((r) => { counts[r.key] = items.filter((d) => !r.key || d.days <= r.key).length; });
      const [, m, d] = today.split('-');
      this.setData({
        items: items.map((d) => Object.assign({}, d, { tone: dueTone(d.days) })),
        counts,
        today: `${Number(m)}月${Number(d)}日`,
        cells: monthCells(items.map((d) => d.day)),
      });
      this.refresh();
    });
  },

  refresh() {
    const { items, range, sel } = this.data;
    const shown = sel ? items.filter((d) => d.day === sel) : items.filter((d) => !range || d.days <= range);
    const hit = items.find((d) => d.day === sel);
    this.setData({ shown, selLabel: hit ? hit.date : '' });
  },

  pickRange(e) {
    this.setData({ range: e.currentTarget.dataset.key, sel: 0 });
    this.refresh();
  },

  pickDay(e) {
    const { day, due } = e.currentTarget.dataset;
    if (!due) return;
    this.setData({ sel: this.data.sel === day ? 0 : day });
    this.refresh();
  },

  clearSel() {
    this.setData({ sel: 0 });
    this.refresh();
  },

  toggleRemind(e) {
    const { id } = e.currentTarget.dataset;
    const reminded = Object.assign({}, this.data.reminded, { [id]: !this.data.reminded[id] });
    this.setData({ reminded });
    wx.showToast({ title: reminded[id] ? '截止前 3 天用微信提醒你一次' : '已取消提醒', icon: 'none' });
  },

  open(e) {
    wx.navigateTo({ url: `/pages/detail/index?id=${e.currentTarget.dataset.id}` });
  },
});
