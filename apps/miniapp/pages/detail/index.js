const api = require('../../utils/api.js');
const { dueInfo, deadlineNote, cnDate, weekday, kindClass } = require('../../utils/format.js');
const { copyLink } = require('../../utils/copy.js');
const { requestOnce } = require('../../utils/subscribe.js');
const { remindedDocs } = require('../../utils/reminders.js');
const { loginOpen } = require('../../utils/consent.js');
const { exportDocs } = require('../../utils/exporter.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');
const attachments = require('../../utils/attachments.js');

// 正文默认显示的字数：在这个长度附近的换行处截断，读起来不断在半句
const BODY_PREVIEW = 600;

function previewOf(text) {
  if (text.length <= BODY_PREVIEW) return text;
  const cut = text.lastIndexOf('\n', BODY_PREVIEW);
  return `${text.slice(0, cut > BODY_PREVIEW - 200 ? cut : BODY_PREVIEW)}……`;
}

// 申报条件：接口只给人工核对、整份确认过的条目；同一类别连着的几条只在第一条写类别
function conditionRows(doc) {
  const items = (doc && doc.apply_conditions && doc.apply_conditions.items) || [];
  return items.map((item, index) => ({
    category: item.category,
    text: item.text,
    quote: item.quote,
    open: false,
    showCat: index === 0 || items[index - 1].category !== item.category,
  }));
}

Page({
  data: { doc: null, due: null, dueNote: '', dueDate: '', dueWeek: '', changes: [], tagClasses: [], reminded: false, error: '',
    bodyOpen: false, bodyFolded: false, bodyPreview: '', bodyRest: 0, supportExpanded: '', supportEvidence: '', conds: [],
    atts: [], attNote: '' },

  onLoad(query) {
    // 协议定稿前（D-056）不显示"订阅截止提醒"和底栏的"纠错"，定稿后照旧
    this.setData({ loginOpen: loginOpen() });
    this.docId = query.id;
    this.lastShow = Date.now();
    this.load();
  },

  // 网络不好加载失败时，页面上点"重试"再加载一次
  load() {
    if (this.refreshing) return;
    this.refreshing = true;
    this.setData({ loading: true, error: '', takenDown: false });
    api.detail(this.docId)
      .then((doc) => {
        // 只有人工核对过的截止日期才显示（合规红线）
        const due = dueInfo(doc.deadline);
        const body = doc.body && doc.body.status === 'ok' ? doc.body : null;
        this.setData({
          doc,
          // 正文默认只显示开头一段；比这长的才给"展开全文"
          bodyOpen: false,
          bodyFolded: !!body && body.text.length > BODY_PREVIEW,
          bodyPreview: body ? previewOf(body.text) : '',
          bodyRest: body ? body.total - body.text.length : 0,
          supportExpanded: '',
          supportEvidence: '',
          conds: conditionRows(doc),
          due,
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
        this.syncReminded();
      })
      .catch((err) => this.setData({
        doc: null, due: null, dueNote: '', changes: [], reminded: false, atts: [], attNote: '', conds: [],
        takenDown: err.status === 404 && err.message === '该文件已下架',
        error: err.status === 404 ? err.message : `数据未更新：${err.message}`,
      }))
      .finally(() => { this.refreshing = false; this.setData({ loading: false }); });
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

  // 点一条申报条件：展开或收起它的原文
  toggleCond(e) {
    const index = Number(e.currentTarget.dataset.index);
    const row = this.data.conds[index];
    if (!row) return;
    const conds = this.data.conds.slice();
    conds[index] = Object.assign({}, row, { open: !row.open });
    this.setData({ conds });
  },

  // 这份文件有没有还没发出的截止提醒（以服务端为准）
  syncReminded() {
    const { doc } = this.data;
    if (!doc) return;
    remindedDocs().then((map) => this.setData({ reminded: !!map[doc.id] }));
  },

  // 跨日返回页面时重算剩余天数
  onShow() {
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
    stopDay(this);
  },

  onUnload() {
    stopDay(this);
  },

  goBack() {
    wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
  },

  openNew(e) {
    const id = e.currentTarget.dataset.id;
    if (id) wx.navigateTo({ url: `/pages/detail/index?id=${id}` });
  },

  recompute() {
    if (this.data.doc) this.setData({ due: dueInfo(this.data.doc.deadline) });
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
