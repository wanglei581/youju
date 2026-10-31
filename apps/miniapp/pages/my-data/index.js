const api = require('../../utils/api.js');
const { describe } = require('../../utils/conditions.js');
const { withdraw, loginOpen, remindAny } = require('../../utils/consent.js');
const { KINDS, DATA_KINDS, STATES } = require('../../utils/advisor.js');
const pages = require('../../utils/pages.js');

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

// 处理中超过一天的一定没人在跑了（和后台的删除、过期清理同一条线）：功能关掉以后也不会有人接着处理，可以删
const ABANDONED = 24 * 3600 * 1000;
function stuck(t) {
  const started = t.startedAt && Date.parse(t.startedAt);
  return t.status === 'running' && Number.isFinite(started) && Date.now() - started > ABANDONED;
}

// 工具记录（批量工具和 AI 的任务）：类型、状态、时间；本人随时能删（D-093 上线门槛第 8 条），
// 只有正在处理的要等结束。结果文件只在"我的记录"里下载，过审版没有这一页就不提
function taskData(d) {
  const rows = d.advisorTasks || [];
  const ai = rows.some((t) => Object.prototype.hasOwnProperty.call(KINDS, t.kind));
  const files = rows.some((t) => Object.prototype.hasOwnProperty.call(DATA_KINDS, t.kind) && t.status === 'done');
  const extras = [d.boostTrial ? '加量体验的开始和结束时间' : '', d.passGiftsTotal ? `送的批量工具天数（${d.passGiftsTotal} 笔）` : ''].filter(Boolean);
  return {
    tasks: rows.map((t) => ({
      id: t.id,
      kind: KINDS[t.kind] || DATA_KINDS[t.kind] || '工具记录',
      rows: Object.prototype.hasOwnProperty.call(DATA_KINDS, t.kind) && t.rowCount ? t.rowCount : 0,
      state: STATES[t.status] || t.status,
      running: t.status === 'running' && !stuck(t),
      queued: t.status === 'queued',
      stuck: stuck(t),
      created: when(t.createdAt),
      finished: when(t.finishedAt),
    })),
    tasksTitle: ai ? '工具和 AI 记录' : '工具记录',
    fileNote: files && pages.has('advisor-tasks') ? '批量工具的结果文件在“我的”→“我的记录”里下载' : '',
    extras: extras.length ? `${extras.join('、')}：在“复制全部数据”里可以看到。` : '',
    toolsShown: pages.has('advisor') || rows.length > 0,
    boostShown: pages.has('advisor') || rows.length > 0 || Boolean(d.boostTrial),
  };
}

function chatRows(rows) {
  return rows.map((c) => ({ ...c, title: c.hasDoc ? (c.docTitle || '文件已下线') : (c.lastQuestion || '找通知'), last: when(c.lastAt) }));
}

// 我的数据：查阅、复制我们保存的你的全部个人信息，改关注名称，删除记录
// （个人信息保护法第四十五至四十七条；网络数据安全管理条例第二十三条）
// 协议定稿前（loginOpen 为 false，D-056）没有入口，直接打开时只显示"暂未开放"，不请求接口
Page({
  data: { loading: true, error: '', aiRetention: '', chats: [], chatsTitle: 'AI 对话记录', chatsNext: null, chatsLoading: false, chatDetail: null, chatLoading: false, chatRetention: '', user: null, follows: [], subs: [], feedback: [], tasks: [], tasksTitle: '', fileNote: '', extras: '', toolsShown: false, boostShown: false, totals: {}, retention: null, exportedAt: '', loginOpen: loginOpen(), remindOpen: remindAny() },

  onShow() {
    if (!this.data.loginOpen) return;
    if (api.watchStartup) api.watchStartup(this);
    this.load();
  },

  onHide() {
    if (api.stopStartup) api.stopStartup(this);
  },

  onUnload() {
    this.chatRequest = (this.chatRequest || 0) + 1;
    this.loadEpoch = (this.loadEpoch || 0) + 1;
    if (api.stopStartup) api.stopStartup(this);
  },

  load() {
    this.chatRequest = (this.chatRequest || 0) + 1;
    this.loadEpoch = (this.loadEpoch || 0) + 1;
    this.setData({ chatDetail: null, chatLoading: false, loading: true, error: '' });
    api.myData()
      .then((d) => {
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
            // 处理结果和处理人写的说明（差距清单 C1）；以前处理的没有结果，照旧只写"已处理"
            outcome: f.handled && f.outcomeLabel ? f.outcomeLabel : '',
            reply: f.handled && f.reply ? f.reply : '',
            handledAt: when(f.handledAt),
            created: when(f.createdAt),
          })),
          ...taskData(d),
          chats: chatRows(d.aiChats || []),
          chatsNext: d.aiChatsNext || null,
          chatsLoading: false,
          chatRetention: (d.aiChats || []).length && d.retention && d.retention.aiChats
            ? `· AI 对话：每一问保存 ${d.retention.aiChats} 天，可以在本页提前删除整条对话` : '',
          totals: { chats: d.aiChatsTotal || 0, chatTurns: d.aiChatTurnsTotal || 0, subs: d.subscriptionsTotal, feedback: d.feedbackTotal, events: d.eventsTotal, tasks: d.advisorTasksTotal || (d.advisorTasks || []).length },
          retention: d.retention,
          // 有过 AI 解读或条款对照任务的才写这两项的保存期限：AI 只对白名单开放时，其他人看不到 AI 字样
          // （AI 以外页面的模板里不写 AI，见 ai-entry.test.js）
          aiRetention: (d.advisorTasks || []).some((t) => t.kind === 'interpret' || t.kind === 'clause') && d.retention && d.retention.toolTasks
            ? `· AI 解读和条款对照：任务输入 ${d.retention.toolInput} 天后删除，任务和结果 ${d.retention.toolTasks} 天后删除` : '',
        });
      })
      .catch((err) => this.setData({ loading: false, error: `加载失败：${err.message}` }));
  },

  moreChats() {
    if (!this.data.chatsNext || this.data.chatsLoading) return;
    const token = this.loadEpoch;
    this.setData({ chatsLoading: true });
    api.myDataChats(this.data.chatsNext).then((d) => {
      if (token !== this.loadEpoch) return;
      const ids = new Set(this.data.chats.map((c) => c.id));
      const rows = d.items.filter((c) => !ids.has(c.id));
      this.raw.aiChats.push(...rows);
      this.raw.aiChatsNext = d.next;
      this.setData({ chats: [...this.data.chats, ...chatRows(rows)], chatsNext: d.next, chatsLoading: false });
    }).catch((err) => {
      if (token !== this.loadEpoch) return;
      this.setData({ chatsLoading: false });
      wx.showToast({ title: err.message || '没能加载，请重试', icon: 'none' });
    });
  },

  viewChat(e) {
    const id = e.currentTarget.dataset.id;
    if (!this.data.chats.some((c) => String(c.id) === String(id))) return;
    this.fetchChat(id);
  },

  earlierChat() {
    const d = this.data.chatDetail;
    if (!d || !d.hasMore || this.data.chatLoading) return;
    this.fetchChat(d.id, d.turns[0].seq);
  },

  fetchChat(id, before) {
    const token = this.chatRequest = (this.chatRequest || 0) + 1;
    this.setData({ chatLoading: true });
    api.myDataChat(id, before).then((d) => {
      if (token !== this.chatRequest) return;
      this.chatRaw = d;
      this.setData({ chatLoading: false, chatDetail: { ...d, turns: d.turns.map((t) => ({
        ...t, skillLabel: { find: '找通知', ask: '问这份通知' }[t.skill] || t.skill,
        state: STATES[t.status] || t.status, created: when(t.createdAt),
      })) } });
    }).catch((err) => {
      if (token !== this.chatRequest) return;
      this.setData({ chatLoading: false });
      wx.showToast({ title: err.message || '没能加载，请重试', icon: 'none' });
    });
  },

  copyChatPage() {
    if (!this.chatRaw || !this.data.chatDetail || this.data.chatLoading) return;
    wx.setClipboardData({ data: JSON.stringify(this.chatRaw, null, 2),
      success: () => wx.showToast({ title: '已复制本页问题及范围', icon: 'none' }),
      fail: () => wx.showToast({ title: '没能复制，请稍后再试', icon: 'none' }) });
  },

  // 复制全部：完整的 JSON，可以粘贴保存（剪贴板只写不读）
  // 复制完整的微信用户标识：联系客服排查问题时用；给自己开 AI 白名单时填到云托管的 YOUJU_AI_ADVISOR_OPENIDS
  copyOpenid() {
    const openid = this.raw && this.raw.user && this.raw.user.openid;
    if (!openid) return;
    wx.setClipboardData({ data: openid, success: () => wx.showToast({ title: '已复制完整标识', icon: 'none' }) });
  },

  copyAll() {
    if (!this.raw) return;
    wx.setClipboardData({
      data: JSON.stringify(this.raw, null, 2),
      success: () => wx.showToast({ title: '已复制全部数据，可以粘贴保存', icon: 'none' }),
      fail: () => wx.showToast({ title: '没能复制：请先同意微信弹出的隐私保护指引，再点一次', icon: 'none' }),
    });
  },

  // 单条复制完整原始记录，包含条件、状态和时间。
  copyOne(e) {
    const { kind, id } = e.currentTarget.dataset;
    const key = { follow: 'follows', sub: 'subscriptions', feedback: 'feedback', task: 'advisorTasks' }[kind];
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

  // 删除一条记录：关注、还没发出的提醒授权、纠错、工具记录（结果文件一起删；排队中的先取消、次数退回）
  remove(e) {
    const { kind, id } = e.currentTarget.dataset;
    const sub = kind === 'sub' && this.data.subs.find((item) => String(item.id) === String(id));
    const cancelling = sub && sub.unused;
    const task = kind === 'task' && this.data.tasks.find((item) => String(item.id) === String(id));
    const what = {
      follow: this.data.remindOpen ? '这个关注（它的微信提醒也会一起取消）' : '这个关注',
      sub: cancelling ? '这次还没发出的提醒' : '这条提醒历史记录',
      feedback: '这条纠错',
      chat: '这条对话的全部问题和结果（已完成的问答次数不退回）',
      task: task && task.queued ? '这条排队中的记录（会先取消，用掉的次数退回）'
        : task && task.stuck ? '这条没有处理完的记录（用掉的次数退回）' : '这条记录和它的结果',
    }[kind];
    const call = { follow: api.deleteFollow, sub: api.deleteSubscription, feedback: api.deleteFeedback, task: api.deleteAdvisorTask, chat: api.deleteChat }[kind];
    if (!call || (kind === 'chat' && !this.data.chats.some((c) => String(c.id) === String(id)))) return;
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
