// 通知详情页右下角的 AI 入口。
// - AI 正式版（D-066，对所有用户开放）：通知详情接口的 ai.open 为真、这份文件能做（ai.interpret 或 ai.clause）就显示，
//   不用先同意协议（点进去再引导同意）；名单、没有正文等做不了的文件不显示。
// - 白名单内测：capabilities.ai_advisor.enabled 严格为 true 才显示（文件做不了的同样不显示）。
// - 其他情况（关闭、不在白名单、未同意协议、请求失败）什么都不渲染，页面和原来一样。
// 旧后台的详情没有 ai 字段时，只按 capabilities 判断。
const api = require('../../utils/api.js');
const context = require('../../utils/tool-context.js');
const { FAB_TEXT } = require('../../utils/advisor.js');

Component({
  properties: {
    blocked: { type: Boolean, value: false },
    bottomRpx: { type: Number, value: 0 },
    docId: { type: null, value: '' },
    docTitle: { type: String, value: '' },
    ai: { type: null, value: null },
  },
  data: { aiEnabled: false, navigating: false, fabText: FAB_TEXT },
  observers: {
    ai() { this.refresh(); },
  },
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
        this.whitelisted = !!res && !!res.ai_advisor && res.ai_advisor.enabled === true;
        this.refresh();
      }).catch(() => { this.whitelisted = false; this.refresh(); });
    },
    refresh() {
      if (!this.alive) return;
      const ai = this.properties.ai;
      const known = !!ai && typeof ai === 'object';
      const eligible = !known || ai.interpret === true || ai.clause === true;
      const open = known && ai.open === true;
      this.setData({ aiEnabled: eligible && (open || this.whitelisted === true) });
    },
    open() {
      if (!this.data.aiEnabled || this.properties.blocked || this.data.navigating) return;
      this.setData({ navigating: true });
      // 直接进解读；这份文件只能做条款对照时进条款对照（10/17：不再先进工具页）
      const ai = this.properties.ai;
      const onlyClause = !!ai && typeof ai === 'object' && ai.interpret !== true && ai.clause === true;
      const url = onlyClause ? context.clauseUrl(this.properties) : context.interpretUrl(this.properties);
      wx.navigateTo({ url, complete: () => { if (this.alive) this.setData({ navigating: false }); } });
    },
  },
});
