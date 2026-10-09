const legal = require('../../content/legal.js');
const { loadCoverage } = require('../../utils/coverage.js');
const { shortTime } = require('../../utils/format.js');
const { loginOpen, openPrivacy } = require('../../utils/consent.js');

const ENV_NAME = { develop: '开发版', trial: '体验版' };

// 关于有据：运营者、备案号、联系和投诉渠道
// （微信小程序运营规范 3.10 侵权投诉机制、3.13 披露机构信息和咨询投诉渠道）。
// 协议定稿前（loginOpen 为 false，D-056）没有关注、提醒、纠错和申请覆盖：使用说明不提这些，纠错改为发邮件；
// 加一段"数据说明"；不打开草案协议，隐私保护指引打开小程序后台填写的官方页面
Page({
  data: { op: legal.operator, disclaimer: legal.disclaimer, version: '', cov: null, covFailed: false, open: {}, loginOpen: loginOpen() },

  onLoad() {
    let version = '';
    try {
      const mp = wx.getAccountInfoSync().miniProgram;
      version = mp.version || ENV_NAME[mp.envVersion] || '';
    } catch (e) { /* 取不到版本号不影响页面 */ }
    this.setData({ version });
    this.loadRange();
  },

  // 收录范围（验收 A8）：已收录的地区、栏目数、最近更新时间，还没收录的地区；取不到时可重试
  loadRange() {
    this.setData({ covFailed: false });
    loadCoverage().then((c) => {
      if (!c) { this.setData({ covFailed: true }); return; }
      this.setData({
        cov: {
          sources: c.sources,
          provinces: c.provinces,
          national: c.national,
          cities: c.cities || [],
          updated: shortTime(c.updatedAt),
          regions: c.regions.map((r) => ({
            name: r.name,
            sources: r.sources,
            updated: shortTime(r.lastUpdatedAt),
            columns: r.columns.map((col) => ({ name: col.name, updated: shortTime(col.lastUpdatedAt) })),
          })),
          notCovered: c.notCovered,
        },
      });
    });
  },

  // 点地区展开或收起它的栏目
  toggleRegion(e) {
    const { name } = e.currentTarget.dataset;
    this.setData({ [`open.${name}`]: !this.data.open[name] });
  },

  copyEmail() {
    const { email } = this.data.op;
    if (!email || email.indexOf('【') === 0) return;
    wx.setClipboardData({
      data: email,
      success: () => wx.showToast({ title: '邮箱已复制', icon: 'none' }),
      fail: () => wx.showToast({ title: '没能复制，请同意隐私保护指引后再试', icon: 'none' }),
    });
  },

  feedback() {
    wx.navigateTo({ url: '/pages/feedback/index' });
  },

  openAgreement(e) {
    wx.navigateTo({ url: `/pages/agreement/index?type=${e.currentTarget.dataset.type}` });
  },

  openPrivacy() {
    openPrivacy();
  },
});
