// AI 页面共用：任务列表的显示、页面顶部的"AI 生成"标注和页面标题。
// 数据工具（名单比对、跟进表）的后端没有上线，这里只处理 AI 任务。
const config = require('./config.js');

const KINDS = { interpret: '政策解读', clause: '条款对照' };
const STATES = { queued: '排队中', running: '处理中', done: '已完成', failed: '失败', cancelled: '已取消' };
// 《人工智能生成合成内容标识办法》第四条要求显式标识：页面顶部固定写"AI 生成"，如实说明由 AI 模型生成。
// AI 内测包（config.aiInternal）再加"内部测试"；AI 正式版（D-066）只写"AI 生成"。
const AI_INTERNAL = config.aiInternal === true;
const AI_LABEL = AI_INTERNAL ? 'AI 生成 · 内部测试：' : 'AI 生成：';
const INTERNAL_PREFIX = '内部测试：';
// 通知详情页右下角入口的文字
const FAB_TEXT = AI_INTERNAL ? 'AI 内测' : 'AI 解读';
// 用不了时的提示。内测包对不在白名单的人只写"暂时没有可用工具"，不提 AI；
// 正式版分三种：没同意协议（引导同意）、连不上服务器（给重试）、功能关着
const OFF_TEXT = AI_INTERNAL ? '暂时没有可用工具' : 'AI 功能暂时不可用，请稍后再试';
const GATE_TEXT = {
  consent: '使用 AI 功能前，请先阅读并同意《用户协议》和《隐私保护指引》。',
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
const INTERNAL_NOTICE = '结果由 AI 模型根据原文生成，每条都附原文引用，可能不全，请以原文为准，不作为能否申报的判断。';

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
function aiTitle(name) {
  return AI_INTERNAL ? `${name} · 内部测试` : name;
}

// 只保留当前账号有权限的 AI 任务；条款对照还要 features 里有 clause。
function allowedKinds(ai) {
  if (!ai || ai.enabled !== true) return [];
  const features = Array.isArray(ai.features) ? ai.features : [];
  return Object.keys(KINDS).filter((kind) => features.includes(kind));
}

function taskView(task) {
  const stamp = task.createdAt && Date.parse(task.createdAt);
  return Object.assign({}, task, {
    kindLabel: KINDS[task.kind] || task.kind,
    stateLabel: STATES[task.status] || task.status,
    time: Number.isFinite(stamp) ? new Date(stamp + 8 * 3600000).toISOString().slice(0, 16).replace('T', ' ') : '',
    terminal: ['done', 'failed', 'cancelled'].includes(task.status),
  });
}

module.exports = { KINDS, STATES, AI_INTERNAL, AI_LABEL, FAB_TEXT, OFF_TEXT, GATE_TEXT, SCOPE, INTERNAL_PREFIX, INTERNAL_NOTICE, aiTitle, gateState, internalNotice, allowedKinds, taskView };
