// 使用帮助（D-075）：固定文字（content/help.js），底部是联系客服和投诉举报。
// AI 那几条只在 AI 对这个人开着时显示（utils/ai-access.js），关着时页面没有 AI 字样
const help = require('../../content/help.js');
const access = require('../../utils/ai-access.js');
const { loginOpen } = require('../../utils/consent.js');

function itemsFor(state) {
  return help.items
    .filter((item) => (!item.ai || (state && state.visible === true && state.confirmed === true)) && (!item.login || loginOpen()))
    .map((item, index) => ({ key: index, q: item.q, a: item.a }));
}

Page({
  data: { items: itemsFor(null) },
  onLoad() {
    wx.setNavigationBarTitle({ title: help.title });
    this.setData({ items: itemsFor(access.current()) });
    this.unsubscribe = access.subscribe((state) => this.setData({ items: itemsFor(state) }));
    access.ensure();
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
