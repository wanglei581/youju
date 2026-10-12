// AI 内测入口（只上传体验版、不合并）：只有 capabilities.ai_advisor.enabled 严格为 true 才显示；
// 其他情况（关闭、不在白名单、未同意协议、请求失败）什么都不渲染，页面和原来一样。
const api = require('../../utils/api.js');
const context = require('../../utils/tool-context.js');

Component({
  properties: {
    blocked: { type: Boolean, value: false },
    bottomRpx: { type: Number, value: 0 },
    docId: { type: null, value: '' },
    docTitle: { type: String, value: '' },
  },
  data: { aiEnabled: false, navigating: false },
  lifetimes: {
    attached() { this.alive = true; this.load(); },
    detached() { this.alive = false; },
  },
  pageLifetimes: {
    show() { this.setData({ navigating: false }); this.load(); },
  },
  methods: {
    load() {
      // 同一次启动内共用缓存，不会每个页面都请求；AI 页面进入时会刷新。
      api.advisorCapabilities().then((res) => {
        if (this.alive) this.setData({ aiEnabled: !!res && !!res.ai_advisor && res.ai_advisor.enabled === true });
      }).catch(() => { if (this.alive) this.setData({ aiEnabled: false }); });
    },
    open() {
      if (!this.data.aiEnabled || this.properties.blocked || this.data.navigating) return;
      this.setData({ navigating: true });
      wx.navigateTo({ url: context.url(this.properties), complete: () => { if (this.alive) this.setData({ navigating: false }); } });
    },
  },
});
