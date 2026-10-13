// AI 内测（只上传体验版、不合并）：任务列表的显示和页面顶部的内部测试标注。
// 数据工具（名单比对、跟进表）的后端没有上线，这里只处理 AI 任务。
const KINDS = { interpret: '政策解读', clause: '条款对照' };
const STATES = { queued: '排队中', running: '处理中', done: '已完成', failed: '失败', cancelled: '已取消' };
// 《人工智能生成合成内容标识办法》要求显式标识：如实写明由 AI 模型生成。
const INTERNAL_PREFIX = '内部测试：';
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

module.exports = { KINDS, STATES, INTERNAL_PREFIX, INTERNAL_NOTICE, internalNotice, allowedKinds, taskView };
