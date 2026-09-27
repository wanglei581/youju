// 列表和详情共用的显示规则（与原型 v2 的 ds.py 一致）

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

function pad(n) {
  return n < 10 ? `0${n}` : `${n}`;
}

// 本地时区的"今天"，形如 2026-09-27
function todayISO(now) {
  const d = now || new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function parseISO(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d);
}

// 从今天到截止日还有几天（当天截止为 0，已过为负数）
function daysUntil(iso, now) {
  const today = parseISO(todayISO(now));
  return Math.round((parseISO(iso) - today) / 86400000);
}

function monthDay(iso) {
  const d = parseISO(iso);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

function cnDate(iso) {
  const d = parseISO(iso);
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

function weekday(iso) {
  return WEEK[parseISO(iso).getDay()];
}

// 截止提示：14 天内红字，7 天内再加红底；已过期单独标出
function dueTone(days) {
  if (days === undefined || days === null) return '';
  if (days < 0) return 'past';
  if (days <= 7) return 'urgent';
  if (days <= 14) return 'soon';
  return '';
}

// 截止信息：只有人工核对过的才显示（合规红线），否则返回 null
function dueInfo(deadline, now) {
  if (!deadline || !deadline.verified || !deadline.date) return null;
  const days = daysUntil(deadline.date, now);
  const at = `${monthDay(deadline.date)}${deadline.time ? ` ${deadline.time}` : ''}`;
  return {
    days,
    tone: dueTone(days),
    text: days < 0 ? `${at}已截止` : `${at}截止 · 剩${days}天`,
  };
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

// "今天 10:42" 或 "9月27日 10:42"（按手机本地时间）
function shortTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return day === todayISO() ? `今天 ${hm}` : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

module.exports = {
  shortTime,
  todayISO, daysUntil, monthDay, cnDate, weekday, dueTone, dueInfo, kindClass, statusClass, highlight, ISSUER_WRAP,
};
