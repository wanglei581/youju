// 协议定稿前（D-056）不开放的页面（关注、关注的新文件、纠错、我的数据），从分享或旧链接直接打开时显示：
// 写明暂未开放，带回首页的链接，不请求接口、不报错
Component({
  options: { addGlobalClass: true },
  methods: {
    goHome() {
      wx.switchTab({ url: '/pages/home/index' });
    },
  },
});
