const { dueInfo, deadlineNote, dueView, kindClass, statusClass, highlight, ISSUER_WRAP, regionText } = require('../../utils/format.js');
const { copyLink } = require('../../utils/copy.js');
const { groupLine } = require('../../utils/repost.js');

// 列表行：类型、状态、地区、截止 → 标题 → 发文机关和日期 + 复制原文链接
Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    doc: { type: Object, value: {} },
    last: { type: Boolean, value: false },
    q: { type: String, value: '' },
    label: { type: String, value: '' },
    // 页面在 onShow 时传入当天日期，跨日返回页面会重新计算剩余天数
    today: { type: String, value: '' },
    // 选择模式（如搜索页勾选导出）：点行不打开详情，由外层处理勾选
    selecting: { type: Boolean, value: false },
  },
  data: { segs: [], due: null, dueNote: '', excerpt: null, kindCls: '', statusCls: '', statusText: '', issuerLong: false,
    groupText: '', groupOpen: false, regionText: '', aiShown: false },
  observers: {
    'doc, q, today': function (doc, q) {
      if (!doc || !doc.title) return;
      const status = doc.status;
      const appStatus = doc.app_status;
      const statusText = appStatus && typeof appStatus === 'object'
        ? ({ open: '征集中', closing: '即将截止', closed: '已截止' }[appStatus.code] || '') : status;
      const statusCls = appStatus && typeof appStatus === 'object' ? appStatus.code : statusClass(status);
      // 接口带了 due（D-061 首发版）：核对过的写日期；机器摘录的写日期、另起一行附原句；其余只写"截止未核对"
      const view = Object.prototype.hasOwnProperty.call(doc, 'due') && doc.kind === '申报通知' ? dueView(doc.due) : undefined;
      let due = dueInfo(doc.deadline);
      let dueNote = deadlineNote(doc);
      let excerpt = null;
      if (view !== undefined) {
        due = view && view.source !== 'unverified' ? { days: view.days, tone: view.tone, text: view.text } : null;
        dueNote = due ? '' : (view || doc.dueSoonUnverified ? '截止未核对，请以原文为准' : '截止时间见原文');
        excerpt = view && view.source === 'excerpt' ? { label: view.label, quote: view.quote } : null;
      }
      this.setData({
        segs: highlight(doc.title, q),
        due,
        dueNote,
        excerpt,
        kindCls: kindClass(doc.kind),
        statusCls,
        statusText,
        // 行上有"AI 解读"按钮时，发文机关另起一行，给日期、复制链接和按钮腾地方
        issuerLong: (doc.issuer || '').length > (this.data.aiShown ? 0 : ISSUER_WRAP),
        // 地区照录服务端写法；来源带区县而地区文字里没写时补上区县
        regionText: regionText(doc),
        // 转发版本（D-055）：同一份部里通知的其他地方版本，点开逐个列出
        groupText: groupLine(doc.group),
      });
    },
    // 行上的"AI 解读"按钮出现或收起时，重排发文机关那一行
    aiShown(aiShown) {
      const doc = this.data.doc;
      if (!doc || !doc.title) return;
      this.setData({ issuerLong: (doc.issuer || '').length > (aiShown ? 0 : ISSUER_WRAP) });
    },
    // 换了一条（翻页、重新搜索）时收起
    'doc.id': function () {
      if (this.data.groupOpen) this.setData({ groupOpen: false });
    },
  },
  methods: {
    open() {
      if (this.data.selecting) return;
      wx.navigateTo({ url: `/pages/detail/index?id=${this.data.doc.id}` });
    },
    copy() {
      copyLink(this.data.doc.url);
    },
    aiToggle(e) {
      this.setData({ aiShown: !!(e.detail && e.detail.shown) });
    },
    toggleGroup() {
      if (this.data.selecting) return;
      this.setData({ groupOpen: !this.data.groupOpen });
    },
    // 打开某个转发版本自己的详情（截止、原文都是那一版自己的）
    openVersion(e) {
      if (this.data.selecting) return;
      wx.navigateTo({ url: `/pages/detail/index?id=${e.currentTarget.dataset.id}` });
    },
    copyVersion(e) {
      copyLink(e.currentTarget.dataset.url);
    },
  },
});
