// 通知详情页"要点"下面的 AI 解读卡片（10/17 创始人定：取代右下角浮动按钮。浮动按钮压在正文上，
// 微信 style v2 的 button 又不认宽度，被撑成大椭圆；而且做不了的文件直接没有按钮，看不懂规则）。
// 显不显示只听 api（D-075，utils/ai-access.js）：AI 对这个人开着、后台确认过才渲染；其余情况（关闭、
// 不在白名单、请求失败）什么都不渲染，页面和原来一样。后台暂时处理不了时卡片写"AI 暂时不可用"、不给按钮。
// 这份文件能做（详情接口的 ai.interpret / ai.clause，是文件本身的属性）就给"开始解读"；做不了也显示卡片，
// 按后台给的原因代码写明原因和下一步（utils/ai-reasons.js）。旧后台的详情没有 ai 字段时按能做处理，
// 进页面后后端会再判一次；旧后台有 ai 没有原因代码时按页面已有的正文信息判断。
const access = require('../../utils/ai-access.js');
const context = require('../../utils/tool-context.js');
const { reasonView } = require('../../utils/ai-reasons.js');
const { copyLink } = require('../../utils/copy.js');

// 和后端 interpret.eligible_body 的正文上限一致
const MAX_BODY = 24000;

function legacyReason(bodyStatus, bodyTotal) {
  if (bodyStatus === 'list') return 'list';
  if (bodyStatus === 'original') return 'kind';
  if (bodyStatus === 'ok' && Number(bodyTotal) > MAX_BODY) return 'too_long';
  if (bodyStatus !== 'ok') return 'no_body';
  return '';
}

Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    blocked: { type: Boolean, value: false },
    docId: { type: null, value: '' },
    docTitle: { type: String, value: '' },
    docUrl: { type: String, value: '' },
    hasAttachments: { type: Boolean, value: false },
    ai: { type: null, value: null },
    bodyStatus: { type: String, value: '' },
    bodyTotal: { type: Number, value: 0 },
  },
  data: { aiEnabled: false, paused: false, available: false, interpretAvailable: false, clauseAvailable: false, reason: '', next: '', nextText: '', navigating: false },
  observers: {
    ai() { this.refresh(); },
    'bodyStatus, bodyTotal, blocked, hasAttachments'() { this.refresh(); },
  },
  lifetimes: {
    attached() {
      this.alive = true;
      this.unsubscribe = access.subscribe(() => this.refresh());
      this.refresh();
      access.ensure();
    },
    detached() {
      this.alive = false;
      if (this.unsubscribe) this.unsubscribe();
    },
  },
  pageLifetimes: {
    show() { this.setData({ navigating: false }); access.ensure(); },
  },
  methods: {
    refresh() {
      if (!this.alive) return;
      const state = access.current();
      const ai = this.properties.ai;
      const known = !!ai && typeof ai === 'object';
      const interpretAvailable = !known || ai.interpret === true;
      const clauseAvailable = known && ai.clause === true;
      const available = interpretAvailable || clauseAvailable;
      const code = known && typeof ai.reason === 'string' && ai.reason ? ai.reason : legacyReason(this.properties.bodyStatus, this.properties.bodyTotal);
      const view = available ? { text: '', next: '', nextText: '' } : reasonView(code, this.properties.hasAttachments);
      this.setData({
        aiEnabled: !this.properties.blocked && state.visible === true && state.confirmed === true,
        paused: state.available === false,
        available, interpretAvailable, clauseAvailable,
        reason: view.text, next: view.next, nextText: view.nextText,
      });
    },
    go(url) {
      if (!this.data.aiEnabled || this.data.paused || this.properties.blocked || this.data.navigating) return;
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
    // 不能解读时的下一步：去查名单、复制原文链接、看附件（让详情页滚过去）、返回
    nextStep() {
      const { next } = this.data;
      if (next === 'lists') wx.navigateTo({ url: '/pages/lists/index' });
      else if (next === 'copy') copyLink(this.properties.docUrl);
      else if (next === 'attachments') this.triggerEvent('attachments');
      else if (next === 'back') wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/home/index' }) });
    },
  },
});
