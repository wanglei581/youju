const api = require('../../utils/api.js');
const { todayISO, clockText } = require('../../utils/format.js');

// 本期清单（公众号方案 PR #287）：从公众号周报里的小程序链接打开（带 id），或从"我的 → 本周周报"打开
// （不带 id，取全国版最近一期）。接口已按省份排好（国家部委在前，各省按条数从多到少），这里按这个顺序分组，
// 组内按栏目：30 天内截止 → 新申报通知 → 名单 → 政策文件，和公众号正文一样。
const SECTIONS = { deadlines: '30 天内截止', applications: '新申报通知', lists: '名单', policies: '政策文件' };
const ORDER = ['deadlines', 'applications', 'lists', 'policies'];
// "全部地区"先显示这么多条，点"显示更多"再加：一期几百条时一次画完手机会卡
const PAGE = 80;

const provinceName = (province) => (province === '国家' ? '国家部委' : (province || '其他'));

function groupItems(items) {
  const groups = [];
  const byProvince = {};
  items.forEach((item) => {
    const key = item.province || '';
    let group = byProvince[key];
    if (!group) {
      group = { province: key, name: provinceName(key), count: 0, sections: [] };
      byProvince[key] = group;
      groups.push(group);
    }
    group.count += 1;
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

// 只画前 limit 条（按组、按栏目的顺序截断），剩下的条数给"显示更多"
function visible(groups, limit) {
  let left = limit;
  const out = [];
  for (const group of groups) {
    if (left <= 0) break;
    const sections = [];
    for (const section of group.sections) {
      if (left <= 0) break;
      const items = section.items.slice(0, left);
      left -= items.length;
      sections.push(Object.assign({}, section, { items }));
    }
    out.push(Object.assign({}, group, { sections }));
  }
  return out;
}

// 统计周：同月写"9月28日—30日"，跨月写"9月28日—10月4日"
function weekText(start, end) {
  const parse = (iso) => (/^\d{4}-\d{2}-\d{2}$/.test(iso || '') ? iso.split('-').map(Number) : null);
  const a = parse(start);
  const b = parse(end);
  if (!a || !b) return '';
  const tail = a[0] === b[0] && a[1] === b[1] ? `${b[2]}日` : `${b[1]}月${b[2]}日`;
  return `${a[1]}月${a[2]}日—${tail}`;
}

Page({
  data: {
    loading: true,
    error: '',
    // 没有这一期（不存在、还没发布或已撤回）或还没有发布的周报：写明原因，给出路
    missing: '',
    digest: null,
    groups: [],
    labels: [],
    index: 0,
    rest: 0,
    today: '',
    weekText: '',
    dataText: '',
  },

  onLoad(query) {
    // 微信把路径里的参数原样交给 onLoad；只认纯数字的期号，其余当作没带（打开最近一期）
    const id = String((query && query.id) || '');
    this.issueId = /^\d{1,10}$/.test(id) ? id : '';
    this.setData({ today: todayISO() });
    this.load();
  },

  // 跨天回到这一页时，剩余天数按新的一天算
  onShow() {
    const today = todayISO();
    if (this.data.today && today !== this.data.today) this.setData({ today });
  },

  load() {
    const seq = (this.seq || 0) + 1;
    this.seq = seq;
    const id = this.issueId;
    this.setData({ loading: true, error: '', missing: '' });
    const request = id
      ? api.digest(id)
      : api.latestDigest().then((brief) => api.digest(brief.id));
    request
      .then((d) => {
        if (seq !== this.seq) return;
        this.show(d);
      })
      .catch((err) => {
        if (seq !== this.seq) return;
        if (err && err.status === 404) {
          this.setData({ loading: false, missing: id ? '这一期周报不存在，或已撤回' : '还没有发布的周报' });
          return;
        }
        this.setData({ loading: false, error: `本期清单没有加载出来：${(err && err.message) || '请稍后再试'}` });
      });
  },

  show(d) {
    const items = Array.isArray(d.items) ? d.items : [];
    this.allGroups = groupItems(items);
    const labels = [`全部地区（${items.length} 条）`].concat(this.allGroups.map((g) => `${g.name}（${g.count} 条）`));
    this.limit = PAGE;
    this.setData({
      loading: false,
      digest: { id: d.id, demo: !!d.demo, total: d.total || items.length, listed: items.length, hidden: d.hidden || 0, truncated: !!d.truncated },
      weekText: weekText(d.weekStart, d.weekEnd),
      dataText: clockText(d.dataAt),
      labels,
      index: 0,
    });
    this.render();
  },

  render() {
    const groups = this.data.index ? [this.allGroups[this.data.index - 1]] : this.allGroups;
    const total = groups.reduce((sum, g) => sum + g.count, 0);
    // 选了省份时整省都列出；"全部地区"分批画
    const limit = this.data.index ? total : this.limit;
    this.setData({ groups: visible(groups, limit), rest: Math.max(0, total - limit) });
  },

  pickProvince(e) {
    const index = Number(e.detail.value) || 0;
    if (index < 0 || index > this.allGroups.length) return;
    this.limit = PAGE;
    this.setData({ index });
    this.render();
    if (wx.pageScrollTo) wx.pageScrollTo({ scrollTop: 0, duration: 0 });
  },

  more() {
    this.limit += PAGE * 2;
    this.render();
  },

  goReport() {
    wx.navigateTo({ url: '/pages/report/index?days=7' });
  },

  goHome() {
    wx.switchTab({ url: '/pages/home/index' });
  },
});
