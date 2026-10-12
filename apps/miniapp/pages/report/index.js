const api = require('../../utils/api.js');
const { exportDocs } = require('../../utils/exporter.js');
const { todayISO, verifyingNote, dueView, monthDay, weekday } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { readCache, writeCache, cachedLabel } = require('../../utils/cache.js');

// 截止（D-061）：原截止雷达和申报日历合成一页。7/14/30 天按列表看，也可以按月看。
// 日期只给人工核对过的，和 8 天以后到期、带原句的机器摘录；7 天内到期还没核对的只列标题，不写日期。
// "只看已核对"打开时列表不请求机器摘录（接口不带 excerpts，和以前的截止雷达一样），按月看在本机去掉摘录。
const PAGE_SIZE = 20;
const MONTH = 0;
const TABS = [
  { key: 7, label: '7 天内' },
  { key: 14, label: '14 天内' },
  { key: 30, label: '30 天内' },
  { key: MONTH, label: '按月看' },
];
const DEPTS = ['不限部门', '科技部门', '工信部门', '其他部门'];
const NO_SOON = { total: 0, items: [] };
// 默认视图（7 天内、不筛省份部门、没开"只看已核对"）存一份到本机，下次打开先显示（utils/cache.js）
const DEADLINE_CACHE = 'youjuDeadlineCacheV1';
const isDefaultView = ({ tab, province, dept, verifiedOnly }) => tab === 7 && !province && !dept && !verifiedOnly;

// 一条截止怎么显示。机器摘录按手机日期已进了 7 天内、或者没带原句的，不写日期（dueView 再拦一次）
function row(base, due) {
  const view = dueView(due);
  const dated = view.source !== 'unverified';
  return Object.assign(base, {
    kindText: due.kind || '申报截止',
    source: view.source,
    srcLabel: view.label,
    quote: dated ? view.quote : '',
    days: dated ? view.days : null,
    tone: dated && view.days < 0 ? 'past' : (dated && view.source === 'verified' ? (view.tone || 'normal') : 'normal'),
    dateText: dated ? monthDay(due.date) : '',
    week: dated ? `${weekday(due.date)}${due.time ? ` ${due.time}` : ''}` : '',
  });
}

// 列表（/reports/deadlines）的一条：带 excerpts 时有 source、quote；旧接口只有核对过的
function reportRow(d) {
  const due = d.due && typeof d.due === 'object'
    ? d.due
    : { kind: d.label, source: d.source || 'verified', date: d.date, time: d.time, quote: d.quote };
  return row({
    key: `r${d.id}`, docId: String(d.id), hasDetail: true, title: d.title,
    meta: [d.issuer, d.province].filter(Boolean).join(' · '), fileNumber: d.file_number || '',
  }, due);
}

// 按月看（/calendar）的一条，what 是截止类型。旧接口的 due 是这一条的日期；带 excerpts 时日期在 date，
// due 换成整份通知的主截止（对象），机器摘录的 verified 为 false、带 source 和 quote
function calendarRow(d) {
  const iso = typeof d.due === 'string' ? d.due : d.date;
  const source = d.source || (d.verified ? 'verified' : 'unverified');
  return row({
    key: d.id, docId: String(d.docId || d.id), hasDetail: d.hasDetail !== false, title: d.title,
    meta: d.region || '', fileNumber: '', iso,
  }, { kind: d.what, source, date: iso, time: d.time, quote: d.quote });
}

const prefixOf = (year, month) => `${year}-${month < 10 ? `0${month}` : month}-`;

// 某年某月的日历格子，周一开头。due：有核对过的截止；ex：只有机器摘录；past：已截止
function monthCells(year, month, marks, today) {
  const first = new Date(year, month - 1, 1).getDay(); // 0=周日
  const lead = (first + 6) % 7;
  const total = new Date(year, month, 0).getDate();
  const cells = [];
  for (let i = 0; i < lead; i += 1) cells.push({ blank: true, key: `b${i}` });
  for (let day = 1; day <= total; day += 1) {
    const dow = new Date(year, month - 1, day).getDay();
    const mark = marks[day] || '';
    cells.push({
      key: `d${day}`, day, due: mark === 'due', ex: mark === 'ex', past: mark === 'past', has: mark === 'due' || mark === 'ex',
      weekend: dow === 0 || dow === 6, today: `${prefixOf(year, month)}${day < 10 ? `0${day}` : day}` === today,
    });
  }
  return cells;
}

// 没有带日期的截止时第一行写什么。7 天内只会有核对过的（机器摘录只给 8 天以后的）
function emptyTitle({ tab, month, verifiedOnly, province, dept }) {
  if (tab === MONTH) return verifiedOnly ? `${month}月没有核对过的截止日期` : `${month}月没有核对过或机器摘录的截止日期`;
  if (province || dept) return '暂无符合条件的项目';
  if (tab === 7 || verifiedOnly) return `${tab} 天内暂无核对过的截止日期`;
  return `${tab} 天内暂无核对过或机器摘录的截止日期`;
}

Page({
  data: {
    statusBarHeight: 20,
    navHeight: 44,
    tabs: TABS,
    tab: 7,
    verifiedOnly: false,
    province: '',
    dept: '',
    provinces: ['不限省份'],
    depts: DEPTS,
    summary: { 7: 0, 14: 0, 30: 0 },
    rows: [],
    total: 0,
    page: 1,
    // 7 天内有候选、还没核对的通知：只列标题，不写日期
    soon: NO_SOON,
    // 没有内容时的说明：截止日期正在人工核对，目前已核对几条（服务端给的数字）
    verifyingNote: '',
    emptyTitle: '',
    // 显示的是本机存的上次数据时写明（取到新数据后清空）
    cachedText: '',
    loading: false,
    error: '',
    // 按月看
    year: 0,
    month: 0,
    cells: [],
    sel: '',
    selDay: 0,
    selLabel: '',
    past: [],
    pastOpen: false,
    truncated: false,
  },

  onLoad() {
    const { statusBarHeight, navHeight } = getApp().globalData;
    const [year, month] = todayISO().split('-').map(Number);
    this.setData({ statusBarHeight, navHeight, year, month });
    this.load(true);
  },
  onShow() {
    if (this.loadedDay && this.loadedDay !== todayISO()) this.reload();
    watchDay(this, () => this.reload());
  },
  onHide() { stopDay(this); },
  onUnload() { stopDay(this); },
  goBack() { wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) }); },

  reload() {
    this.cal = null;
    if (this.data.tab === MONTH) this.loadMonth(); else this.load(true);
  },
  retry() { this.reload(); },

  load(reset) {
    if (!reset && this.data.loading) return;
    const page = reset ? 1 : this.data.page + 1;
    const requestSeq = (this.requestSeq || 0) + 1;
    this.requestSeq = requestSeq;
    this.calSeq = (this.calSeq || 0) + 1; // 丢掉还没回来的按月看请求
    this.loadedDay = todayISO();
    const { tab, province, dept, verifiedOnly } = this.data;
    const unfiltered = tab === 30 && !province && !dept;
    if (!this.optionsLoading && !this.filterOptionsLoaded && !unfiltered) this.loadOptions();
    const cacheable = reset && isDefaultView(this.data);
    const cached = cacheable && !this.data.rows.length ? readCache(DEADLINE_CACHE) : null;
    if (cached) {
      this.show(cached.data, { reset: true, page: 1, cachedText: cachedLabel(cached.at) });
      this.setData({ loading: true, error: '' });
    } else {
      this.setData(reset ? { loading: true, error: '', rows: [], cachedText: '' } : { loading: true, error: '' });
    }
    api.deadlineReport({ days: tab, province, dept, page, size: PAGE_SIZE }, { verifiedOnly })
      .then((res) => {
        if (requestSeq !== this.requestSeq) return;
        if (unfiltered && !this.filterOptionsLoaded) this.setOptions(res);
        this.show(res, { reset, page, cachedText: '' });
        if (cacheable) {
          writeCache(DEADLINE_CACHE, {
            summary: res.summary, items: res.items, total: res.total, unverifiedSoon: res.unverifiedSoon,
            verified_total: res.verified_total,
          });
        }
      })
      .catch((err) => {
        if (requestSeq !== this.requestSeq) return;
        // 显示着上次的数据就留着，只在上面写明没更新
        this.setData({ loading: false, error: err.message });
      });
  },

  // 显示一页结果（接口刚返回的，或本机存的上次的）；截止日期按手机当天重新算
  show(res, { reset, page, cachedText }) {
    const rows = (res.items || []).map(reportRow);
    this.setData({
      summary: res.summary || { 7: 0, 14: 0, 30: 0 },
      rows: reset ? rows : this.data.rows.concat(rows),
      total: res.total, page, loading: false, cachedText,
      soon: this.data.verifiedOnly ? NO_SOON : (res.unverifiedSoon || NO_SOON),
      verifyingNote: verifyingNote(res.verified_total),
      emptyTitle: emptyTitle(this.data),
    });
  },

  setOptions(res) {
    this.filterOptionsLoaded = true;
    this.setData({ provinces: ['不限省份'].concat(Object.keys(res.by_province || {}).sort()) });
  },

  loadOptions() {
    this.optionsLoading = true;
    api.deadlineReport({ days: 30, province: '', dept: '', page: 1, size: 1 })
      .then((res) => this.setOptions(res))
      .catch(() => {})
      .then(() => { this.optionsLoading = false; });
  },

  // 按月看：一次取全部（接口分页取到 1000 条为止），换月、点日子、切"只看已核对"都在本机算
  loadMonth() {
    if (this.data.cachedText) this.setData({ cachedText: '' });
    const seq = (this.calSeq || 0) + 1;
    this.calSeq = seq;
    this.requestSeq = (this.requestSeq || 0) + 1; // 丢掉还没回来的列表请求
    this.loadedDay = todayISO();
    if (this.cal) { this.setData({ loading: false, error: '' }); this.showMonth(); return; }
    this.setData({ loading: true, error: '', rows: [] });
    api.calendar().then((res) => {
      if (seq !== this.calSeq) return;
      this.cal = res;
      this.setData({ loading: false, truncated: !!res.truncated, verifyingNote: verifyingNote(res.verifiedTotal) });
      this.showMonth();
    }).catch((err) => {
      if (seq !== this.calSeq) return;
      this.setData({ loading: false, error: `加载失败：${err.message}` });
    });
  },

  showMonth() {
    if (!this.cal) return;
    const { year, month, sel, verifiedOnly } = this.data;
    const prefix = prefixOf(year, month);
    const all = (this.cal.items || []).concat(this.cal.past || []).map(calendarRow);
    const upcoming = all.filter((d) => d.source !== 'unverified' && d.days >= 0 && (!verifiedOnly || d.source === 'verified'));
    // 已截止的只放核对过的（含按截止时刻刚过去、接口还算作未截止的）
    const past = all.filter((d) => d.source === 'verified' && d.days < 0 && d.days >= -30)
      .sort((a, b) => (a.iso < b.iso ? 1 : a.iso > b.iso ? -1 : 0));
    const marks = {};
    upcoming.filter((d) => d.iso.startsWith(prefix)).forEach((d) => {
      const day = Number(d.iso.slice(8));
      if (d.source === 'verified') marks[day] = 'due'; else if (marks[day] !== 'due') marks[day] = 'ex';
    });
    past.filter((d) => d.iso.startsWith(prefix)).forEach((d) => {
      const day = Number(d.iso.slice(8));
      if (!marks[day]) marks[day] = 'past';
    });
    const inMonth = upcoming.filter((d) => d.iso.startsWith(prefix));
    const shown = sel ? inMonth.filter((d) => d.iso === sel) : inMonth;
    this.setData({
      cells: monthCells(year, month, marks, todayISO()),
      rows: shown,
      total: shown.length,
      past: sel ? [] : past.filter((d) => d.iso.startsWith(prefix)),
      selDay: sel ? Number(sel.slice(8)) : 0,
      selLabel: sel && shown.length ? shown[0].dateText : '',
      soon: verifiedOnly ? NO_SOON : (this.cal.unverifiedSoon || NO_SOON),
      emptyTitle: emptyTitle(this.data),
    });
  },

  pickTab(e) {
    const tab = Number(e.currentTarget.dataset.key);
    if (tab === this.data.tab) return;
    this.setData({ tab, sel: '', pastOpen: false });
    if (tab === MONTH) this.loadMonth(); else this.load(true);
  },
  toggleVerified(e) {
    this.setData({ verifiedOnly: !!e.detail.value, sel: '' });
    if (this.data.tab === MONTH) { if (this.cal) this.showMonth(); else this.loadMonth(); } else this.load(true);
  },
  pickProvince(e) {
    const index = Number(e.detail.value);
    this.setData({ province: index === 0 ? '' : this.data.provinces[index] }); this.load(true);
  },
  pickDept(e) {
    const index = Number(e.detail.value);
    this.setData({ dept: index === 0 ? '' : this.data.depts[index] }); this.load(true);
  },
  more() { if (this.data.tab !== MONTH && this.data.rows.length < this.data.total) this.load(false); },

  shiftMonth(e) {
    let { year, month } = this.data;
    month += Number(e.currentTarget.dataset.step);
    if (month === 0) { month = 12; year -= 1; }
    if (month === 13) { month = 1; year += 1; }
    this.setData({ year, month, sel: '', pastOpen: false });
    this.showMonth();
  },
  pickDay(e) {
    const { day, has } = e.currentTarget.dataset;
    if (!has) return;
    const iso = `${prefixOf(this.data.year, this.data.month)}${day < 10 ? `0${day}` : day}`;
    this.setData({ sel: this.data.sel === iso ? '' : iso });
    this.showMonth();
  },
  clearSel() { this.setData({ sel: '' }); this.showMonth(); },
  togglePast() { this.setData({ pastOpen: !this.data.pastOpen }); },

  open(e) {
    const { id, has } = e.currentTarget.dataset;
    if (!has) return;
    wx.navigateTo({ url: `/pages/detail/index?id=${id}` });
  },
  // 没有内容时去搜索申报通知；选了省份、部门的带过去（取值和搜索筛选一致）。
  // 国家部委在搜索里是"层级"（UI 第二轮起不在地区里），服务端两处是同一个条件，结果相同
  goSearch() {
    const params = [`kind=${encodeURIComponent('申报通知')}`];
    const { province } = this.data;
    if (province === '国家部委') params.push(`levels=${encodeURIComponent(province)}`);
    else if (province) params.push(`regions=${encodeURIComponent(province)}`);
    if (this.data.dept) params.push(`depts=${encodeURIComponent(this.data.dept)}`);
    wx.navigateTo({ url: `/pages/search/index?${params.join('&')}` });
  },
  exportOne(e) { exportDocs([e.currentTarget.dataset.id], { label: '截止' }); },
});
