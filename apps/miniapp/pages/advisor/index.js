const api = require('../../utils/api.js');

Page({
  data: { loading: true, error: '', enabled: false, skills: [] },
  onShow() { this.load(); },
  load() {
    this.setData({ loading: true, error: '' });
    api.advisorStatus().then((res) => {
      this.setData({ loading: false, enabled: res.enabled, skills: [
        { kind: 'compare', title: '名单比对', description: '在已收录的公示/公布名单里查找客户' },
        { kind: 'track', title: '在报项目跟进表', description: '按客户省份列出截止日期已核对、尚未截止的申报通知' },
      ].map((skill) => Object.assign(skill, { remaining: Math.max(0, res.quota[skill.kind].limit - res.quota[skill.kind].used), maxRows: res.quota[skill.kind].maxRows })) });
    }).catch((err) => this.setData({ loading: false, enabled: false, error: err.status === 404 ? '工具暂未开放' : err.message }));
  },
  form(e) { wx.navigateTo({ url: `/pages/advisor-form/index?kind=${e.currentTarget.dataset.kind}` }); },
  tasks() { wx.navigateTo({ url: '/pages/advisor-tasks/index' }); },
});
