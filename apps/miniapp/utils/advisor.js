// 共同任务的显示与 AI 独立授权；数据工具开关不能扩大 AI 权限。
const config = require('./config.js');
const { failedText } = require('./ai-reasons.js');

const KINDS = { interpret: 'AI 解读', clause: '条款对照' };
const DATA_KINDS = { compare: '批量名单比对', track: '客户相关通知表' };
const STATES = { queued: '排队中', running: '处理中', done: '已完成', failed: '失败', cancelled: '已取消' };
// 《人工智能生成合成内容标识办法》第四条要求显式标识：页面顶部固定写"AI 生成"，如实说明由 AI 模型生成。
// AI 内测包（config.aiInternal）再加"内部测试"；AI 正式版（D-066）只写"AI 生成"。
const AI_INTERNAL = config.aiInternal === true;
const AI_LABEL = AI_INTERNAL ? 'AI 生成 · 内部测试：' : 'AI 生成：';
const INTERNAL_PREFIX = '内部测试：';
// AI 入口的名字（通知详情页"要点"下面的卡片标题）：内测包和正式版都叫"AI 解读"（10/17 创始人定，同一件事只用一个名字；
// 内测包的区别只在页面顶部和标题写"内部测试"）
const FAB_TEXT = 'AI 解读';
// 任务列表（原"任务中心""我的任务""全部任务""最近任务"）统一叫这个，入口在"我的"页
const RECORDS_TEXT = '我的记录';
// 用不了时的提示。分三种：没同意协议（引导同意）、连不上服务器（给重试）、功能关着。
// 功能关着时内测包、正式版都只写"暂时没有可用工具"，不提 AI（D-075：AI 关着时任何页面没有 AI 字样；
// 从旧链接打开解读页、对照页时直接回首页）。
// 没同意通用协议时数据工具和 AI 一起不可用（capabilities 403），工具中心、任务中心共用这句，不写 AI。
const OFF_TEXT = '暂时没有可用工具';
const GATE_TEXT = {
  consent: '使用前，请先阅读并同意《用户协议》和《隐私保护指引》。',
  // 同意过旧版本、协议更新后还没同意新版（服务端 403 说"已更新"）：卡片上直接说，不用点进去才知道（10/18 验收）
  updated: '《用户协议》和《隐私保护指引》已更新，请阅读并同意新版后继续使用。',
  offline: '暂时连不上服务器，请稍后重试。',
  off: OFF_TEXT,
};
// 适用人群、场合、用途（《生成式人工智能服务管理暂行办法》第十条要求公开）
const SCOPE = [
  '适用人群：企业里负责项目申报的经办人员。',
  '适用场合：阅读政府部门发布的申报通知、准备申报材料时。',
  '用途：把通知里的申报对象、条件、材料等原句摘出来分类，方便快速找到；条款对照把原文条件和你填写的企业情况并排列出，供你自己对照。',
  '不适用：判断企业能不能申报、代替阅读通知原文。不面向未成年人。',
];
const INTERNAL_NOTICE = '摘录通知原文原句，可能不全；不判断能否申报，以原文和主管部门解释为准。';

// 后端 capabilities 的 disclosure 如果带了说明文字（字符串或 text 字段），优先显示后端的；
// 目前线上只返回 modelLabel、filingNo，所以显示固定文字。
function internalNotice(disclosure) {
  let text = '';
  if (typeof disclosure === 'string') text = disclosure;
  else if (disclosure && typeof disclosure.text === 'string') text = disclosure.text;
  text = text.trim();
  if (text.startsWith(INTERNAL_PREFIX)) text = text.slice(INTERNAL_PREFIX.length).trim();
  return text || INTERNAL_NOTICE;
}

// capabilities 的结果对应哪种提示（内测包一律 off）
function gateState(res) {
  if (AI_INTERNAL) return 'off';
  const reason = res && res.reason;
  return reason === 'consent' || reason === 'offline' ? reason : 'off';
}

// 开放 AI 后的页面标题：内测包后面加"内部测试"
// 用不了时卡片上的说明：没同意协议的分"第一次"和"协议已更新"
function gateText(gate, caps) {
  return GATE_TEXT[gate === 'consent' && caps && caps.consentUpdated === true ? 'updated' : gate] || '';
}

function aiTitle(name) {
  return AI_INTERNAL ? `${name} · 内部测试` : name;
}

// 只保留当前账号有权限的 AI 任务；条款对照还要 features 里有 clause。
function allowedKinds(ai) {
  if (!ai || ai.enabled !== true) return [];
  const features = Array.isArray(ai.features) ? ai.features : [];
  return Object.keys(KINDS).filter((kind) => features.includes(kind));
}

function dataKinds(data) {
  return data && data.enabled === true && Array.isArray(data.features) ? Object.keys(DATA_KINDS).filter((kind) => data.features.includes(kind)) : [];
}

function allKinds(caps) {
  return dataKinds(caps && caps.data_tools).concat(allowedKinds(caps && caps.ai_advisor));
}

// 只记任务编号和文件身份，不保存结果或客户内容。模块在页面销毁后仍保留，
// 列表中任务消失或文件身份改变才解除 410，避免轮询/重新进页复活下载按钮。
const unavailableResults = new Map();
const fileIdentity = (task) => JSON.stringify([task.kind, task.hasFile, task.createdAt, task.finishedAt, task.expiresAt]);
function blockDownload(task, message) { unavailableResults.set(String(task.id), { identity: fileIdentity(task), message }); }
function taskViews(items) {
  const ids = new Set(items.map((task) => String(task.id)));
  for (const id of unavailableResults.keys()) if (!ids.has(id)) unavailableResults.delete(id);
  return items.map(taskView);
}

function taskView(task) {
  const blocked = unavailableResults.get(String(task.id));
  const unavailable = blocked && blocked.identity === fileIdentity(task);
  if (blocked && !unavailable) unavailableResults.delete(String(task.id));
  const stamp = task.createdAt && Date.parse(task.createdAt);
  const dataTask = Object.prototype.hasOwnProperty.call(DATA_KINDS, task.kind);
  const expired = Boolean(task.expiresAt && Date.parse(task.expiresAt) <= Date.now());
  return Object.assign({}, task, {
    kindLabel: KINDS[task.kind] || DATA_KINDS[task.kind] || task.kind,
    stateLabel: STATES[task.status] || task.status,
    time: Number.isFinite(stamp) ? new Date(stamp + 8 * 3600000).toISOString().slice(0, 16).replace('T', ' ') : '',
    terminal: ['done', 'failed', 'cancelled'].includes(task.status),
    dataTask, expired,
    downloadable: dataTask && task.status === 'done' && task.hasFile === true && !expired && !unavailable,
    resultNote: unavailable ? blocked.message : dataTask && task.status === 'done' && (!task.hasFile || expired) ? '结果已过期或已删除，请重新提交' : '',
    expiresText: task.expiresAt ? String(task.expiresAt).slice(0, 10) : '',
    // 失败、取消的已经退回占用的次数（服务端 release_usage）；卡片上写明，免得用户以为白用了一次（10/18 验收）
    refundNote: task.status === 'failed' || task.status === 'cancelled' ? '这次不算次数' : '',
    // AI 任务失败按后台的错误代码写（D-075），和结果页一样；数据工具照旧用后台的文字
    errorText: task.status === 'failed' && Object.prototype.hasOwnProperty.call(KINDS, task.kind) ? failedText(task, { refund: false }) : (task.error || ''),
  });
}

// 今天还剩几次（AI 首页、工具中心、解读页、对照页共用）：后台给的是数才算（取整、不小于 0）；
// 没给、给的是 null、空串、true/false 都当不知道（null），不当 0，免得误写"用完了"、拦下还能用的（第 25 轮）
function remainingOf(ai, kind) {
  const value = ai && ai.remaining ? ai.remaining[kind] : undefined;
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : null;
}

module.exports = { KINDS, DATA_KINDS, STATES, AI_INTERNAL, AI_LABEL, FAB_TEXT, RECORDS_TEXT, OFF_TEXT, GATE_TEXT, SCOPE, INTERNAL_PREFIX, gateText, INTERNAL_NOTICE, aiTitle, gateState, internalNotice, allowedKinds, dataKinds, allKinds, taskView, taskViews, blockDownload, remainingOf };
