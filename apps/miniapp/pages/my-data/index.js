const api = require('../../utils/api.js');
const { taskView } = require('../../utils/advisor.js');
const { describe } = require('../../utils/conditions.js');
const { withdraw } = require('../../utils/consent.js');

const SUB_KIND = { newDocs: '关注有新文件', deadline: '截止前提醒' };
const SUB_STATUS = { unused: '还没发出', sent: '已发出', failed: '发送失败', refused: '被拒收', expired: '已失效' };
const CHECK = { pass: '已通过内容安全检查', unchecked: '未做内容安全检查' };

// "2026-09-28T01:18:44.868910+08:00" → "2026-09-28 01:18"
function when(iso) {
  return iso ? iso.slice(0, 16).replace('T', ' ') : '';
}

// 用户标识只显示首尾；"复制全部"里是完整的
function maskId(id) {
  return id && id.length > 10 ? `${id.slice(0, 4)}…${id.slice(-4)}` : id || '';
}

// 我的数据：查阅、复制我们保存的你的全部个人信息，改关注名称，删除记录
// （个人信息保护法第四十五至四十七条；网络数据安全管理条例第二十三条）
Page({
  data: { loading: true, error: '', user: null, follows: [], subs: [], feedback: [], advisorTasks: [], aiEnabled: false, clauseEnabled: false, totals: {}, retention: null, exportedAt: '' },

  onShow() {
    this.load();
  },

  load() {
    this.setData({ loading: true, error: '' });
    Promise.all([api.myData(), api.advisorCapabilities()])
      .then(([d, caps]) => {
        const aiEnabled = caps.ai_advisor.enabled === true;
        const clauseEnabled = aiEnabled && (caps.ai_advisor.features || []).includes('clause');
        const tasks = (d.advisorTasks || []).filter((task) =>
          (task.kind !== 'interpret' || aiEnabled) && (task.kind !== 'clause' || clauseEnabled));
        d = Object.assign({}, d, { advisorTasks: tasks });
        this.raw = d;
        const followName = {};
        d.follows.forEach((f) => { followName[f.id] = f.name; });
        const u = d.user;
        this.setData({
          loading: false,
          exportedAt: when(d.exportedAt),
          user: u && {
            id: maskId(u.openid),
            created: when(u.createdAt),
            visited: when(u.lastVisitedAt),
            agreedVersion: u.agreed ? u.agreed.version : '',
            agreedAt: u.agreed ? when(u.agreed.at) : '',
            advisorVersion: u.advisorAgreed ? u.advisorAgreed.version : '',
            advisorAt: u.advisorAgreed ? when(u.advisorAgreed.at) : '',
          },
          follows: d.follows.map((f) => ({ id: f.id, name: f.name, desc: describe(f.conditions), created: when(f.createdAt) })),
          subs: d.subscriptions.map((s) => ({
            id: s.id,
            kind: SUB_KIND[s.kind] || s.kind,
            target: s.followId ? `关注"${followName[s.followId] || s.followId}"` : (s.docTitle || '文件已下线'),
            status: SUB_STATUS[s.status] || s.status,
            unused: s.status === 'unused',
            created: when(s.createdAt),
            attempted: when(s.attemptedAt),
          })),
          feedback: d.feedback.map((f) => ({
            id: f.id,
            title: f.docId ? (f.docTitle || '文件已下线') : '一般建议',
            content: f.content,
            check: CHECK[f.checkResult] || f.checkResult,
            handling: f.handled ? '已处理' : '待处理',
            handledAt: when(f.handledAt),
            created: when(f.createdAt),
          })),
          advisorTasks: tasks.map(taskView), aiEnabled, clauseEnabled,
          totals: { subs: d.subscriptionsTotal, feedback: d.feedbackTotal, events: d.eventsTotal, advisor: d.advisorTasksTotal || 0 },
          retention: d.retention,
        });
      })
      .catch((err) => this.setData({ loading: false, error: `加载失败：${err.message}` }));
  },

  // 复制全部：完整的 JSON，可以粘贴保存（剪贴板只写不读）
  copyAll() {
    if (!this.raw) return;
    wx.setClipboardData({
      data: JSON.stringify(this.raw, null, 2),
      success: () => wx.showToast({ title: '已复制全部数据，可以粘贴保存', icon: 'none' }),
      fail: () => wx.showToast({ title: '没能复制，请同意隐私保护指引后再试', icon: 'none' }),
    });
  },
  advisorResult(e) {
    const id = e.currentTarget.dataset.id;
    const task = this.data.advisorTasks.find((item) => String(item.id) === String(id));
    if (!this.data.aiEnabled || !task) return;
    if (task.kind === 'clause' && this.data.clauseEnabled) {
      wx.navigateTo({ url: `/pages/advisor-clause/index?taskId=${encodeURIComponent(id)}` });
    }
  },

  // 单条复制完整原始记录，包含条件、状态和时间。
  copyOne(e) {
    const { kind, id } = e.currentTarget.dataset;
    const key = { follow: 'follows', sub: 'subscriptions', feedback: 'feedback' }[kind];
    const row = this.raw && key && this.raw[key].find((item) => String(item.id) === String(id));
    if (!row) return;
    wx.setClipboardData({
      data: JSON.stringify(row, null, 2),
      success: () => wx.showToast({ title: '已复制这条完整记录', icon: 'none' }),
      fail: () => wx.showToast({ title: '没能复制，请稍后再试', icon: 'none' }),
    });
  },

  // 更正：改关注名称
  rename(e) {
    const { id, name } = e.currentTarget.dataset;
    wx.showModal({
      title: '改关注名称',
      editable: true,
      placeholderText: '1–40 字',
      content: name,
      confirmText: '保存',
      success: (res) => {
        if (!res.confirm) return;
        const next = (res.content || '').trim();
        if (!next || next === name) return;
        api.renameFollow(id, next.slice(0, 40))
          .then(() => { wx.showToast({ title: '已改名', icon: 'none' }); this.load(); })
          .catch((err) => wx.showToast({ title: err.message || '没改成，请稍后再试', icon: 'none' }));
      },
    });
  },

  // 删除一条记录：关注、还没发出的提醒授权、纠错
  remove(e) {
    const { kind, id } = e.currentTarget.dataset;
    const sub = kind === 'sub' && this.data.subs.find((item) => String(item.id) === String(id));
    const cancelling = sub && sub.unused;
    const what = { follow: '这个关注（它的微信提醒也会一起取消）', sub: cancelling ? '这次还没发出的提醒' : '这条提醒历史记录', feedback: '这条纠错', advisor: '这个工具任务和结果文件' }[kind];
    const call = { follow: api.deleteFollow, sub: api.deleteSubscription, feedback: api.deleteFeedback, advisor: api.deleteAdvisorTask }[kind];
    wx.showModal({
      title: cancelling ? '取消提醒' : '删除记录',
      content: `删除${what}？删除后不能恢复。`,
      confirmText: cancelling ? '取消提醒' : '删除',
      confirmColor: '#E5484D',
      success: (res) => {
        if (!res.confirm) return;
        call(id)
          .then(() => { wx.showToast({ title: '已删除', icon: 'none' }); this.load(); })
          .catch((err) => wx.showToast({ title: err.message || '没删成，请稍后再试', icon: 'none' }));
      },
    });
  },

  deleteAll() {
    withdraw().then((ok) => { if (ok) wx.navigateBack(); });
  },
});
