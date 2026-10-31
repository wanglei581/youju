// 不能 AI 解读的原因（D-075 第六节第 3 条）：后台给原因代码（列表、详情、提交任务用同一个判断），
// 这里写卡片上的话和下一步。没有附件时不给"看附件"；来源那条不说"只收录标题"（代码保证不了）。
// 卡片和提交被拒（422 AI_NOT_ELIGIBLE）共用这一份，同一个原因只有一种说法
const REASONS = {
  list: { text: '名单不需要 AI 解读。要查企业在不在名单上，可以用“查名单”。', next: 'lists' },
  kind: { text: '这类文件不做 AI 解读。', next: 'read' },
  source: { text: '这个来源暂不用于 AI 解读。', next: 'read' },
  no_body: { text: '还没有拿到完整正文，正文可能在附件里。', next: 'attachments' },
  too_long: { text: '正文超过 2.4 万字，暂不能解读，请先看要点和原文。', next: '' },
  unavailable: { text: '这份文件已下架。', next: 'back' },
  // 原文链接已失效（后台隔天两次确认，backend/src/youju/link_check.py；分不清是撤稿还是改了地址）：
  // 为免误导不再开始新的解读；复制链接也打不开，不给下一步
  link_gone: { text: '原文链接已失效，这份通知暂不提供 AI 解读和条款对照。', next: '' },
};
const FALLBACK = '这份文件暂不能解读，请查看原文。';
// 下一步按钮上的字
const NEXT_TEXT = { lists: '去查名单', read: '站内阅读正文', attachments: '看附件', back: '返回' };

// next：lists 去查名单；read 站内阅读正文；attachments 有附件时看附件、没有时站内阅读正文；back 返回
function reasonView(code, hasAttachments) {
  const reason = REASONS[code];
  let next = reason ? reason.next : 'read';
  if (next === 'attachments' && !hasAttachments) next = 'read';
  return { text: reason ? reason.text : FALLBACK, next, nextText: NEXT_TEXT[next] || '' };
}

// 任务失败按后台给的错误代码写（D-075），不比对文字：排队后才撞上全站上限、admin 处理不了时退回、
// 被移出白名单，都已退回次数。被移出白名单也写"暂时不可用"，不写"未开放"。
// refund 为假时不带"没有扣你的次数"（任务列表另有一行"这次不算次数"）
const FAILED = {
  AI_DAILY_CAP: '今天 AI 的总用量满了，明天 0 点恢复',
  AI_UNAVAILABLE: 'AI 暂时不可用，请稍后再试',
  AI_OFF: 'AI 暂时不可用，请稍后再试',
};
function failedText(task, options = {}) {
  const base = FAILED[task && task.errorCode];
  if (!base) return (task && task.error) || '结果已失效，请重新提交';
  return options.refund === false ? `${base}。` : `${base}；这次没有扣你的次数。`;
}
// 这两种失败说明 AI 对这个人已经不能用了：页面要让 AI 板块重新问一次开没开
function revokedCode(code) {
  return code === 'AI_OFF' || code === 'AI_UNAVAILABLE';
}

module.exports = { REASONS, FALLBACK, NEXT_TEXT, FAILED, reasonView, failedText, revokedCode };
