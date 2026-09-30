// 列表和详情共用的显示规则（与原型 v2 的 ds.py 一致）

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const DAY_MS = 86400000;
const BEIJING_OFFSET = 8 * 60 * 60 * 1000;

function pad(n) {
  return n < 10 ? `0${n}` : `${n}`;
}

// 用 UTC 读加了 8 小时的时间，避免手机时区和夏令时影响北京时间。
function beijingDate(now) {
  return new Date((now || new Date()).getTime() + BEIJING_OFFSET);
}

// 北京时间的"今天"，形如 2026-09-27
function todayISO(now) {
  const d = beijingDate(now);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

function parseISO(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

// 从今天到截止日还有几天（当天截止为 0，已过为负数）
function daysUntil(iso, now, time) {
  const today = parseISO(todayISO(now));
  const date = parseISO(iso);
  const days = Math.round((date - today) / DAY_MS);
  // 只有日期时当天仍有效；有明确时刻时，当天到点就转为已截止。
  const match = typeof time === 'string' && /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(time.trim());
  if (days === 0 && match) {
    const hours = Number(match[1]); const minutes = Number(match[2]); const seconds = Number(match[3] || 0);
    if (hours < 24 && minutes < 60 && seconds < 60) {
      const cutoff = date.getTime() - BEIJING_OFFSET + (hours * 3600 + minutes * 60 + seconds) * 1000;
      if ((now || new Date()).getTime() >= cutoff) return -1;
    }
  }
  return days;
}

function monthDay(iso) {
  const d = parseISO(iso);
  return `${d.getUTCMonth() + 1}月${d.getUTCDate()}日`;
}

function cnDate(iso) {
  const d = parseISO(iso);
  return `${d.getUTCFullYear()}年${d.getUTCMonth() + 1}月${d.getUTCDate()}日`;
}

function weekday(iso) {
  return WEEK[parseISO(iso).getUTCDay()];
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
  const days = daysUntil(deadline.date, now, deadline.time);
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

// "今天 10:42"、"9月27日 10:42"，不是今年的带年份"2025年9月27日 10:42"（北京时间）
function shortTime(iso) {
  if (!iso) return '';
  const d = beijingDate(new Date(iso));
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  const day = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  if (day === todayISO()) return `今天 ${hm}`;
  const year = d.getUTCFullYear() === beijingDate().getUTCFullYear() ? '' : `${d.getUTCFullYear()}年`;
  return `${year}${d.getUTCMonth() + 1}月${d.getUTCDate()}日 ${hm}`;
}

module.exports = {
  shortTime,
  todayISO, daysUntil, monthDay, cnDate, weekday, dueTone, dueInfo, kindClass, statusClass, highlight, ISSUER_WRAP,
};
