// 自定义底栏：未选中用线性图标，选中用面性图标（D-015）
Component({
  data: {
    selected: 0,
    list: [
      { pagePath: '/pages/home/index', text: '首页', icon: 'home', iconOn: 'home-filled' },
      { pagePath: '/pages/follow/index', text: '关注', icon: 'notification', iconOn: 'notification-filled' },
      { pagePath: '/pages/files/index', text: '文件', icon: 'file', iconOn: 'file-filled' },
      { pagePath: '/pages/me/index', text: '我的', icon: 'user', iconOn: 'user-filled' },
    ],
  },
  methods: {
    switchTab(e) {
      const { index } = e.currentTarget.dataset;
      wx.switchTab({ url: this.data.list[index].pagePath });
    },
  },
});
