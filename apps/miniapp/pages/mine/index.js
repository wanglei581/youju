const api = require('../../utils/api');
const config = require('../../utils/config');
const settings = require('../../utils/settings');
const selection = require('../../utils/selection');
const watches = require('../../utils/watches');
const exportsUtil = require('../../utils/exports');
const format = require('../../utils/format');
const app = getApp();

Page({
  data: { baseLabel: '', healthLabel: '尚未检测', healthTone: 'busy', checking: false, watchCount: 0, fileCount: 0, selectedCount: 0, version: config.APP_VERSION },
  onShow() { this.refresh(); },
  refresh() {
    const health = app.globalData.health;
    this.setData({
      baseLabel: settings.shortLabel(settings.getBaseUrl()),
      healthLabel: health && health.baseUrl === settings.getBaseUrl() ? (health.ok ? '已连接 · ' + format.dateTime(health.checkedAt) : '连接失败 · ' + health.message) : '尚未检测',
      healthTone: health && health.baseUrl === settings.getBaseUrl() ? (health.ok ? 'ok' : 'warn') : 'busy',
      watchCount: watches.list().length,
      fileCount: exportsUtil.history().length,
      selectedCount: selection.count(),
    });
  },
  onCheck() {
    if (this.data.checking) return;
    this.setData({ checking: true });
    api.checkHealth().then((result) => {
      app.setHealth(result);
      this.setData({ checking: false });
      this.refresh();
    });
  },
  onSettings() { wx.navigateTo({ url: '/pages/settings/index' }); },
  onSources() { wx.navigateTo({ url: '/pages/sources/index' }); },
  onAbout() { wx.navigateTo({ url: '/pages/about/index' }); },
});
