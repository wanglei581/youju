const api = require('../../utils/api.js');
const { todayISO, daysUntil, monthDay, weekday, dueTone } = require('../../utils/format.js');
const { requestOnce } = require('../../utils/subscribe.js');
const { remindedDocs } = require('../../utils/reminders.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');

const RANGES = [
  { key: 7, label: '7 天内' },
  { key: 14, label: '14 天内' },
  { key: 30, label: '30 天内' },
  { key: 0, label: '全部' },
];

// 某年某月的日历格子，周一开头
// dueDays：还没截止的日子（蓝点）；pastDays：近 30 天已截止的日子（灰点）
function monthCells(year, month, dueDays, pastDays) {
  const first = new Date(year, month - 1, 1).getDay(); // 0=周日
  const lead = (first + 6) % 7;
  const total = new Date(year, month, 0).getDate();
  const cells = [];
  for (let i = 0; i < lead; i += 1) cells.push({ blank: true, key: `b${i}` });
  for (let day = 1; day <= total; day += 1) {
    const dow = new Date(year, month - 1, day).getDay();
    const due = dueDays.includes(day);
    cells.push({ key: `d${day}`, day, due, past: !due && pastDays.includes(day), weekend: dow === 0 || dow === 6 });
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
    // 近 30 天已截止的事项（和还没截止的分开显示，默认收起）；pastShown 跟着"只看某月 / 某天"筛选
    past: [],
    pastShown: [],
    pastTitle: '',
    pastOpen: false,
    year: 0,
    month: 0,
    cells: [],
    sel: '',
    monthOnly: false,
    selDay: 0,
    selLabel: '',
    nearest: null,
    reminded: {},
  },

  onShow() {
    this.load();
    watchDay(this, () => this.load());
  },

  onHide() {
    stopDay(this);
  },

  onUnload() {
    stopDay(this);
  },

  reload() {
    this.load();
  },

  load() {
    this.setData({ error: '' });
    api.calendar().then(({ items }) => {
      // 只显示人工核对过的事项；剩余天数按今天实时计算。还没截止的（含今天截止）和近 30 天已截止的分开显示
      const verified = items
        .filter((d) => d.verified)
        .map((d) => {
          const days = daysUntil(d.due);
          return Object.assign({}, d, {
            days, tone: dueTone(days), date: monthDay(d.due), week: weekday(d.due),
          });
        });
      const upcoming = verified.filter((d) => d.days >= 0).sort((a, b) => a.days - b.days);
      const past = verified.filter((d) => d.days < 0 && d.days >= -30).sort((a, b) => b.days - a.days);
      const counts = {};
      RANGES.forEach((r) => { counts[r.key] = upcoming.filter((d) => !r.key || d.days <= r.key).length; });
      const base = upcoming.length ? upcoming[0].due : todayISO();
      const [y, m] = base.split('-').map(Number);
      const keepMonth = this.data.monthOnly && this.data.year;
      this.setData({ items: upcoming, past, counts, today: monthDay(todayISO()), nearest: upcoming[0] || null });
      if (!keepMonth) this.setData({ year: y, month: m });
      this.buildMonth();
      this.refresh();
    }).catch((err) => this.setData({ error: `加载失败：${err.message}` }));
    remindedDocs().then((reminded) => this.setData({ reminded }));
  },

  buildMonth() {
    const { year, month, items } = this.data;
    const prefix = `${year}-${month < 10 ? `0${month}` : month}-`;
    const dueDays = items.filter((d) => d.due.startsWith(prefix)).map((d) => Number(d.due.slice(8)));
    const pastDays = this.data.past.filter((d) => d.due.startsWith(prefix)).map((d) => Number(d.due.slice(8)));
    this.setData({ cells: monthCells(year, month, dueDays, pastDays) });
  },

  shiftMonth(e) {
    let { year, month } = this.data;
    month += Number(e.currentTarget.dataset.step);
    if (month === 0) { month = 12; year -= 1; }
    if (month === 13) { month = 1; year += 1; }
    this.setData({ year, month, sel: '', monthOnly: true });
    this.buildMonth();
    this.refresh();
  },

  refresh() {
    const { items, range, sel, monthOnly, year, month } = this.data;
    const prefix = `${year}-${month < 10 ? `0${month}` : month}-`;
    let shown;
    if (sel) shown = items.filter((d) => d.due === sel);
    else if (monthOnly) shown = items.filter((d) => d.due.startsWith(prefix));
    else shown = items.filter((d) => !range || d.days <= range);
    const hit = items.find((d) => d.due === sel);
    // 已截止的跟着筛选：只看某天时不显示（只能选有截止的日子），只看某月时只显示这个月的
    const { past } = this.data;
    let pastShown = past;
    let pastTitle = '近 30 天已截止';
    if (sel) pastShown = [];
    else if (monthOnly) { pastShown = past.filter((d) => d.due.startsWith(prefix)); pastTitle = `${month}月已截止`; }
    this.setData({ shown, pastShown, pastTitle, selLabel: hit ? hit.date : '', selDay: sel ? Number(sel.slice(8)) : 0 });
  },

  togglePast() {
    this.setData({ pastOpen: !this.data.pastOpen });
  },

  pickRange(e) {
    this.setData({ range: e.currentTarget.dataset.key, sel: '', monthOnly: false });
    this.refresh();
  },

  pickDay(e) {
    const { day, due } = e.currentTarget.dataset;
    if (!due) return;
    const { year, month } = this.data;
    const iso = `${year}-${month < 10 ? `0${month}` : month}-${day < 10 ? `0${day}` : day}`;
    this.setData({ sel: this.data.sel === iso ? '' : iso });
    this.refresh();
  },

  clearSel() {
    this.setData({ sel: '', monthOnly: false });
    this.refresh();
  },

  // 翻月后点"显示全部"：显示全部未截止的事项
  showAll() {
    this.setData({ sel: '', monthOnly: false, range: 0 });
    this.refresh();
  },

  // 截止提醒：一次性订阅消息，允许后才算订阅成功
  // 每条的 id 是截止记录编号，订阅按文件编号（docId）
  toggleRemind(e) {
    const { id } = e.currentTarget.dataset;
    // 点击时按今天重算剩余天数，不用缓存值
    const item = this.data.items.find((x) => x.id === id);
    if (!item) return;
    const days = daysUntil(item.due);
    if (days < 3) {
      wx.showToast({ title: '离截止不到 3 天，来不及提前提醒了', icon: 'none' });
      return;
    }
    if (this.data.reminded[item.docId]) {
      wx.showToast({ title: '已订阅：截止前 3 天提醒你一次', icon: 'none' });
      return;
    }
    requestOnce('deadline', { type: 'deadline', docId: item.docId }).then((ok) => {
      if (!ok) return;
      this.setData({ reminded: Object.assign({}, this.data.reminded, { [item.docId]: true }) });
      wx.showToast({ title: '已订阅截止提醒', icon: 'none' });
    });
  },

  open(e) {
    const { id, has } = e.currentTarget.dataset;
    if (!has) return;
    wx.navigateTo({ url: `/pages/detail/index?id=${id}` });
  },
});
