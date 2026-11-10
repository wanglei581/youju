// 使用帮助（D-075）：固定文字（content/help.js），底部是联系客服和投诉举报。
// AI 那几条只在 AI 对这个人开着时显示（utils/ai-access.js），关着时页面没有 AI 字样
const help = require('../../content/help.js');
const access = require('../../utils/ai-access.js');
const { loginOpen } = require('../../utils/consent.js');

function itemsFor(state) {
  return help.items
    .filter((item) => (!item.ai || (state && state.visible === true && state.confirmed === true)) && (!item.login || loginOpen()))
    .map((item) => ({ key: item.key, q: item.q, a: item.a }));
}
// 从页面上的"说明 ›"进来（?topic=deadline）：滚到那一条、加一圈框；没有这一条就停在顶上
const TOPIC = /^[a-z-]{1,20}$/;

Page({
  data: { items: itemsFor(null), topic: '' },
  onLoad(query) {
    wx.setNavigationBarTitle({ title: help.title });
    const topic = query && TOPIC.test(String(query.topic || '')) ? String(query.topic) : '';
    this.setData({ items: itemsFor(access.current()), topic });
    this.unsubscribe = access.subscribe((state) => this.setData({ items: itemsFor(state) }));
    access.ensure();
  },
  onReady() {
    const { topic, items } = this.data;
    if (topic && items.some((item) => item.key === topic)) wx.pageScrollTo({ selector: `#help-${topic}`, duration: 0 });
  },
  // 回到前台时再问一次 AI 板块的状态（多久真问一次由 ensure 管，第 11 轮）
  onShow() {
    access.ensure();
  },
  onUnload() {
    if (this.unsubscribe) this.unsubscribe();
  },
  report() {
    wx.navigateTo({ url: '/pages/complaint/index' });
  },
});
