// 列表行上的"AI 解读"按钮（D-075）。三样都满足才显示：这一行能解读（接口每行带 ai.interpret，
// 是通知本身的属性，和 AI 开没开无关）、AI 对这个人开着、后台确认过且能处理（utils/ai-access.js）。
// 点了直接进解读页；没同意协议的，解读页先走同意。不能解读的不放按钮，也不在行上写原因（原因在详情页卡片里）。
// 显示与否变了会发 toggle 事件，列表行据此给按钮腾地方
const access = require('../../utils/ai-access.js');
const context = require('../../utils/tool-context.js');

Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    docId: { type: null, value: '' },
    docTitle: { type: String, value: '' },
    ai: { type: null, value: null },
    // 勾选导出时不显示，免得勾选时误点进去用掉次数
    hidden: { type: Boolean, value: false },
  },
  data: { shown: false },
  observers: {
    'ai, hidden, docId': function () { this.sync(); },
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
  methods: {
    sync() {
      if (!this.alive) return;
      const { ai, hidden, docId } = this.properties;
      const shown = !hidden && !!docId && !!ai && ai.interpret === true && access.canOpen();
      if (shown === this.data.shown) return;
      this.setData({ shown });
      this.triggerEvent('toggle', { shown });
    },
    open() {
      if (!this.data.shown || this.navigating) return;
      this.navigating = true;
      wx.navigateTo({
        url: context.interpretUrl({ docId: this.properties.docId, docTitle: this.properties.docTitle }),
        complete: () => { this.navigating = false; },
      });
    },
  },
});
