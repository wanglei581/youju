const { loginOpen } = require('../utils/consent.js');
// first-release-cut:start
const access = require('../utils/ai-access.js');
const pages = require('../utils/pages.js');
// first-release-cut:end

// 自定义底栏：未选中用线性图标，选中用面性图标（D-015）。
// 协议定稿前（D-056）不显示"关注"，只有首页、导出、我的三项。tab 是这一项在 app.json 底栏里的序号：
// 各页仍按这个序号 getTabBar().setData({ selected })，少了一项也不错位。
const TABS = [
  { pagePath: '/pages/home/index', text: '首页', icon: 'home', iconOn: 'home-filled', tab: 0 },
  { pagePath: '/pages/follow/index', text: '关注', icon: 'notification', iconOn: 'notification-filled', tab: 1, login: true },
  // first-release-cut:start
  // AI 板块（D-075）：画在中间，序号是新加的 4（app.json 底栏第 5 项），其余四页的序号不变；
  // 只在 api 说 AI 对这个人开着时显示（utils/ai-access.js），关着时底栏和原来一样；包里没有 AI 首页（过审版）时不显示。
  // 图标是样式画的四角星（字体里没有合适的字形）：未选中空心、选中实心
  { pagePath: '/pages/ai/index', text: 'AI', spark: true, tab: 4, ai: true },
  // first-release-cut:end
  // D-087：这一格叫"导出"（原"文件"）——全小程序"文件"都指通知、名单、政策，这一格是导出记录
  { pagePath: '/pages/files/index', text: '导出', icon: 'file', iconOn: 'file-filled', tab: 2 },
  { pagePath: '/pages/me/index', text: '我的', icon: 'user', iconOn: 'user-filled', tab: 3 },
];

// 这一格这次显不显示：要登录的看协议定稿没有（D-056）
function shown(item, state) {
  if (item.login && !loginOpen()) return false;
  // first-release-cut:start
  if (item.ai) return pages.has('ai') && !!state && state.visible === true;
  // first-release-cut:end
  return true;
}

function listFor(state) {
  return TABS.filter((item) => shown(item, state));
}

Component({
  data: {
    selected: 0,
    list: listFor(null),
  },
  lifetimes: {
    attached() {
      // first-release-cut:start
      const sync = (state) => this.setData({ list: listFor(state) });
      sync(access.current());
      this.unsubscribe = access.subscribe(sync);
      access.ensure();
      // first-release-cut:end
    },
    detached() {
      if (this.unsubscribe) this.unsubscribe();
    },
  },
  pageLifetimes: {
    show() {
      // first-release-cut:start
      access.ensure();
      // first-release-cut:end
    },
  },
  methods: {
    switchTab(e) {
      wx.switchTab({ url: e.currentTarget.dataset.path });
    },
  },
});
