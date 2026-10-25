const { loginOpen } = require('../utils/consent.js');
const access = require('../utils/ai-access.js');
const pages = require('../utils/pages.js');

// 自定义底栏：未选中用线性图标，选中用面性图标（D-015）。
// 协议定稿前（D-056）不显示"关注"，只有首页、文件、我的三项。tab 是这一项在 app.json 底栏里的序号：
// 各页仍按这个序号 getTabBar().setData({ selected })，少了一项也不错位。
// AI 板块（D-075）：画在中间，序号是新加的 4（app.json 底栏第 5 项），其余四页的序号不变；
// 只在 api 说 AI 对这个人开着时显示（utils/ai-access.js），关着时底栏和原来一样；包里没有 AI 首页（过审版）时不显示。
// 图标是样式画的四角星（字体里没有合适的字形）：未选中空心、选中实心
const TABS = [
  { pagePath: '/pages/home/index', text: '首页', icon: 'home', iconOn: 'home-filled', tab: 0 },
  { pagePath: '/pages/follow/index', text: '关注', icon: 'notification', iconOn: 'notification-filled', tab: 1, login: true },
  { pagePath: '/pages/ai/index', text: 'AI', spark: true, tab: 4, ai: true },
  { pagePath: '/pages/files/index', text: '文件', icon: 'file', iconOn: 'file-filled', tab: 2 },
  { pagePath: '/pages/me/index', text: '我的', icon: 'user', iconOn: 'user-filled', tab: 3 },
];

function listFor(state) {
  return TABS.filter((item) => (!item.login || loginOpen()) && (!item.ai || (pages.has('ai') && state && state.visible === true)));
}

Component({
  data: {
    selected: 0,
    list: listFor(null),
  },
  lifetimes: {
    attached() {
      const sync = (state) => this.setData({ list: listFor(state) });
      sync(access.current());
      this.unsubscribe = access.subscribe(sync);
      access.ensure();
    },
    detached() {
      if (this.unsubscribe) this.unsubscribe();
    },
  },
  pageLifetimes: {
    show() { access.ensure(); },
  },
  methods: {
    switchTab(e) {
      wx.switchTab({ url: e.currentTarget.dataset.path });
    },
  },
});
