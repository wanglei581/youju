// 加量体验的确认页（D-070 ③）：点"开始体验"之前把规则写全（几天、不能暂停、每次多少家、每天几次、不用填付款信息、
// 结束后回到免费版、本月还剩几次免费、结果结束后照样能看能导出、每个微信一次），用户点了才起算。
// 开卖之前这里不提购买；规则和数字都以服务端为准（utils/boost.js 只拼文字）。
const api = require('../../utils/api.js');
const boost = require('../../utils/boost.js');

Page({
  data: { loading: true, loadError: '', error: '', available: false, active: false, starting: false,
    items: [], subtitle: '', startLabel: '', activeLine: '', note: '' },
  onShow() { this.dead = false; if (!this.data.starting) this.load(); },
  onUnload() { this.dead = true; },
  load() {
    const serial = this.serial = (this.serial || 0) + 1;
    this.setData({ loading: true, loadError: '', error: '' });
    api.advisorStatus().then((status) => {
      if (this.dead || serial !== this.serial) return;
      if (!status || status.enabled !== true) throw new Error('批量工具暂时不可用，请稍后重试');
      this.show(status.trial, boost.confirmItems(status));
    }).catch((err) => {
      if (!this.dead && serial === this.serial) this.setData({ loading: false, loadError: (err && err.message) || '没能读取，请重试' });
    });
  },
  show(trial, items) {
    const view = boost.trialView(trial);
    const available = view.trialAvailable && items.length > 0;
    this.setData({ loading: false, available, active: view.trialActive, items: available ? items : [],
      subtitle: view.trialHours ? `${view.trialHours / 24} 天内每次最多 ${view.trialRows} 家（免费）` : '',
      startLabel: `开始体验（立即起算 ${view.trialHours} 小时）`,
      activeLine: view.trialActive ? boost.trialLine(view) : '',
      note: available ? '' : boost.unavailableText(view) });
  },
  start() {
    if (this.data.starting || !this.data.available || this.dead) return;
    this.setData({ starting: true, error: '' });
    api.startBoostTrial().then((trial) => {
      if (this.dead) return;
      if (!boost.trialView(trial).trialActive) throw new Error('没能开始，请重试');
      this.serial = (this.serial || 0) + 1;
      this.setData({ starting: false });
      this.show(trial, []);
      wx.showToast({ title: '加量体验已开始', icon: 'none' });
    }).catch((err) => {
      if (this.dead) return;
      this.setData({ starting: false, error: (err && err.message) || '没能开始，请重试' });
      // 用过了、有使用期、开关关了：按服务端的最新状态重新显示
      if (err && (err.status === 404 || err.status === 409)) this.load();
      // 协议已更新（只同意过写进体验记录之前的版本，后台 TOOLS_AGREEMENT_VERSION）：请他读新版全文并同意，回来重新读状态
      if (err && err.status === 403) require('../../utils/consent.js').ensureConsent().then((ok) => { if (ok && !this.dead) this.load(); });
    });
  },
  back() { wx.navigateBack({ fail: () => wx.redirectTo({ url: '/pages/advisor-form/index' }) }); },
});
