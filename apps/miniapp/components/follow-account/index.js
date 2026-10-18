const { canFollow, openProfile } = require('../../utils/official-account.js');

// "关注公众号"一行（公众号方案 PR #287）：截止页底部、我的、关于各放一行，点了打开公众号主页。
// 没配公众号原始 ID、基础库低于 3.7.10 时整行不显示；不弹窗、不挡功能、不给奖励（D-041、D-070）。
// variant：bar 是淡蓝底的一条（截止页、关于），row 和"我的"页的列表行一样
Component({
  properties: {
    text: { type: String, value: '关注公众号，每周一收申报周报' },
    variant: { type: String, value: 'bar' },
  },
  data: { show: false },
  lifetimes: {
    attached() {
      this.setData({ show: canFollow() });
    },
  },
  methods: {
    // 微信限定只在点击行为里调用：直接在点击回调里打开
    open() {
      openProfile();
    },
  },
});
