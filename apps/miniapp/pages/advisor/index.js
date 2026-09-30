const api = require('../../utils/api.js');

Page({
  data: { loading: true, error: '', enabled: false, skills: [] },
  onShow() { this.load(); },
  load() {
    this.setData({ loading: true, error: '' });
    api.advisorStatus().then((res) => {
      this.setData({ loading: false, enabled: res.enabled, skills: [
        { kind: 'compare', title: '名单比对', description: '查客户上过哪些公示/公布名单' },
        { kind: 'track', title: '在报项目跟进表', description: '按客户省份列出未截止项目' },
      ].map((skill) => Object.assign(skill, { remaining: Math.max(0, res.quota[skill.kind].limit - res.quota[skill.kind].used), maxRows: res.quota[skill.kind].maxRows })) });
    }).catch((err) => this.setData({ loading: false, enabled: false, error: err.status === 404 ? '工具暂未开放' : err.message }));
  },
  form(e) { wx.navigateTo({ url: `/pages/advisor-form/index?kind=${e.currentTarget.dataset.kind}` }); },
  tasks() { wx.navigateTo({ url: '/pages/advisor-tasks/index' }); },
});
