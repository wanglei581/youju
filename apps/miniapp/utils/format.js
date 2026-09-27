// 列表和详情共用的显示规则（与原型 v2 的 ds.py 一致）

// 截止提示：14 天内红字，7 天内再加红底
function dueTone(days) {
  if (days === undefined || days === null) return '';
  if (days <= 7) return 'urgent';
  if (days <= 14) return 'soon';
  return '';
}

// 类型标签颜色：申报=蓝，名单=橙，政策=绿
function kindClass(kind) {
  return { 申报通知: 'apply', 名单: 'list', 政策: 'policy' }[kind] || 'plain';
}

function statusClass(status) {
  return status === '公示' ? 'public' : 'final';
}

// 把标题按关键词切成段，用于高亮
function highlight(text, q) {
  if (!q || !text) return [{ t: text || '', hl: false }];
  const out = [];
  let from = 0;
  let at = text.indexOf(q);
  while (at !== -1) {
    if (at > from) out.push({ t: text.slice(from, at), hl: false });
    out.push({ t: q, hl: true });
    from = at + q.length;
    at = text.indexOf(q, from);
  }
  if (from < text.length) out.push({ t: text.slice(from), hl: false });
  return out;
}

// 发文机关超过 11 个字时单独一行，不截断
const ISSUER_WRAP = 11;

module.exports = { dueTone, kindClass, statusClass, highlight, ISSUER_WRAP };
