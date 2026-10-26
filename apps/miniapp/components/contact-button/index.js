// 联系客服（D-075 第七节）：用微信自带的客服按钮（open-type="contact"），按钮上只写"联系客服"。
// 按钮下写回复时间（创始人定：每天有人看、周末也看）和客服管什么，免得进了会话第一句就问"我们能报吗"。
// 鸿蒙版微信不支持客服按钮（平台标识 ohos、ohos_pc）：换成一行字，给使用帮助和协议里写的联系邮箱，不编造电话或微信号。
// session-from 只带排查要用的短标识：从哪一页来、通知编号、出错原因代码；不带企业情况、客户名单、工具和解读的结果、任务编号。
// 在某份通知的页面上点：打开消息卡片，标题是通知标题、路径是这份通知的详情页（默认卡片不一定指向详情页），
// 进了会话发不发由用户自己定。variant="row" 是"我的"页列表里的一行
const legal = require('../../content/legal.js');
const pages = require('../../utils/pages.js');

const MAX_FROM = 1024;
const SAFE = /^[A-Za-z0-9_-]{1,40}$/;
const DOC_ID = /^\d{1,12}$/;
const CARD_TITLE = 60;

function platform() {
  try {
    if (typeof wx.getDeviceInfo === 'function') return String(wx.getDeviceInfo().platform || '');
    return String(wx.getSystemInfoSync().platform || '');
  } catch (e) { return ''; }
}

function sessionFrom({ from, docId, reason }) {
  const parts = ['youju'];
  if (SAFE.test(String(from || ''))) parts.push(`from=${from}`);
  if (DOC_ID.test(String(docId || ''))) parts.push(`doc=${docId}`);
  if (SAFE.test(String(reason || ''))) parts.push(`reason=${reason}`);
  return parts.join('|').slice(0, MAX_FROM);
}

Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    from: { type: String, value: '' },
    docId: { type: null, value: '' },
    docTitle: { type: String, value: '' },
    reason: { type: String, value: '' },
    variant: { type: String, value: 'block' },
  },
  // help：包里有使用帮助页才给"看使用帮助"（过审版首发 16 页没有，D-091），没有时只给邮箱
  data: { supported: true, help: pages.has('help'), sessionFrom: 'youju', showCard: false, cardTitle: '', cardPath: '', email: '' },
  observers: {
    'from, docId, docTitle, reason': function () { this.sync(); },
  },
  lifetimes: {
    attached() { this.sync(); },
  },
  methods: {
    sync() {
      const { from, docId, docTitle, reason } = this.properties;
      const doc = DOC_ID.test(String(docId || '')) ? String(docId) : '';
      const title = String(docTitle || '').trim().slice(0, CARD_TITLE);
      this.setData({
        supported: !/^ohos/i.test(platform()),
        sessionFrom: sessionFrom({ from, docId: doc, reason }),
        showCard: !!(doc && title),
        cardTitle: doc && title ? title : '',
        cardPath: doc && title ? `/pages/detail/index?id=${doc}` : '',
        email: (legal.operator && legal.operator.email) || '',
      });
    },
    help() {
      if (this.data.help) wx.navigateTo({ url: '/pages/help/index' });
    },
    // 过审版没有使用帮助页：去"关于有据"看联系邮箱（那里点一下能复制）
    about() {
      wx.navigateTo({ url: '/pages/about/index' });
    },
  },
});
