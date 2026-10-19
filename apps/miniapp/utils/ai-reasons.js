// 不能 AI 解读的原因（D-075 第六节第 3 条）：后台给原因代码（列表、详情、提交任务用同一个判断），
// 这里写卡片上的话和下一步。没有附件时不给"看附件"；来源那条不说"只收录标题"（代码保证不了）。
// 卡片和提交被拒（422 AI_NOT_ELIGIBLE）共用这一份，同一个原因只有一种说法
const REASONS = {
  list: { text: '名单不需要 AI 解读。要查企业在不在名单上，可以用“查名单”。', next: 'lists' },
  kind: { text: '这类文件不做 AI 解读。', next: 'copy' },
  source: { text: '这个来源暂不用于 AI 解读。', next: 'copy' },
  no_body: { text: '还没有拿到完整正文，正文可能在附件里。', next: 'attachments' },
  too_long: { text: '正文超过 2.4 万字，暂不能解读，请先看要点和原文。', next: '' },
  unavailable: { text: '这份文件已下架。', next: 'back' },
};
const FALLBACK = '这份文件暂不能解读，请查看原文。';
// 下一步按钮上的字
const NEXT_TEXT = { lists: '去查名单', copy: '复制原文链接', attachments: '看附件', back: '返回' };

// next：lists 去查名单；copy 复制原文链接；attachments 有附件时看附件、没有时复制原文链接；back 返回
function reasonView(code, hasAttachments) {
  const reason = REASONS[code];
  let next = reason ? reason.next : 'copy';
  if (next === 'attachments' && !hasAttachments) next = 'copy';
  return { text: reason ? reason.text : FALLBACK, next, nextText: NEXT_TEXT[next] || '' };
}

module.exports = { REASONS, FALLBACK, NEXT_TEXT, reasonView };
