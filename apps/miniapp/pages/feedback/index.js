const api = require('../../utils/api.js');
const { ensureConsent, loginOpen } = require('../../utils/consent.js');

const MAX = 500;
const COVERAGE_PREFIX = '申请覆盖：';
// type=pay：从购买页、订单页点"遇到问题"进来；带上订单号，核对台据此找到订单
const ORDER_ID = /^\d{1,12}$/;
function payPrefix(orderId) { return ORDER_ID.test(orderId || '') ? `支付问题（订单 ${orderId}）：` : '支付问题：'; }

// 纠错与建议：只收文字和对应的文件，不收姓名、电话等联系方式。
// 提交前要同意协议；协议定稿前（loginOpen 为 false，D-056）没有入口，直接打开时只显示"暂未开放"
Page({
  // reward：纠错被采纳送天数的规则（D-070 ⑦，服务端开关开着时 /me 带回；只在纠错时显示）
  data: { docId: '', title: '', content: '', count: 0, max: MAX, sending: false, coverage: false, pay: false, orderId: '', prefix: '', loginOpen: loginOpen(), reward: '' },

  onLoad(query) {
    if (!this.data.loginOpen) return;
    // type=coverage：申请覆盖（想增加的地区和栏目），提交时内容前加"申请覆盖："方便核对台区分
    const coverage = query.type === 'coverage';
    const pay = query.type === 'pay';
    const prefix = coverage ? COVERAGE_PREFIX : pay ? payPrefix(query.orderId) : '';
    this.setData({
      docId: coverage || pay ? '' : query.id || '',
      title: !coverage && !pay && query.title ? decodeURIComponent(query.title) : '',
      coverage,
      pay,
      // 带上了订单号才说"已自动带上"，并写出是哪一笔：订单页记的可能是之前操作过的那一笔，问的不是它时用户能看出来；
      // 没带上时请用户自己写（Codex 10/19）
      orderId: pay && ORDER_ID.test(query.orderId || '') ? query.orderId : '',
      prefix,
      max: MAX - prefix.length,
    });
    if (coverage) wx.setNavigationBarTitle({ title: '申请覆盖' });
    if (pay) wx.setNavigationBarTitle({ title: '支付和订单问题' });
    if (!coverage && !pay) this.loadReward();
  },

  // 采纳标准在提交之前写出来（D-070 ⑦："采纳标准公开"）；读不到就不写，不影响纠错
  loadReward() {
    if (typeof api.me !== 'function') return;
    api.me().then((res) => {
      const reward = res && res.correctionReward;
      if (!reward || !Number.isInteger(reward.days) || !Number.isInteger(reward.monthLimit) || typeof reward.rule !== 'string') return;
      this.setData({ reward: `纠错被采纳送 ${reward.days} 天批量工具使用期：${reward.rule}；每人每月最多 ${reward.monthLimit} 天。` });
    }).catch(() => { /* 读不到规则也照常能纠错 */ });
  },

  onInput(e) {
    if (e.detail.value.length > this.data.max) wx.showToast({ title: `最多输入 ${this.data.max} 字，请缩短内容`, icon: 'none' });
    const content = e.detail.value.slice(0, this.data.max);
    this.setData({ content, count: content.length });
  },

  submit() {
    const content = this.data.content.trim();
    if (!content) {
      wx.showToast({ title: this.data.pay ? '请写下遇到的支付或订单问题' : '请写下要纠正或建议的内容', icon: 'none' });
      return;
    }
    if (content.length > this.data.max) {
      wx.showToast({ title: `最多输入 ${this.data.max} 字，请缩短内容`, icon: 'none' });
      return;
    }
    if (this.data.sending) return;
    ensureConsent().then((ok) => { if (ok) this.send(`${this.data.prefix}${content}`); });
  },

  send(content) {
    this.setData({ sending: true });
    api.feedback(this.data.docId, content)
      .then((res) => {
        // 服务端去掉了手机号、身份证号、邮箱时告诉用户
        if (res && res.masked) wx.showToast({ title: '已收到，手机号等个人信息已隐去', icon: 'none' });
        else wx.showToast({ title: '已收到，谢谢你', icon: 'success' });
        setTimeout(() => wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/me/index' }) }), 1200);
      })
      .catch((err) => {
        this.setData({ sending: false });
        wx.showToast({ title: err.message || '提交失败，请稍后再试', icon: 'none', duration: 2500 });
      });
  },
});
