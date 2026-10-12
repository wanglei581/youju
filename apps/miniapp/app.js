App({
  globalData: {
    // 自定义导航栏（首页、我的、查名单的蓝色顶部）用：状态栏高度、导航内容区高度、右上角胶囊占的宽度
    advisorStatusPromise: null,
    advisorStatusPending: null,
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
  },

  onLaunch() {
    require('./utils/tool-overlays.js').install(wx);
    const config = require('./utils/config.js');
    if (config.cloudEnv && wx.cloud) wx.cloud.init({ env: config.cloudEnv });
    try {
      // 尺寸怎么算、电脑版怎么兜底见 utils/nav.js
      Object.assign(this.globalData, require('./utils/nav.js').navMetrics(wx.getWindowInfo(), wx.getMenuButtonBoundingClientRect()));
    } catch (e) {
      // 取不到时用默认值，页面照常显示
    }
  },
});
