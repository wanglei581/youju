// AI 解读（政策解读与材料清单）。直达也要 capabilities 开放才显示；正式版没同意协议时引导同意，连不上时给重试。
const api = require('../../utils/api.js');
const { createSession } = require('../../utils/ai-interpret.js');
const { internalNotice, allowedKinds, aiTitle, gateState, AI_LABEL, AI_INTERNAL, OFF_TEXT, gateText } = require('../../utils/advisor.js');
const context = require('../../utils/tool-context.js');
const access = require('../../utils/ai-access.js');
const { watchDay, stopDay } = require('../../utils/daywatch.js');

const DAY_TURNED = '已经过了 0 点，可以重新开始了。';

Page({
  data: { aiLabel: AI_LABEL, loading: true, outdated: false, aiEnabled: false, gate: 'off', gateText: OFF_TEXT, aiNotice: '', phase: '', progress: '', error: '', title: '', docId: '', taskId: '', checked: false, needsConsent: true, terms: [], result: null, canRetry: false, showTerms: false, clauseEnabled: false, resumable: false, errorCode: '' },
  onLoad(query = {}) {
    const doc = context.read(query) || {};
    // 从"我的记录"打开已有任务（例如换了手机）：须知按钮写"查看结果"，不像要新建。
    // viewing 不写进上面的 data：和 D-073 的 PR 改的是同一行，免得合并冲突。
    // 通知编号、标题一开始就放进页面数据：被拦下（没开始任务）时，"看原文"和联系客服也要用
    this.setData({ viewing: !!query.taskId, docId: doc.docId || '', title: doc.title || '' });
    this.session = createSession({ api, storage: wx, docId: doc.docId, title: doc.title, taskId: query.taskId,
      update: (value) => {
        if (value.unavailable) access.refresh();
        if (this.visible && this.data.aiEnabled) this.setData(value);
      } });
  },
  onShow() {
    this.visible = true;
    const epoch = this.epoch = (this.epoch || 0) + 1;
    wx.setNavigationBarTitle({ title: '工具' });
    // 今天的次数用完、全站满了时停在这一页跨过零点：不自己重新开始（会直接提交、用掉次数），
    // 改写"可以重新开始了"，点了再问（第 18 轮）
    this.setData({ dayTurned: false });
    watchDay(this, () => {
      if (this.visible && (this.data.gate === 'used' || this.data.gate === 'full')) this.setData({ dayTurned: true, gateText: DAY_TURNED });
    });
    this.setData({ loading: true, aiEnabled: false });
    api.advisorCapabilities({ refresh: true }).then((res) => {
      if (!this.visible || epoch !== this.epoch) return;
      // 能力接口说的能不能处理和 AI 板块记的对不上：让板块马上重新问，列表上的按钮跟着变（第 11 轮）
      access.reconcile(res);
      const ai = res.ai_advisor || {};
      const aiEnabled = ai.enabled === true && allowedKinds(ai).includes('interpret');
      if (aiEnabled) wx.setNavigationBarTitle({ title: aiTitle('AI 解读') });
      // D-075：新提交前先看后台能不能处理、全站今天满了没有（打开已有的结果不拦）
      // 自己今天的次数用完了也先拦下，不发注定被拒的任务
      const used = aiEnabled && Number((ai.remaining || {}).interpret) === 0;
      const blocked = aiEnabled && !this.data.viewing && (ai.available === false || ai.siteFull === true || used);
      if (blocked) {
        const gate = ai.available === false ? 'paused' : ai.siteFull === true ? 'full' : 'used';
        this.setData({ loading: false, aiEnabled: false, gate,
          gateText: {
            paused: 'AI 暂时不可用，请稍后再试。',
            full: '今天 AI 的总用量满了，明天 0 点恢复；可以先看原文。',
            used: '今天的AI 解读 5 次用完了，明天 0 点恢复；以前的结果在“我的记录”里照样能看。',
          }[gate] });
        return;
      }
      const gate = aiEnabled ? '' : gateState(res);
      // AI 对这个人没开着（关闭、不在白名单；旧链接、旧记录打开）：正式版回首页，不留一页"不可用"（D-075）；
      // 内测包照旧只写"暂时没有可用工具"
      if (gate === 'off' && !AI_INTERNAL) {
        // 底栏、列表上的入口也跟着重新问一次（关了或被移出白名单时随即收起）
        access.refresh();
        this.setData({ aiEnabled: false, gate }); wx.switchTab({ url: '/pages/home/index' }); return;
      }
      this.setData({ loading: false, aiEnabled, gate, gateText: gateText(gate, res), aiNotice: aiEnabled ? internalNotice(ai.disclosure) : '',
        clauseEnabled: aiEnabled && allowedKinds(ai).includes('clause') });
      if (aiEnabled) this.session.show();
    }).catch(() => {
      if (this.visible && epoch === this.epoch) this.setData({ loading: false, aiEnabled: false, gate: 'off', gateText: OFF_TEXT });
    });
  },
  // 正式版：没同意协议的先去同意，回来自动刷新；连不上的重试
  consent() {
    if (this.data.gate !== 'consent') return;
    require('../../utils/consent.js').ensureConsent({ purpose: 'ai' }).then((ok) => { if (ok && this.visible) this.onShow(); });
  },
  // 连不上、暂时不可用：重试就重新问能力接口（停在这一页时后台恢复了，点一下就能用，第 11 轮）
  reload() { if (this.data.gate === 'offline' || this.data.gate === 'paused' || this.data.dayTurned) this.onShow(); },
  // 今天的次数用完了：去看以前的结果
  records() { if (this.data.gate === 'used') wx.navigateTo({ url: '/pages/advisor-tasks/index' }); },
  onHide() { this.visible = false; this.epoch = (this.epoch || 0) + 1; stopDay(this); this.session.hide(); },
  onUnload() { this.onHide(); },
  check(e) { this.session.check((e.detail.value || []).includes('agree')); },
  agree() { if (this.data.aiEnabled) this.session.agree(); },
  // 使用须知只有一个"我知道了，开始解读"按钮（10/17）；打开已有任务时写"我知道了，查看结果"
  start() { if (this.data.aiEnabled) this.session.start(); },
  toggleTerms() { this.setData({ showTerms: !this.data.showTerms }); },
  // 解读结果下面的可选下一步：和我的企业对一下
  clause() {
    if (this.data.aiEnabled && this.data.clauseEnabled && this.data.docId) {
      wx.navigateTo({ url: context.clauseUrl({ docId: this.data.docId, docTitle: this.data.title }) });
    }
  },
  retry() { if (this.data.aiEnabled && this.data.canRetry) this.session.retry(); },
  // 看原文：只是打开通知详情，被拦下时也能看
  original() { if (this.data.docId) wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(this.data.docId)}` }); },
  feedback() {
    if (this.data.aiEnabled && this.data.docId) wx.navigateTo({ url: `/pages/feedback/index?id=${encodeURIComponent(this.data.docId)}&title=${encodeURIComponent(this.data.title)}` });
  },
  tasks() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/advisor-tasks/index' }); },
  report() { if (this.data.aiEnabled) wx.navigateTo({ url: '/pages/complaint/index' }); },
});
