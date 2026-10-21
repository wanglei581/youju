const api = require('../../utils/api.js');
const { dueInfo, deadlineNote, dueView, cnDate, weekday, kindClass, regionText } = require('../../utils/format.js');
const { copyLink } = require('../../utils/copy.js');
const { requestOnce, remindOpen } = require('../../utils/subscribe.js');
const { remindedDocs } = require('../../utils/reminders.js');
const { loginOpen } = require('../../utils/consent.js');
const { exportDocs } = require('../../utils/exporter.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const attachments = require('../../utils/attachments.js');
const { versionRows, upstreamLine } = require('../../utils/repost.js');

// 正文默认显示的字数：在这个长度附近的换行处截断，读起来不断在半句
const BODY_PREVIEW = 600;

// 转发卡片的标题（D-070：卡片只显示通知标题和发文机关）："发文机关｜通知标题"，标题里已经写了发文机关的不重复。
// 微信卡片只显示两行左右，太长的截到 60 个字（按字截，不把表情等双字节字符截成半个）
const SHARE_TITLE_MAX = 60;
function shareTitle(doc) {
  const title = String(doc.title || '').trim();
  const issuer = String(doc.issuer || '').trim();
  const chars = Array.from(issuer && !title.includes(issuer) ? `${issuer}｜${title}` : title);
  return chars.length > SHARE_TITLE_MAX ? `${chars.slice(0, SHARE_TITLE_MAX - 1).join('')}…` : chars.join('');
}

// 文件加载出来以后才能转发：每次加载（包括回到这页时的刷新）先把右上角的"转发"变灰，加载成功再打开；
// 打不开（下架、编号不对、网络出错）时一直是灰的，不会转出一张打不开、没有标题或内容过期的卡片。不开朋友圈
function allowShare(on) {
  if (typeof wx === 'undefined') return;
  if (on && wx.showShareMenu) wx.showShareMenu({ menus: ['shareAppMessage'] });
  if (!on && wx.hideShareMenu) wx.hideShareMenu({ menus: ['shareAppMessage', 'shareTimeline'] });
}

function previewOf(text) {
  if (text.length <= BODY_PREVIEW) return text;
  const cut = text.lastIndexOf('\n', BODY_PREVIEW);
  return `${text.slice(0, cut > BODY_PREVIEW - 200 ? cut : BODY_PREVIEW)}……`;
}

// 申报条件：接口只给人工核对、整份确认过的条目，每条是类别和通知原文里的原句（逐字引用）。
// 只显示原句，不显示概括的说法；同一句原文只列一次；同一类别连着的几条只在第一条写类别
function conditionRows(doc) {
  const items = (doc && doc.apply_conditions && doc.apply_conditions.items) || [];
  const seen = {};
  const rows = items.filter((item) => {
    if (!item || !item.quote || seen[item.quote]) return false;
    seen[item.quote] = true;
    return true;
  });
  return rows.map((item, index) => ({
    category: item.category,
    quote: item.quote,
    showCat: index === 0 || rows[index - 1].category !== item.category,
  }));
}

// 详情的截止（D-061）：每个类型一行。日期只给核对过的和机器摘录的（摘录只会是 8 天以后到期的）
function deadlineRows(deadlines) {
  return deadlines.map((d) => {
    const view = dueView(d);
    const dated = view.source !== 'unverified';
    return {
      kind: d.kind || '截止',
      source: view.source,
      label: view.label,
      // 被拦下的摘录（按手机日期进了 7 天内、超过 30 天）不显示原句：原句里就写着日期
      quote: dated ? view.quote : '',
      days: dated ? view.days : null,
      tone: dated ? (view.source === 'excerpt' ? 'normal' : (view.tone || 'normal')) : '',
      dateText: dated ? cnDate(d.date) : '',
      week: dated ? `${weekday(d.date)}${d.time ? ` ${d.time}` : ''}` : '',
    };
  });
}

Page({
  data: { doc: null, shareReady: false, regionText: '', due: null, dueNote: '', dueDate: '', dueWeek: '', changes: [], tagClasses: [], reminded: false, error: '',
    bodyOpen: false, bodyFolded: false, bodyPreview: '', bodyRest: 0, supportExpanded: '', supportEvidence: '', conds: [],
    atts: [], attNote: '', versions: [], upstream: '' },

  onLoad(query) {
    if (api.watchStartup) api.watchStartup(this);
    // 协议定稿前（D-056）不显示"订阅截止提醒"和底栏的"纠错"，定稿后照旧
    this.setData({ loginOpen: loginOpen(), remindOpen: remindOpen('deadline') });
    this.docId = query.id;
    this.lastShow = Date.now();
    this.load();
  },

  // 标题卡里那行提示（D-075）：滚到"要点"下面的卡片
  toAi() {
    wx.pageScrollTo({ selector: '#ai-entry', duration: 300 });
  },

  // 卡片上"看附件"：滚到附件那一块
  toAttachments() {
    wx.pageScrollTo({ selector: '#attachments', duration: 300 });
  },

  // 转发给好友和群（D-070）：接收人不用登录也能打开同一份文件；不带任何奖励
  onShareAppMessage() {
    const { doc } = this.data;
    if (!doc) return { title: '文件详情', path: `/pages/detail/index?id=${encodeURIComponent(this.docId || '')}` };
    return { title: shareTitle(doc), path: `/pages/detail/index?id=${encodeURIComponent(doc.id)}` };
  },

  // 网络不好加载失败时，页面上点"重试"再加载一次
  load() {
    if (this.refreshing) return;
    this.refreshing = true;
    allowShare(false);
    // 底栏"转发"按钮和右上角菜单同步：加载或刷新时不能点，加载成功才能点
    this.setData({ loading: true, error: '', takenDown: false, shareReady: false });
    api.detail(this.docId)
      .then((doc) => {
        // 只有人工核对过的截止日期才显示（合规红线）
        const due = dueInfo(doc.deadline);
        const body = doc.body && doc.body.status === 'ok' ? doc.body : null;
        // 首发版（D-061）：接口给 deadlines 时按类型逐条写（企业申报、推荐、材料报送……），
        // 核对过的标"已核对"，机器摘录的标"机器摘录·未经人工核对"并附原句，其余写"截止未核对"
        const dls = Array.isArray(doc.deadlines) ? deadlineRows(doc.deadlines) : null;
        this.setData({
          doc,
          // 地区照录服务端写法；来源带区县而地区文字里没写时补上区县
          regionText: regionText(doc),
          // 正文默认只显示开头一段；比这长的才给"展开全文"
          bodyOpen: false,
          bodyFolded: !!body && body.text.length > BODY_PREVIEW,
          bodyPreview: body ? previewOf(body.text) : '',
          bodyRest: body ? body.total - body.text.length : 0,
          supportExpanded: '',
          supportEvidence: '',
          conds: conditionRows(doc),
          // 转发版本（D-055）：同一份部里通知的各地版本，各自的截止按版本列出
          versions: versionRows(doc.repost),
          upstream: upstreamLine(doc.repost),
          due,
          dls,
          // 没有核对过的日期时只写说明（截止未核对 / 截止时间见原文），不给任何日期
          dueNote: deadlineNote(doc),
          dueDate: due ? cnDate(doc.deadline.date) : '',
          dueWeek: due ? `${weekday(doc.deadline.date)}${doc.deadline.time ? ` ${doc.deadline.time}` : ''}` : '',
          tagClasses: (doc.tags || []).map((t) => kindClass(t)),
          // 附件：存有副本的点了在小程序里打开，其他的复制原文链接并说明原因
          atts: attachments.decorate(doc.attachments),
          attNote: attachments.note(doc.attachments),
          // 已变更的截止（延期、调整、撤销）：不再当作有效截止，写明原因并链接到新通知
          changes: (doc.deadline_changes || []).map((c) => Object.assign({}, c, {
            dateText: `${cnDate(c.date)}${c.time ? ` ${c.time}` : ''}`,
          })),
        });
        allowShare(true);
        this.setData({ shareReady: true });
        this.syncReminded();
      })
      .catch((err) => this.setData({
        doc: null, due: null, dueNote: '', changes: [], reminded: false, atts: [], attNote: '', conds: [], versions: [], upstream: '',
        takenDown: err.status === 404 && err.message === '该文件已下架',
        error: err.status === 404 ? err.message : `数据未更新：${err.message}`,
      }))
      .then(() => { this.refreshing = false; this.setData({ loading: false }); });
  },

  toggleBody() {
    this.setData({ bodyOpen: !this.data.bodyOpen });
  },

  toggleSupport(e) {
    const label = e.currentTarget.dataset.label;
    const fields = this.data.doc && this.data.doc.fields;
    const evidence = fields && fields.support_evidence && fields.support_evidence[label];
    this.setData({
      supportExpanded: this.data.supportExpanded === label ? '' : label,
      supportEvidence: this.data.supportExpanded === label ? '' : (evidence || ''),
    });
  },

  // 这份文件有没有还没发出的截止提醒（以服务端为准）
  syncReminded() {
    const { doc } = this.data;
    if (!doc) return;
    remindedDocs().then((map) => this.setData({ reminded: !!map[doc.id] }));
  },

  // 跨日返回页面时重算剩余天数
  onShow() {
    if (api.watchStartup) api.watchStartup(this);
    const stamp = Date.now();
    if (this.lastShow === undefined || stamp - this.lastShow >= 1000) {
      this.load();
      this.lastShow = stamp;
    }
    this.recompute();
    this.syncReminded();
    watchDay(this, () => this.recompute());
  },

  onHide() {
    if (api.stopStartup) api.stopStartup(this);
    stopDay(this);
  },

  onUnload() {
    if (api.stopStartup) api.stopStartup(this);
    stopDay(this);
  },

  goBack() {
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  // 打开同组另一个版本的详情（当前这一版不跳转）
  openVersion(e) {
    const { id, current } = e.currentTarget.dataset;
    if (id && !current) wx.navigateTo({ url: `/pages/detail/index?id=${id}` });
  },

  copyVersion(e) {
    copyLink(e.currentTarget.dataset.url);
  },

  openNew(e) {
    const id = e.currentTarget.dataset.id;
    if (id) wx.navigateTo({ url: `/pages/detail/index?id=${id}` });
  },

  recompute() {
    const doc = this.data.doc;
    if (doc) this.setData({ due: dueInfo(doc.deadline), dls: Array.isArray(doc.deadlines) ? deadlineRows(doc.deadlines) : null });
  },

  // 截止提醒：一次性订阅消息，用户在微信弹窗里允许后才算订阅成功
  toggleRemind() {
    if (this.data.reminded) {
      wx.showToast({ title: '已订阅：截止前 3 天提醒你一次', icon: 'none' });
      return;
    }
    const { doc } = this.data;
    const due = dueInfo(doc && doc.deadline);
    this.setData({ due });
    if (!due || due.days < 3) {
      wx.showToast({ title: '离截止不到 3 天，来不及提前提醒了', icon: 'none' });
      return;
    }
    requestOnce('deadline', { type: 'deadline', docId: doc.id }).then((ok) => {
      if (!ok) return;
      this.setData({ reminded: true });
      wx.showToast({ title: '已订阅截止提醒', icon: 'none' });
    });
  },

  copy() {
    copyLink(this.data.doc && this.data.doc.url);
  },

  // 点附件：能打开的下载后用微信打开（右上角菜单可转发、保存）；不能打开的复制原文链接并说明原因
  openAttachment(e) {
    const att = this.data.atts[e.currentTarget.dataset.index];
    const { doc } = this.data;
    if (!att || !doc || this.openingAttachment) return;
    this.openingAttachment = true;
    const done = () => { this.openingAttachment = false; };
    attachments.open(att, doc.url).then(done, done);
  },

  // 导出本份：Excel 或 Word，含原文链接和数据说明
  exportDoc() {
    const { doc } = this.data;
    if (doc) exportDocs([doc.id], { label: doc.title.length > 30 ? `${doc.title.slice(0, 30)}…` : doc.title });
  },

  feedback() {
    const doc = this.data.doc;
    if (!doc) return;
    wx.navigateTo({ url: `/pages/feedback/index?id=${doc.id}&title=${encodeURIComponent(doc.title)}` });
  },
});
