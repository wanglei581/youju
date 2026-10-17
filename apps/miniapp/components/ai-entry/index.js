// 通知详情页"要点"下面的 AI 解读卡片（10/17 创始人定：取代右下角浮动按钮。浮动按钮压在正文上，
// 微信 style v2 的 button 又不认宽度，被撑成大椭圆；而且做不了的文件直接没有按钮，看不懂规则）。
// - AI 正式版（D-066，对所有用户开放）：通知详情接口的 ai.open 为真就显示卡片；
// - 白名单内测：capabilities.ai_advisor.enabled 严格为 true 才显示；
// - 其他情况（关闭、不在白名单、未同意协议、请求失败）什么都不渲染，页面和原来一样。
// 这份文件能做（ai.interpret / ai.clause）就给"开始解读"；做不了也显示卡片并写明原因，
// 原因按页面已有的正文信息判断（不改后台）。旧后台的详情没有 ai 字段时按能做处理，进页面后后端会再判一次。
const api = require('../../utils/api.js');
const context = require('../../utils/tool-context.js');

// 和后端 interpret.eligible_body 的正文上限一致
const MAX_BODY = 24000;

function reasonOf(bodyStatus, bodyTotal) {
  if (bodyStatus === 'list') return '名单类文件不提供 AI 解读。要查企业在不在名单上，可以用“查名单”。';
  if (bodyStatus === 'original') return '这类文件不提供 AI 解读，请复制原文链接查看原文。';
  if (bodyStatus === 'ok' && Number(bodyTotal) > MAX_BODY) return '正文超过 2.4 万字，暂不能解读，请查看原文。';
  if (bodyStatus !== 'ok') return '这份文件没有可公开的完整正文，暂不能解读，请查看原文。';
  return '这份文件暂不能解读，请查看原文。';
}

Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    blocked: { type: Boolean, value: false },
    docId: { type: null, value: '' },
    docTitle: { type: String, value: '' },
    ai: { type: null, value: null },
    bodyStatus: { type: String, value: '' },
    bodyTotal: { type: Number, value: 0 },
  },
  data: { aiEnabled: false, available: false, interpretAvailable: false, clauseAvailable: false, reason: '', navigating: false },
  observers: {
    ai() { this.refresh(); },
    'bodyStatus, bodyTotal, blocked'() { this.refresh(); },
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
      const open = known && ai.open === true;
      const interpretAvailable = !known || ai.interpret === true;
      const clauseAvailable = known && ai.clause === true;
      const available = interpretAvailable || clauseAvailable;
      this.setData({
        aiEnabled: !this.properties.blocked && (open || this.whitelisted === true),
        available, interpretAvailable, clauseAvailable,
        reason: available ? '' : reasonOf(this.properties.bodyStatus, this.properties.bodyTotal),
      });
    },
    go(url) {
      if (!this.data.aiEnabled || this.properties.blocked || this.data.navigating) return;
      this.setData({ navigating: true });
      wx.navigateTo({ url, complete: () => { if (this.alive) this.setData({ navigating: false }); } });
    },
    // 能解读就进解读；这份文件只能做条款对照时进条款对照
    open() {
      if (!this.data.available) return;
      this.go(this.data.interpretAvailable ? context.interpretUrl(this.properties) : context.clauseUrl(this.properties));
    },
    openClause() {
      if (this.data.clauseAvailable) this.go(context.clauseUrl(this.properties));
    },
  },
});
