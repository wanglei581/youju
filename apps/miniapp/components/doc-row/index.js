const { dueTone, kindClass, statusClass, highlight, ISSUER_WRAP } = require('../../utils/format.js');
const { copyLink } = require('../../utils/copy.js');

// 列表行：类型、状态、地区、截止 → 标题 → 发文机关和日期 + 复制原文链接
Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    doc: { type: Object, value: {} },
    last: { type: Boolean, value: false },
    q: { type: String, value: '' },
    label: { type: String, value: '' },
  },
  data: { segs: [], tone: '', kindCls: '', statusCls: '', issuerLong: false },
  observers: {
    'doc, q': function (doc, q) {
      if (!doc || !doc.title) return;
      this.setData({
        segs: highlight(doc.title, q),
        tone: doc.deadline ? dueTone(doc.deadline.days) : '',
        kindCls: kindClass(doc.kind),
        statusCls: statusClass(doc.status),
        issuerLong: (doc.issuer || '').length > ISSUER_WRAP,
      });
    },
  },
  methods: {
    open() {
      wx.navigateTo({ url: `/pages/detail/index?id=${this.data.doc.id}` });
    },
    copy() {
      copyLink(this.data.doc.url);
    },
  },
});
