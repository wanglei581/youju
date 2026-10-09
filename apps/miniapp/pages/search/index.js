const api = require('../../utils/api.js');
const { todayISO } = require('../../utils/format.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const { exportDocs, MAX } = require('../../utils/exporter.js');
const { ensureConsent, loginOpen } = require('../../utils/consent.js');
const { requestOnce, remindOpen } = require('../../utils/subscribe.js');
const { loadCoverage, notCoveredNames, allUncovered } = require('../../utils/coverage.js');
const {
  requestScope, categoryConflict, supportConflict, migrateNational, NATIONAL, CATEGORIES, TAG_NOTE, CATEGORY_NOTE, GUIDE,
  foldNote, SUPPORT_MODES, SUPPORT_NOTE, INDUSTRIES, INDUSTRY_NOTE,
} = require('../../utils/conditions.js');

const KIND_TABS = [
  { key: 'all', label: '全部' },
  { key: '申报通知', label: '申报通知' },
  { key: '名单', label: '名单与公示' },
  { key: '政策', label: '政策' },
];

// 筛选弹层分左右两栏：左边按下面的顺序列组名（旁边是已选个数），右边是当前组的选项。
// 地区"不限地区"与具体省份互斥；发布时间单选（取值与服务端 /api/v1/search 一致）。
// 国家部委只在"层级"里选，不受地区限制，和所选地区的文件一起列出；省级只列所选地区的
// （请求的拼法见 conditions.requestScope，旧条件见 migrateNational）；市级与省级分别筛选
const REGION_SHORT = ['不限地区', '北京', '天津', '河北', '上海', '江苏', '安徽', '山东', '广东', '四川', '更多省份'];
const GROUPS = [
  // 还没收录的省份取自收录范围接口，写在 extra 里
  { key: 'region', title: '地区', hint: '可多选', multi: true, options: REGION_SHORT, note: '国家部委的文件在“层级”里选，和地区一起选时会同时列出', extra: '' },
  { key: 'type', title: '类型', hint: '可多选', multi: true, options: ['申报通知', '名单与公示', '政策文件', GUIDE, '其他'] },
  { key: 'open', title: '申报状态', hint: '单选', multi: false, options: ['只看未截止'], note: '只含截止日期已核对、还没截止的通知' },
  { key: 'time', title: '发布时间', hint: '单选', multi: false, options: ['近 7 天', '近 30 天', '近 90 天', '今年', '不限'] },
  // 项目类别、支持方式、产业方向都按标题归类（tagged）：右栏顶部统一写一行 TAG_NOTE，选项下面只写各组特有的补充。
  // 项目类别只有申报通知和名单有，不作资格判断；支持方式、产业方向只用于筛选（列表和详情不显示）
  { key: 'category', title: '项目类别', hint: '可多选', multi: true, tagged: true, options: CATEGORIES, note: CATEGORY_NOTE },
  { key: 'support', title: '支持方式', hint: '可多选', multi: true, tagged: true, options: SUPPORT_MODES, note: SUPPORT_NOTE },
  { key: 'industry', title: '产业方向', hint: '可多选', multi: true, tagged: true, options: INDUSTRIES, note: INDUSTRY_NOTE },
  { key: 'status', title: '名单状态', hint: '可多选', multi: true, options: ['公示', '公布'] },
  { key: 'level', title: '层级', hint: '可多选', multi: true, options: [NATIONAL, '省级', '市级'],
    note: '和地区一起选时，国家部委的文件同时列出，省级只列所选地区的，市级也按所选地区筛选；地市目前只收录部分城市，见“我的 → 数据从哪里来”' },
  { key: 'dept', title: '发文部门', hint: '可多选', multi: true, options: ['科技部门', '工信部门', '其他部门'] },
];
const ALL_PROVINCES = ['北京', '天津', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江', '上海', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北', '湖南', '广东', '广西', '海南', '重庆', '四川', '贵州', '云南', '西藏', '陕西', '甘肃', '青海', '宁夏', '新疆'];
const DEFAULT_TIME = '近 30 天';
const DEFAULT_PICKED = { region: ['不限地区'], level: [], type: [], category: [], support: [], industry: [], status: [], time: [DEFAULT_TIME], dept: [], open: [] };
// 关注确认框的标题：写关注之后会怎样
const FOLLOW_NOTE = '关注后，有新文件时在关注页标出';
const CATEGORY_CONFLICT = '项目类别只用于申报通知和名单，所选页签或类型里没有这两类文件，请调整类型或项目类别';
const SUPPORT_CONFLICT = '支持方式只用于申报通知和名单，所选页签或类型里没有这两类文件，请调整类型或支持方式';
const OPEN_NOTE = '“只看未截止”只包含截止日期已经人工核对的申报通知，截止日期正在核对中，可以去掉这个条件再看';
const PAGE = 20;

// 一组里算作"已选"的值：地区不算"不限地区"，发布时间只有改了默认的近 30 天才算
function activeValues(group, values) {
  if (group === 'region') return values.filter((r) => r !== '不限地区');
  if (group === 'time') return values.filter((t) => t !== DEFAULT_TIME);
  return values;
}

// 已选条件条上的文字：脱离了组名也要看得懂
function chipLabel(group, option) {
  if (group === 'type' && option === '其他') return '其他类型';
  if (group === 'time' && option === '不限') return '不限时间';
  if (group === 'status') return `名单${option}`;
  return option;
}

// 筛选入口那一排是固定文字；入口管的组里有已选时变品牌蓝，点开时右栏显示有已选的那一组
const ENTRY_GROUPS = { region: ['region', 'level'], time: ['time'], status: ['open', 'status'] };
const MORE_GROUPS = ['type', 'category', 'support', 'industry', 'dept'];

Page({
  data: {
    q: '',
    tabs: KIND_TABS,
    tab: 'all',
    items: [],
    total: 0,
    totalAll: 0,
    verifiedOpenTotal: 0,
    // 合并进转发组、没有另计的版本数（D-055）
    reposts: 0,
    loading: false,
    error: '',
    filterOpen: false,
    groups: GROUPS,
    // 筛选弹层右栏当前显示的组；counts 是每组已选几项（左栏组名旁边显示）
    activeGroup: 'region',
    counts: {},
    // 已选条件条：[{ key, group, option, label }]，按左栏组的顺序；空时不显示这一条
    activeChips: [],
    tagNote: TAG_NOTE,
    // 协议定稿前（D-056）不显示"关注"和"没找到想要的？告诉我们"（纠错要先同意协议），定稿后照旧
    loginOpen: loginOpen(),
    picked: DEFAULT_PICKED,
    pickedMap: {},
    // 筛选入口：on 是否变蓝，group 点开时右栏显示哪一组；chipTime 是发布时间（"不限时间共 X 份"用）
    entries: { region: { on: false, group: 'region' }, time: { on: false, group: 'time' }, status: { on: false, group: 'open' } },
    moreOn: false,
    chipTime: '近 30 天',
    // 选择模式：勾选文件后导出 Excel / Word
    selecting: false,
    chosen: {},
    chosenCount: 0,
    // 没有结果时：选中的地区全部还没收录，如"浙江（正在接入）"；否则附一行"暂未收录"的说明
    uncovered: '',
    notCoveredNote: '',
    // 没有结果时的补充说明：地区和层级没有重叠，或只看未截止
    emptyHint: '',
    // 资讯与指南（与申报无关的新闻、会议、招聘、许可等）默认折叠，写明折叠了几篇，点开就列出来
    showGuides: false,
    folded: 0,
    guides: 0,
    fold: { text: '', link: '' },
  },

  onLoad(query) {
    if (api.watchStartup) api.watchStartup(this);
    const kind = decodeURIComponent(query.kind || '');
    const time = decodeURIComponent(query.time || '');
    const picked = Object.assign({}, DEFAULT_PICKED);
    if (['近 7 天', '近 30 天', '近 90 天', '今年', '不限'].includes(time)) picked.time = [time];
    [['regions', 'region'], ['levels', 'level'], ['types', 'type'], ['categories', 'category'], ['supports', 'support'], ['industries', 'industry'], ['status', 'status'], ['depts', 'dept']].forEach(([param, group]) => {
      if (query[param]) picked[group] = decodeURIComponent(query[param]).split(',');
    });
    if (String(query.open) === '1') picked.open = ['只看未截止'];
    // 旧条件地区里的"国家部委"：结果相同时换成层级
    const migrated = migrateNational(picked.region, picked.level);
    picked.region = migrated.regions;
    picked.level = migrated.levels;
    this.setData({ picked });
    this.setData({
      q: decodeURIComponent(query.q || ''),
      tab: ['申报通知', '名单', '政策'].includes(kind) ? kind : 'all',
    });
    this.refreshChips();
    this.apply(true);
    // 地区筛选下的说明：还没收录的省份
    loadCoverage().then((c) => {
      const names = notCoveredNames(c);
      if (!names) return;
      const extra = `${names}暂未收录`;
      this.setData({ groups: this.data.groups.map((g) => (g.key === 'region' ? Object.assign({}, g, { extra }) : g)) });
    });
  },

  onHide() {
    if (api.stopStartup) api.stopStartup(this);
    stopDay(this);
  },

  onUnload() {
    if (api.stopStartup) api.stopStartup(this);
    stopDay(this);
  },

  onShow() {
    if (api.watchStartup) api.watchStartup(this);
    this.setData({ today: todayISO() });
    // 页面在后台跨过零点后再显示，时间筛选要按今天重新查
    if (this.loaded) this.apply(true);
    watchDay(this, (day) => {
      this.setData({ today: day });
      this.apply(true);
    });
  },

  // 按关键词、页签、筛选条件查询；reset 为 false 时是"加载更多"
  // 当前的搜索条件（与服务端 /api/v1/search 的参数、关注条件一致）
  conditions() {
    const { q, tab, picked } = this.data;
    // 地区和层级按 requestScope 拼：选了地区且层级含国家部委时，地区里加上国家部委。
    // 没选层级时不带 levels，和加层级之前保存的关注条件一致
    const scope = requestScope(picked.region, picked.level);
    return {
      q: q.trim(),
      kind: tab,
      regions: scope.regions,
      levels: scope.levels,
      types: picked.type.join(','),
      // 没选项目类别时不带这一项，和加项目类别之前保存的关注条件一致
      categories: picked.category.length ? picked.category.join(',') : undefined,
      // 支持方式、产业方向同样：没选时不带
      supports: picked.support.length ? picked.support.join(',') : undefined,
      industries: picked.industry.length ? picked.industry.join(',') : undefined,
      status: picked.status.join(','),
      time: picked.time[0],
      depts: picked.dept.join(','),
      open: picked.open.includes('只看未截止') ? 1 : undefined,
    };
  },

  // 网络不好出错时，点"重试"按当前条件重新搜索
  removeOpen() {
    this.setData({ picked: { ...this.data.picked, open: [] } });
    this.refreshChips(); this.apply(true);
  },
  allTime() {
    this.setData({ picked: { ...this.data.picked, time: ['不限'] } });
    this.refreshChips(); this.apply(true);
  },
  retry() {
    this.apply(true);
  },

  apply(reset) {
    const { items } = this.data;
    // fold 不是筛选条件，不进关注：1 折叠资讯与指南（只给篇数），0 列出来
    const params = Object.assign(this.conditions(), { limit: PAGE, offset: reset ? 0 : items.length, fold: this.data.showGuides ? 0 : 1 });
    const seq = (this.seq || 0) + 1;
    this.seq = seq;
    // 换了关键词或筛选条件，之前勾选的不再对应当前结果，清空
    this.setData(reset ? { loading: true, error: '', chosen: {}, chosenCount: 0, uncovered: '', notCoveredNote: '', emptyHint: '' } : { loading: true, error: '' });
    api.search(params)
      .then((res) => {
        if (seq !== this.seq) return; // 只用最后一次查询的结果
        this.loaded = true;
        const folded = res.folded || 0;
        const guides = res.guides || 0;
        this.setData({
          items: reset ? res.items : items.concat(res.items),
          total: res.total,
          totalAll: res.total_all,
          verifiedOpenTotal: res.verified_open_total || 0,
          groups: this.data.groups.map((g) => g.key === 'open' ? { ...g, note: `只含截止日期已核对、还没截止的通知（目前 ${res.verified_open_total || 0} 份）` } : g),
          reposts: res.reposts || 0,
          folded,
          guides,
          fold: foldNote(folded, this.data.showGuides ? guides : 0),
          loading: false,
        });
        // 没有结果时区分"该地区还没收录"和"没有匹配的文件"（验收 A8）：只有选中的地区全部
        // 还没收录才说"还没收录"，不从关键词里推断地名
        if (!res.total) {
          let emptyHint = '';
          if (categoryConflict(params.kind, params.types, params.categories)) emptyHint = CATEGORY_CONFLICT;
          else if (supportConflict(params.kind, params.types, params.supports)) emptyHint = SUPPORT_CONFLICT;
          else if (params.open) emptyHint = OPEN_NOTE;
          this.setData({ emptyHint });
          const regions = params.regions.split(',');
          loadCoverage().then((c) => {
            if (seq !== this.seq) return;
            const hit = allUncovered(c, regions);
            this.setData({
              uncovered: hit.map((r) => `${r.name}（${r.reason}）`).join('、'),
              notCoveredNote: notCoveredNames(c),
            });
          });
        }
      })
      .catch((err) => {
        if (seq !== this.seq) return;
        // 重新查询失败时不保留旧结果，避免把旧条件的结果当成新结果
        this.setData(reset ? { loading: false, error: err.message, items: [], total: 0, totalAll: 0, reposts: 0, folded: 0, guides: 0, fold: foldNote(0, 0) } : { loading: false, error: err.message });
      });
  },

  refreshChips() {
    const { picked } = this.data;
    const pickedMap = {};
    Object.keys(picked).forEach((g) => picked[g].forEach((o) => { pickedMap[`${g}:${o}`] = true; }));
    const regions = picked.region.filter((r) => r !== '不限地区');
    const counts = {};
    const activeChips = [];
    GROUPS.forEach((g) => {
      const values = activeValues(g.key, picked[g.key] || []);
      counts[g.key] = values.length;
      values.forEach((o) => activeChips.push({ key: `${g.key}:${o}`, group: g.key, option: o, label: chipLabel(g.key, o) }));
    });
    // 选了不在常用列表里的省份（如从关注打开）时直接列出全部省份，看得到选中的那一项
    const expanded = !!this.moreRegions || regions.some((r) => !REGION_SHORT.includes(r));
    const options = expanded ? ['不限地区'].concat(ALL_PROVINCES) : REGION_SHORT;
    const entries = {};
    Object.keys(ENTRY_GROUPS).forEach((name) => {
      const keys = ENTRY_GROUPS[name];
      const hit = keys.find((k) => counts[k]);
      entries[name] = { on: !!hit, group: hit || keys[0] };
    });
    this.setData({
      groups: this.data.groups.map((g) => (g.key === 'region' ? Object.assign({}, g, { options }) : g)),
      counts,
      activeChips,
      pickedMap,
      entries,
      moreOn: MORE_GROUPS.some((k) => counts[k]),
      chipTime: picked.time[0],
    });
  },

  onInput(e) {
    this.setData({ q: e.detail.value, loading: true });
    this.seq = (this.seq || 0) + 1; // 作废进行中的查询，防止旧结果回写
    clearTimeout(this.inputTimer);
    this.inputTimer = setTimeout(() => this.apply(true), 300);
  },

  clearQ() {
    this.setData({ q: '' });
    this.apply(true);
  },

  cancel() {
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  goTool(e) {
    wx.navigateTo({ url: e.currentTarget.dataset.url });
  },

  pickTab(e) {
    this.setData({ tab: e.currentTarget.dataset.key });
    this.apply(true);
  },

  // 点"点开查看"列出资讯与指南，再点"收起"折叠回去
  toggleGuides() {
    this.setData({ showGuides: !this.data.showGuides });
    this.apply(true);
  },

  loadMore() {
    if (!this.data.loading && this.data.items.length < this.data.total) this.apply(false);
  },

  onReachBottom() {
    this.loadMore();
  },

  // 从"地区""近 30 天""状态"进来时右栏直接显示那一组；从"筛选"进来时显示上次看的组
  openFilter(e) {
    const group = e && e.currentTarget && e.currentTarget.dataset.group;
    this.setData({ filterOpen: true, activeGroup: group || this.data.activeGroup });
  },

  pickGroup(e) {
    this.setData({ activeGroup: e.currentTarget.dataset.key });
  },

  onFilterVisible(e) {
    this.setData({ filterOpen: e.detail.visible });
  },

  closeFilter() {
    this.setData({ filterOpen: false });
  },

  pickOption(e) {
    const { group, option } = e.currentTarget.dataset;
    // "更多省份"只是展开全部省份，不是筛选条件
    if (group === 'region' && option === '更多省份') {
      this.moreRegions = true;
      this.refreshChips();
      return;
    }
    const def = GROUPS.find((g) => g.key === group);
    const cur = this.data.picked[group] || [];
    let next;
    if (group === 'open') {
      next = cur.includes(option) ? [] : [option]; // 只有一项，再点一次取消
    } else if (!def.multi) {
      next = [option];
    } else if (group === 'region' && option === '不限地区') {
      next = ['不限地区'];
    } else {
      next = cur.includes(option) ? cur.filter((o) => o !== option) : cur.filter((o) => o !== '不限地区').concat(option);
      if (group === 'region' && next.length === 0) next = ['不限地区'];
    }
    this.setPicked(group, next);
  },

  setPicked(group, next) {
    this.setData({ picked: Object.assign({}, this.data.picked, { [group]: next }) });
    this.refreshChips();
    this.apply(true);
  },

  // 已选条件条和空结果里点"去掉"：去掉这一个条件，立即按新条件查
  removePicked(e) {
    const { group, option } = e.currentTarget.dataset;
    let next = (this.data.picked[group] || []).filter((o) => o !== option);
    if (group === 'region' && !next.length) next = ['不限地区'];
    if (group === 'time') next = [DEFAULT_TIME];
    this.setPicked(group, next);
  },

  // 筛选弹层的"重置"、已选条件条的"清空"、空结果的"清空条件"
  resetFilter() {
    this.moreRegions = false;
    this.setData({ picked: DEFAULT_PICKED });
    this.refreshChips();
    this.apply(true);
  },

  // 默认的关注名称，如"\u201c高新技术企业\u201d · 山东 · 申报通知"。地区和层级按界面上选的写
  // （请求里拼进地区的国家部委不重复写）
  followName() {
    const c = this.conditions();
    const { picked } = this.data;
    const tab = KIND_TABS.find((t) => t.key === c.kind);
    const regions = picked.region.filter((r) => r && r !== '不限地区');
    const parts = [];
    if (c.q) parts.push(`\u201c${c.q}\u201d`);
    parts.push(regions.length ? regions.slice(0, 3).join('、') : '不限地区');
    if (picked.level.length) parts.push(picked.level.join('、'));
    if (c.categories) parts.push(c.categories.split(',').slice(0, 2).join('、'));
    if (c.supports) parts.push(c.supports.split(',').slice(0, 2).join('、'));
    if (c.industries) parts.push(c.industries.split(',').slice(0, 2).join('、'));
    parts.push(tab ? tab.label : '全部');
    if (c.open) parts.push('只看未截止');
    return parts.join(' · ').slice(0, 40);
  },

  // 关注这个搜索：保存当前条件，有新文件时在"关注"里标出；关注后可以订阅一次微信提醒
  follow() {
    ensureConsent().then((ok) => {
      if (!ok) return;
      const conditions = this.conditions();
      if (!conditions.q) delete conditions.q;
      // 可编辑的确认框里内容是名称输入框，说明写在标题里
      wx.showModal({
        title: FOLLOW_NOTE,
        editable: true,
        placeholderText: '给这个关注起个名字',
        content: this.followName(),
        confirmText: '关注',
        success: (res) => {
          if (!res.confirm) return;
          const name = (res.content || '').trim().slice(0, 40) || this.followName();
          api.createFollow(name, conditions)
            .then((created) => {
              // 微信提醒还没开通时只告诉用户已关注，不问要不要订阅
              if (!remindOpen('newDocs')) {
                wx.showToast({ title: '已关注，有新文件时会在"关注"里标出', icon: 'none', duration: 2500 });
                return undefined;
              }
              return wx.showModal({
                title: '已关注',
                content: '有新文件时会在"关注"里标出。要不要再订阅一次微信提醒？',
                confirmText: '订阅提醒',
                cancelText: '不用了',
                success: (r) => {
                  if (!r.confirm) return;
                  requestOnce('newDocs', { followId: created.id }).then((subscribed) => {
                    if (subscribed) wx.showToast({ title: '已订阅 1 次微信提醒', icon: 'none' });
                  });
                },
              });
            })
            .catch((err) => {
              const invalid = err.status === 422 && (!err.message || /422|输入内容或筛选条件/.test(err.message));
              wx.showToast({ title: invalid ? '关注条件或名称不符合要求，请调整后重试' : (err.message || '关注失败，请稍后再试'), icon: 'none' });
            });
        },
      });
    });
  },

  // 进入或退出选择模式；退出时清空勾选
  select() {
    const selecting = !this.data.selecting;
    this.setData({ selecting, chosen: {}, chosenCount: 0 });
  },

  togglePick(e) {
    if (!this.data.selecting) return;
    const id = e.currentTarget.dataset.id;
    const chosen = Object.assign({}, this.data.chosen);
    if (chosen[id]) {
      delete chosen[id];
    } else {
      if (Object.keys(chosen).length >= MAX) {
        wx.showToast({ title: `一次最多导出 ${MAX} 份`, icon: 'none' });
        return;
      }
      chosen[id] = true;
    }
    this.setData({ chosen, chosenCount: Object.keys(chosen).length });
  },

  // 全选已加载的（最多 200 份）
  pickAll() {
    const chosen = {};
    this.data.items.slice(0, MAX).forEach((item) => { chosen[item.id] = true; });
    this.setData({ chosen, chosenCount: Object.keys(chosen).length });
  },

  exportChosen() {
    // 按列表里的顺序导出
    const ids = this.data.items.filter((item) => this.data.chosen[item.id]).map((item) => item.id);
    exportDocs(ids, { label: this.followName() });
  },

  feedback() {
    wx.switchTab({ url: '/pages/me/index' });
  },
});
