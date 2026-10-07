const { loginOpen } = require('../utils/consent.js');

// 自定义底栏：未选中用线性图标，选中用面性图标（D-015）。
// 协议定稿前（D-056）不显示"关注"，只有首页、文件、我的三项。tab 是这一项在 app.json 底栏里的序号：
// 各页仍按这个序号 getTabBar().setData({ selected })，少了一项也不错位
const TABS = [
  { pagePath: '/pages/home/index', text: '首页', icon: 'home', iconOn: 'home-filled', tab: 0 },
  { pagePath: '/pages/follow/index', text: '关注', icon: 'notification', iconOn: 'notification-filled', tab: 1, login: true },
  { pagePath: '/pages/files/index', text: '文件', icon: 'file', iconOn: 'file-filled', tab: 2 },
  { pagePath: '/pages/me/index', text: '我的', icon: 'user', iconOn: 'user-filled', tab: 3 },
];

Component({
  data: {
    selected: 0,
    list: TABS.filter((item) => !item.login || loginOpen()),
  },
  methods: {
    switchTab(e) {
      wx.switchTab({ url: e.currentTarget.dataset.path });
    },
  },
});
