// 通知详情标题卡里的一行"这份通知可以 AI 解读 ↓"（D-075 第六节第 2 条，创始人定了加）。
// 这份能解读（详情接口的 ai.interpret）、AI 对这个人开着、后台确认过且能处理时才显示；点了让详情页滚到
// "要点"下面的卡片（卡片位置不动）。显示时把这份记下来，AI 首页"刚才看的通知"用（utils/ai-access.js）
const access = require('../../utils/ai-access.js');

Component({
  options: { styleIsolation: 'apply-shared' },
  // 只要这几项，不把整份详情（含正文）传进组件
  properties: {
    docId: { type: null, value: '' },
    docTitle: { type: String, value: '' },
    issuer: { type: String, value: '' },
    date: { type: String, value: '' },
    ai: { type: null, value: null },
  },
  data: { shown: false },
  observers: {
    'docId, ai': function () { this.sync(); },
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
      const { docId, docTitle, issuer, date, ai } = this.properties;
      const shown = !!docId && !!ai && ai.interpret === true && access.canOpen();
      if (shown !== this.data.shown) this.setData({ shown });
      if (shown) access.rememberDoc({ id: docId, title: docTitle, issuer, date });
    },
    jump() {
      if (this.data.shown) this.triggerEvent('jump');
    },
  },
});
