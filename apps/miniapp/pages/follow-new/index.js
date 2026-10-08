const api = require('../../utils/api.js');
const { todayISO } = require('../../utils/format.js');
const { foldNote } = require('../../utils/conditions.js');
const { loginOpen } = require('../../utils/consent.js');

const PAGE = 20;

// 某个关注上次查看之后的新文件；看完可以全部标为已读
Page({
  // 资讯与指南（与申报无关的新闻、会议、招聘、许可等）和搜索页一样默认折叠，写明折叠了几篇
  // gated：服务端返回 403（还没同意用户协议和隐私保护指引，或协议已更新），页面显示说明卡而不是加载失败
  // loginOpen 为 false（协议定稿前，D-056）时只显示"暂未开放"，不请求接口
  data: { id: '', name: '', items: [], total: 0, folded: 0, loading: true, error: '', gated: false, today: '', showGuides: false, fold: { text: '', link: '' }, loginOpen: loginOpen() },

  onLoad(query) {
    if (!this.data.loginOpen) return;
    const name = query.name ? decodeURIComponent(query.name) : '';
    this.setData({ id: query.id, name, today: todayISO() });
    if (name) wx.setNavigationBarTitle({ title: name });
    this.load(true);
  },

  load(reset) {
    if (this.fetching) return;
    this.fetching = true;
    const offset = reset ? 0 : this.data.items.length;
    this.setData({ loading: true, error: '', gated: false });
    api.followNew(this.data.id, { limit: PAGE, offset, fold: this.data.showGuides ? 0 : 1 })
      .then((res) => this.setData({
        items: reset ? res.items : this.data.items.concat(res.items),
        total: res.total,
        folded: res.folded || 0,
        fold: foldNote(res.folded || 0, this.data.showGuides ? (res.guides || 0) : 0),
        loading: false,
      }))
      .catch((err) => this.setData(err.status === 403
        ? { loading: false, gated: true, items: [], total: 0, folded: 0 }
        : { loading: false, error: `加载失败：${err.message}` }))
      .then(() => { this.fetching = false; });
  },

  goMe() {
    wx.switchTab({ url: '/pages/me/index' });
  },

  toggleGuides() {
    this.setData({ showGuides: !this.data.showGuides });
    this.load(true);
  },

  onReachBottom() {
    if (this.data.items.length < this.data.total) this.load(false);
  },

  markSeen() {
    api.seenFollow(this.data.id)
      .then(() => {
        wx.showToast({ title: '已全部标为已读', icon: 'none' });
        setTimeout(() => wx.navigateBack(), 800);
      })
      .catch((err) => wx.showToast({ title: err.message || '操作失败，请稍后再试', icon: 'none' }));
  },
});
