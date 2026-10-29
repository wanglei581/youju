const api = require('../../utils/api.js');
const { todayISO, clockText } = require('../../utils/format.js');

// 本期清单（公众号方案 PR #287）：从公众号周报里的小程序链接打开（带 id），或从"我的 → 本周周报"打开
// （不带 id，取全国版最近一期）。接口已按省份排好（国家部委在前，各省按条数从多到少），这里按这个顺序分组，
// 组内按栏目：30 天内截止 → 新申报通知 → 名单 → 政策文件，和公众号正文一样。
// 条目分页取（每次 100 条）：一期几百条时不用一次取完、一次画完；按省筛也是服务端筛、分页取。
const SECTIONS = { deadlines: '30 天内截止', applications: '新申报通知', lists: '名单', policies: '政策文件' };
const ORDER = ['deadlines', 'applications', 'lists', 'policies'];
const PAGE = 100;

const provinceName = (province) => (province === '国家' ? '国家部委' : (province || '其他'));

// counts：各省这一期的总条数（接口给的，不是已取到的条数）
function groupItems(items, counts = {}) {
  const groups = [];
  const byProvince = {};
  items.forEach((item) => {
    const key = item.province || '';
    let group = byProvince[key];
    if (!group) {
      group = { province: key, name: provinceName(key), count: counts[key] || 0, loaded: 0, sections: [] };
      byProvince[key] = group;
      groups.push(group);
    }
    group.loaded += 1;
    if (group.loaded > group.count) group.count = group.loaded;
    const sectionKey = SECTIONS[item.section] ? item.section : 'applications';
    let section = group.sections.find((s) => s.key === sectionKey);
    if (!section) {
      section = { key: sectionKey, label: SECTIONS[sectionKey], items: [] };
      group.sections.push(section);
    }
    section.items.push(item);
  });
  groups.forEach((group) => group.sections.sort((a, b) => ORDER.indexOf(a.key) - ORDER.indexOf(b.key)));
  return groups;
}

// 统计周：同月写"9月28日—30日"，跨月写"9月28日—10月4日"
function weekText(start, end) {
  const parse = (iso) => (/^\d{4}-\d{2}-\d{2}$/.test(iso || '') ? iso.split('-').map(Number) : null);
  const a = parse(start);
  const b = parse(end);
  if (!a || !b) return '';
  if (a[0] !== b[0]) return `${a[0]}年${a[1]}月${a[2]}日—${b[0]}年${b[1]}月${b[2]}日`;
  const tail = a[1] === b[1] ? `${b[2]}日` : `${b[1]}月${b[2]}日`;
  return `${a[1]}月${a[2]}日—${tail}`;
}

Page({
  data: {
    loading: true,
    error: '',
    // 没有这一期（不存在、还没发布或已撤回）、还没有发布的周报、链接不完整：写明原因，给出路
    missing: '',
    digest: null,
    groups: [],
    labels: [],
    index: 0,
    // 当前筛选下还没取的条数（"显示更多"）
    rest: 0,
    moreLoading: false,
    today: '',
    weekText: '',
    dataText: '',
  },

  onLoad(query) {
    // 微信把路径里的参数原样交给 onLoad；只认纯数字的期号。带了期号但格式不对（公众号里粘贴的路径被截断等）
    // 就说链接不完整，不悄悄换成最近一期——读者会以为看到的就是文章里那一期
    const raw = query && query.id !== undefined && query.id !== null ? String(query.id) : '';
    this.issueId = /^\d{1,10}$/.test(raw) ? raw : '';
    // 不带期号打开的（"我的 → 本周周报"）：找不到时说"还没有发布的周报"
    this.fromLatest = !raw;
    this.provinces = [];
    this.items = [];
    this.setData({ today: todayISO() });
    if (raw && !this.issueId) {
      this.setData({ loading: false, missing: '这一期的链接不完整，打不开' });
      return;
    }
    this.load();
  },

  // 跨天回到这一页时，剩余天数按新的一天算
  onShow() {
    const today = todayISO();
    if (this.data.today && today !== this.data.today) this.setData({ today });
  },

  load() {
    this.fetch(true);
  },

  // reset：从第一条重新取（打开、重试、换省份）；否则接着取下一页
  fetch(reset) {
    const seq = (this.seq || 0) + 1;
    this.seq = seq;
    const province = this.data.index && this.provinces[this.data.index - 1] ? this.provinces[this.data.index - 1].province : '';
    const offset = reset ? 0 : this.items.length;
    if (reset) this.setData({ loading: true, error: '', missing: '' });
    else this.setData({ moreLoading: true });
    const opened = this.issueId
      ? Promise.resolve(this.issueId)
      : api.latestDigest().then((brief) => { this.issueId = String(brief.id); return this.issueId; });
    opened
      .then((id) => api.digest(id, { province: province || undefined, offset, limit: PAGE }))
      .then((d) => {
        if (seq !== this.seq) return;
        this.receive(d, reset);
      })
      .catch((err) => {
        if (seq !== this.seq) return;
        if (err && err.status === 404) {
          this.setData({ loading: false, moreLoading: false, digest: null, missing: this.fromLatest ? '还没有发布的周报' : '这一期周报不存在，或已撤回' });
          return;
        }
        const message = (err && err.message) || '请稍后再试';
        if (!reset) {
          this.setData({ moreLoading: false });
          wx.showToast({ title: `没加载出来：${message}`, icon: 'none' });
          return;
        }
        this.setData({ loading: false, error: `本期清单没有加载出来：${message}` });
      });
  },

  receive(d, reset) {
    const page = Array.isArray(d.items) ? d.items : [];
    this.items = (reset ? [] : this.items).concat(page);
    if (Array.isArray(d.provinces)) this.provinces = d.provinces;
    const all = this.provinces.reduce((sum, p) => sum + (p.count || 0), 0);
    const counts = {};
    this.provinces.forEach((p) => { counts[p.province] = p.count; });
    const total = typeof d.total === 'number' ? d.total : this.items.length;
    this.setData({
      loading: false,
      moreLoading: false,
      error: '',
      digest: { id: d.id, demo: !!d.demo, total: all || total, hidden: d.hidden || 0 },
      weekText: weekText(d.weekStart, d.weekEnd),
      dataText: clockText(d.dataAt),
      labels: [`全部地区（${all || total} 条）`].concat(this.provinces.map((p) => `${provinceName(p.province)}（${p.count} 条）`)),
      groups: groupItems(this.items, counts),
      rest: Math.max(0, total - this.items.length),
    });
  },

  pickProvince(e) {
    const index = Number(e.detail.value) || 0;
    if (index < 0 || index > this.provinces.length || index === this.data.index) return;
    this.setData({ index });
    this.fetch(true);
    if (wx.pageScrollTo) wx.pageScrollTo({ scrollTop: 0, duration: 0 });
  },

  more() {
    if (this.data.moreLoading || !this.data.rest) return;
    this.fetch(false);
  },

  goReport() {
    wx.navigateTo({ url: '/pages/report/index?days=7' });
  },

  goHome() {
    wx.switchTab({ url: '/pages/home/index' });
  },
});
