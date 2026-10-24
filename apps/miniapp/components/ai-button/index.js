// 列表行上的"AI 解读"按钮（D-075）。三样都满足才显示：这一行能解读（接口每行带 ai.interpret，
// 是通知本身的属性，和 AI 开没开无关）、AI 对这个人开着、后台确认过且能处理（utils/ai-access.js）。
// 点了直接进解读页；没同意协议的，解读页先走同意。不能解读的不放按钮，也不在行上写原因（原因在详情页卡片里）。
// 显示与否变了会发 toggle 事件，列表行据此给按钮腾地方
const access = require('../../utils/ai-access.js');
const context = require('../../utils/tool-context.js');

// action：默认 interpret（"AI 解读"，进解读页）；clause 是从 AI 首页"条款对照"那一行来选通知时（"条款对照"，进对照页，
// 看这一行能不能做条款对照）。不认的当 interpret
const ACTIONS = {
  interpret: { label: 'AI 解读', flag: 'interpret', url: (props) => context.interpretUrl(props) },
  clause: { label: '条款对照', flag: 'clause', url: (props) => context.clauseUrl(props) },
};
const actionOf = (name) => (Object.prototype.hasOwnProperty.call(ACTIONS, name) ? ACTIONS[name] : ACTIONS.interpret);

Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    docId: { type: null, value: '' },
    docTitle: { type: String, value: '' },
    ai: { type: null, value: null },
    // 勾选导出时不显示，免得勾选时误点进去用掉次数（不叫 hidden：那是所有组件都有的通用属性）
    selecting: { type: Boolean, value: false },
    action: { type: String, value: 'interpret' },
  },
  data: { shown: false, label: ACTIONS.interpret.label },
  observers: {
    'ai, selecting, docId, action': function () { this.sync(); },
  },
  lifetimes: {
    attached() {
      this.alive = true;
      this.unsubscribe = access.subscribe(() => this.sync());
      this.sync();
      access.ensure();
    },
    detached() {
      this.alive = false;
      if (this.unsubscribe) this.unsubscribe();
    },
  },
  // 页面回到前台（从别的页面返回、切回来）：再问一次 AI 板块的状态，后台恢复了按钮能出来；
  // 多久真问一次由 utils/ai-access.js 的 ensure 管（Codex 小程序第 10 轮）
  pageLifetimes: {
    show() { access.ensure(); },
  },
  methods: {
    sync() {
      if (!this.alive) return;
      const { ai, selecting, docId, action } = this.properties;
      const act = actionOf(action);
      const shown = !selecting && !!docId && !!ai && ai[act.flag] === true && access.canOpen();
      if (shown === this.data.shown && act.label === this.data.label) return;
      const toggled = shown !== this.data.shown;
      this.setData({ shown, label: act.label });
      if (toggled) this.triggerEvent('toggle', { shown });
    },
    open() {
      if (!this.data.shown || this.navigating) return;
      this.navigating = true;
      wx.navigateTo({
        url: actionOf(this.properties.action).url({ docId: this.properties.docId, docTitle: this.properties.docTitle }),
        complete: () => { this.navigating = false; },
      });
    },
  },
});
