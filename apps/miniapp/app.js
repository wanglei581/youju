App({
  globalData: {
    // 自定义导航栏（首页、我的、查名单的蓝色顶部）用：状态栏高度、导航内容区高度、右上角胶囊占的宽度
    statusBarHeight: 20,
    navHeight: 44,
    menuWidth: 96,
  },

  onLaunch() {
    const config = require('./utils/config.js');
    if (config.cloudEnv && wx.cloud) wx.cloud.init({ env: config.cloudEnv });
    try {
      // 尺寸怎么算、电脑版怎么兜底见 utils/nav.js
      Object.assign(this.globalData, require('./utils/nav.js').navMetrics(wx.getWindowInfo(), wx.getMenuButtonBoundingClientRect()));
    } catch (e) {
      // 取不到时用默认值，页面照常显示
    }
    this.watchUpdate();
  },

  // 打开和回到前台时悄悄叫醒接口服务（它空闲约 25 分钟后缩到 0，再起来要 1 分钟到几分钟）：用户多半先看内容
  // （走常驻的 admin），等点同意、关注、纠错时它已经醒了。20 分钟内答复过就不再叫，见 utils/api.js 的 wake
  onShow() {
    try { require('./utils/api.js').prewarm(); } catch (e) { /* 叫不醒不影响使用：提交时会再叫 */ }
  },

  // 有新版本时提示重启：微信打开小程序时在后台下载新版本，下载好了问用户；选"稍后"就下次打开时用新版本
  watchUpdate() {
    if (!wx.getUpdateManager) return;
    try {
      const manager = wx.getUpdateManager();
      manager.onUpdateReady(() => {
        wx.showModal({
          title: '新版本已准备好',
          content: '重启小程序就能用新版本。',
          confirmText: '重启',
          cancelText: '稍后',
          success: (res) => { if (res.confirm) manager.applyUpdate(); },
        });
      });
    } catch (e) {
      // 取不到更新管理器时照常使用，下次打开时微信也会换成新版本
    }
  },
});
