const { dueInfo, kindClass, statusClass, highlight, ISSUER_WRAP } = require('../../utils/format.js');
const { copyLink } = require('../../utils/copy.js');

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
  data: { segs: [], due: null, kindCls: '', statusCls: '', statusText: '', issuerLong: false },
  observers: {
    'doc, q, today': function (doc, q) {
      if (!doc || !doc.title) return;
      const status = doc.status;
      const appStatus = doc.app_status;
      const statusText = appStatus && typeof appStatus === 'object'
        ? ({ open: '征集中', closing: '即将截止', closed: '已截止' }[appStatus.code] || '') : status;
      const statusCls = appStatus && typeof appStatus === 'object' ? appStatus.code : statusClass(status);
      this.setData({
        segs: highlight(doc.title, q),
        due: dueInfo(doc.deadline),
        kindCls: kindClass(doc.kind),
        statusCls,
        statusText,
        issuerLong: (doc.issuer || '').length > ISSUER_WRAP,
      });
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
  },
});
