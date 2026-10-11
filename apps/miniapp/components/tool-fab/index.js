const api = require('../../utils/api.js');
const { loginOpen } = require('../../utils/consent.js');
const context = require('../../utils/tool-context.js');
const overlays = require('../../utils/tool-overlays.js');

Component({
  properties: {
    blocked: { type: Boolean, value: false },
    tabPage: { type: Boolean, value: false },
    bottomRpx: { type: Number, value: 0 },
    bottomSelector: { type: String, value: '' },
    docId: { type: null, value: '' },
    docTitle: { type: String, value: '' },
    source: { type: String, value: '' },
    keyword: { type: String, value: '' },
    kind: { type: String, value: 'all' },
    filters: { type: Object, value: null },
  },
  data: { loginOpen: loginOpen(), enabled: false, aiEnabled: false, visible: false, keyboard: false, overlay: false, barHeight: '', navigating: false },
  observers: {
    blocked() { if (this.data.visible) this.measure(); },
  },
  lifetimes: {
    attached() {
      this.alive = true;
      this.unsubscribe = overlays.subscribe((overlay) => this.setData({ overlay }));
      this.keyboardChanged = (event) => this.setData({ keyboard: this.data.visible && event.height > 0 });
      this.resized = () => this.measure();
      wx.onKeyboardHeightChange(this.keyboardChanged);
      wx.onWindowResize(this.resized);
    },
    ready() { this.show(); },
    detached() {
      this.alive = false;
      this.unsubscribe();
      wx.offKeyboardHeightChange(this.keyboardChanged);
      wx.offWindowResize(this.resized);
    },
  },
  pageLifetimes: {
    show() { this.show(); },
    hide() { this.setData({ visible: false, keyboard: false, navigating: false }); },
    resize() { this.measure(); },
  },
  methods: {
    show() {
      if (!this.data.loginOpen) return;
      this.setData({ visible: true, navigating: false });
      this.measure();
      this.load();
      api.advisorStatus().then((res) => {
        if (this.alive) this.setData({ enabled: res.enabled === true });
      }).catch(() => { if (this.alive) this.setData({ enabled: false }); });
    },
    load() {
      // 同一次启动内共用缓存，不会每个页面都请求；AI 页面进入时会刷新。
      api.advisorCapabilities().then((res) => {
        if (this.alive) this.setData({ aiEnabled: !!res && !!res.ai_advisor && res.ai_advisor.enabled === true });
      }).catch(() => { if (this.alive) this.setData({ aiEnabled: false }); });
    },
    measure() {
      const pages = getCurrentPages(); const page = pages[pages.length - 1];
      if (!page) return;
      const owner = this.properties.tabPage && page.getTabBar ? page.getTabBar() : page;
      const selector = this.properties.tabPage ? '.bar' : this.properties.bottomSelector;
      if (!selector || !owner) return;
      const query = owner.createSelectorQuery ? owner.createSelectorQuery() : wx.createSelectorQuery().in(owner);
      query.select(selector).boundingClientRect((rect) => {
        if (!this.alive || !rect) return;
        const win = wx.getWindowInfo();
        const safe = win.safeArea ? Math.max(0, win.screenHeight - win.safeArea.bottom) : 0;
        // 底栏本身已含安全区，先扣除，再由 CSS env 加一次。
        this.setData({ barHeight: `${Math.max(0, rect.height - safe)}px` });
      }).exec();
    },
    open() {
      if (!(this.data.enabled || this.data.aiEnabled) || !this.data.visible || this.data.keyboard || this.data.overlay || this.properties.blocked || this.data.navigating) return;
      this.setData({ navigating: true });
      wx.navigateTo({ url: context.url(this.properties), complete: () => { if (this.alive) this.setData({ navigating: false }); } });
    },
  },
});
