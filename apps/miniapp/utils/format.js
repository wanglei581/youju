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

// 带时区的时间（如服务端的 2026-10-06T06:30:00+08:00）→ 北京时间"10月6日 06:30"，不是今年的加上年份。
// 自己解析，不依赖各平台 Date.parse 对时区和小数秒的支持；格式不对返回空串（页面就不写时间）。
function clockText(iso, now) {
  const m = typeof iso === 'string'
    && /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|([+-])(\d{2}):?(\d{2}))$/.exec(iso.trim());
  if (!m) return '';
  const offset = m[7] === 'Z' ? 0 : (m[8] === '-' ? -1 : 1) * (Number(m[9]) * 60 + Number(m[10])) * 60000;
  const stamp = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6] || 0)) - offset;
  const d = beijingDate(new Date(stamp));
  const year = d.getUTCFullYear() === beijingDate(now).getUTCFullYear() ? '' : `${d.getUTCFullYear()}年`;
  return `${year}${d.getUTCMonth() + 1}月${d.getUTCDate()}日 ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

// 截止提示：14 天内红字，7 天内再加红底；已过期单独标出
function dueTone(days) {
  if (days === undefined || days === null) return '';
  if (days < 0) return 'past';
  if (days <= 7) return 'urgent';
  if (days <= 14) return 'soon';
  return '';
}

// 列表一行的截止文字。当天截止写"今天截止"，和首页"7 天内截止"、截止页的标签一致，不写"剩0天"（10/22 走查）
function dueText(iso, time, days) {
  const at = `${monthDay(iso)}${time ? ` ${time}` : ''}`;
  if (days < 0) return `${at}已截止`;
  if (days === 0) return `今天${time ? ` ${time}` : ''}截止`;
  return `${at}截止 · 剩${days}天`;
}

// 截止信息：只有人工核对过的才显示（合规红线），否则返回 null
function dueInfo(deadline, now) {
  if (!deadline || !deadline.verified || !deadline.date) return null;
  const days = daysUntil(deadline.date, now, deadline.time);
  return {
    days,
    tone: dueTone(days),
    text: dueText(deadline.date, deadline.time, days),
  };
}

// 申报通知没有核对过的截止日期时的说明，只给文字、不给任何日期（合规红线）：
// 有待核对的候选（deadline_state 为 unverified）写"截止未核对"，没有候选写"截止时间见原文"。
// 有核对过的日期时返回空串，由 dueInfo 显示日期；其他类型的文件不写说明。
const NOTE_UNVERIFIED = '截止未核对，请以原文为准';

// 首发版（D-061）的截止显示。接口带 excerpts=1 时每条有 due（主截止）、详情有 deadlines（逐个类型）：
// - verified：人工核对过的，写日期和剩余天数，标"已人工核对"；
// - excerpt：机器从原文摘出的，只会是 8–30 天到期的，写日期并标"机器摘录·未经人工核对"，必须带原句；
// - unverified：有候选但不显示日期（7 天内没核对的、30 天以后的、过期的、几个日期对不上的），只写"截止未核对"。
// 摘录没带原句、按手机日期算已进入 7 天内（页面放过夜、手机日期和服务端差一天）或在 30 天以后，
// 这里再拦一次，只写"截止未核对"。
const VERIFIED_LABEL = '已人工核对';
const EXCERPT_LABEL = '机器摘录·未经人工核对';
const EXCERPT_MIN_DAYS = 8;
const EXCERPT_MAX_DAYS = 30;
function dueView(due, now) {
  if (!due) return null;
  const excerpt = due.source === 'excerpt';
  if (due.date && (due.source === 'verified' || (excerpt && due.quote))) {
    const days = daysUntil(due.date, now, due.time);
    if (!excerpt || (days >= EXCERPT_MIN_DAYS && days <= EXCERPT_MAX_DAYS)) {
      return {
        source: due.source,
        kind: due.kind || '',
        days,
        tone: excerpt ? 'excerpt' : dueTone(days),
        text: dueText(due.date, due.time, days),
        label: excerpt ? EXCERPT_LABEL : VERIFIED_LABEL,
        quote: due.quote || '',
      };
    }
  }
  return { source: 'unverified', kind: due.kind || '', days: null, tone: '', text: NOTE_UNVERIFIED, label: '', quote: '' };
}
const NOTE_SEE_ORIGINAL = '截止时间见原文';
function deadlineNote(doc) {
  if (!doc || doc.kind !== '申报通知' || dueInfo(doc.deadline)) return '';
  return doc.deadline_state === 'unverified' ? NOTE_UNVERIFIED : NOTE_SEE_ORIGINAL;
}

// 截止雷达、申报日历没有内容时的说明；total 是服务端给的已核对条数（不是数字时不写条数）
function verifyingNote(total) {
  return typeof total === 'number' && total >= 0
    ? `截止日期正在人工核对中，目前已核对 ${total} 条`
    : '截止日期正在人工核对中';
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

// 列表和详情的地区：照录服务端 region（如"青岛 · 市级"）；来源带区县、region 里又没写区县名时补上，
// 写成"青岛·市南区 · 级别"，不丢区县。没有区县的原样显示
function regionText(doc) {
  const region = (doc && doc.region) || '';
  const district = doc && typeof doc.district === 'string' ? doc.district.trim() : '';
  if (!district || region.includes(district)) return region;
  const area = [doc.city && doc.city !== district ? doc.city : '', district].filter(Boolean).join('·');
  return doc.level ? `${area} · ${doc.level}` : [region, district].filter(Boolean).join(' · ');
}

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

// 截止核对断档（定稿方案第八节）：最近一次人工核对截止之后，已经过了 3 个及以上工作日（周一到周五，
// 不扣节假日）没再核对，首页写"今日未更新"；从没核对过也写。reviewedAt 是后端给的带时区的时间。
const STALE_WORKDAYS = 3;
function workdaysSince(iso, now) {
  const today = parseISO(todayISO(now));
  let count = 0;
  for (let t = parseISO(iso).getTime() + 86400000; t <= today.getTime() && count < STALE_WORKDAYS; t += 86400000) {
    const day = new Date(t).getUTCDay();
    if (day !== 0 && day !== 6) count += 1;
  }
  return count;
}
function reviewStaleNote(reviewedAt, now) {
  if (!reviewedAt) return '今日未更新：截止日期还没有人工核对';
  const stamp = Date.parse(reviewedAt);
  if (Number.isNaN(stamp)) return '';
  const day = todayISO(new Date(stamp));
  if (workdaysSince(day, now) < STALE_WORKDAYS) return '';
  return `今日未更新：截止日期最近一次人工核对是 ${monthDay(day)}`;
}

// ---- 详情页"能读到什么"（D-097）：正文开头在段末停、两处同名的阅读入口、正文卡顶上那一行、没正文时说什么 ----
// 能不能读以服务端为准（详情接口的 body 和 attachments[].readable、state、stateText，和阅读页同一个
// 判断），这里只决定页面怎么写，不另做判断。

// 不满一屏就全部显示（"全文如下"）：28rpx 字号一行约 18 个字、一屏约 16 行
const ONE_SCREEN = 300;
// 长的显示开头约 3 段，在段落末尾停；开头几段太短（"各有关单位："）就多带几段，第一段就很长的
// 在句末停
const OPENING_PARAS = 3;
const OPENING_MIN = 80;
const OPENING_MAX = 450;
const OPENING_MAX_PARAS = 6;

function aboutChars(total) {
  const n = Number(total) || 0;
  if (n < 1000) return String(Math.max(10, Math.round(n / 10) * 10));
  return String(Math.round(n / 100) * 100);
}

// 第一段就很长：在 OPENING_MAX 以内最后一个句号、分号后面停；前一半里都没有就停在 OPENING_MAX
function cutLong(text) {
  let cut = -1;
  for (let i = 0; i < Math.min(text.length, OPENING_MAX); i += 1) {
    if ('。；！？'.includes(text[i])) cut = i + 1;
  }
  return text.slice(0, cut >= OPENING_MAX / 2 ? cut : OPENING_MAX);
}

// body：详情接口的 body（status 为 ok 时有 text、total、truncated）。返回 { text, full, label }：
// full 为 true 时正文已在详情页全部显示
function opening(body) {
  const text = String((body && body.text) || '');
  const total = Number(body && body.total) || text.length;
  const truncated = !!(body && body.truncated);
  if (!truncated && total <= ONE_SCREEN) return { text, full: true, label: '全文如下' };
  const paras = text.split('\n').filter((p) => p.trim());
  const picked = [];
  let len = 0;
  for (const p of paras) {
    const enough = picked.length >= OPENING_PARAS && len >= OPENING_MIN;
    if (enough || picked.length >= OPENING_MAX_PARAS || (picked.length && len + p.length > OPENING_MAX)) break;
    picked.push(p);
    len += p.length;
  }
  let shown = picked.join('\n');
  if (shown.length > OPENING_MAX) shown = cutLong(shown);
  if (!truncated && shown.length >= paras.join('\n').length) return { text, full: true, label: '全文如下' };
  return { text: shown, full: false, label: `下面是开头，全文约 ${aboutChars(total)} 字` };
}

// 附件读不了时正文卡顶上那一行的短说法；别的原因看附件列表里每个附件后面写的
const SHORT = {
  scan: '扫描件暂不能识别文字',
  pending: '新附件一般半天内可读',
  format: '格式暂不支持',
};

// "附件 3 个，2 个能读，1 个扫描件暂不能识别文字"
function attachmentLine(atts) {
  const list = atts || [];
  if (!list.length) return '';
  const ok = list.filter((a) => a.readable).length;
  if (ok === list.length) return `附件 ${list.length} 个，${list.length > 1 ? '都' : ''}能读`;
  const rest = list.filter((a) => !a.readable);
  const states = [...new Set(rest.map((a) => a.state))];
  const why = states.length === 1 && SHORT[states[0]] ? SHORT[states[0]] : '暂不能读';
  if (ok) return `附件 ${list.length} 个，${ok} 个能读，${rest.length} 个${why}`;
  return `附件 ${list.length} 个，暂不能读（${why === '暂不能读' ? '原因见下面每个附件' : why}）`;
}

// 正文卡顶上那一行："正文约 3200 字 · 附件 3 个，2 个能读，1 个扫描件暂不能识别文字"
function summaryLine(body, atts) {
  const parts = [];
  if (body && body.status === 'ok') parts.push(`正文约 ${aboutChars(body.total)} 字`);
  const line = attachmentLine(atts);
  if (line) parts.push(line);
  return parts.join(' · ');
}

// 没正文时写的话（设计稿第三节）；名单类正文主要是名单的、个人事项的也在这里
function noBodyText(body) {
  if (!body) return '';
  if (body.status === 'list') return '这份通知的正文里有名单，名单请在“查名单”里按企业名称查询';
  if (body.status === 'original') return '含个人信息，不在小程序内展示全文';
  if (body.reason === 'attachments') return '这份通知的内容在附件里';
  if (body.reason === 'link_gone') return '原网页已打不开，正文暂不提供';
  return '这个网站的正文还取不到';
}

// 正文下面和底栏两处同名的入口（D-097）：正文只显示了开头 → "阅读全文"（去阅读页看正文）；正文已全部
// 显示或没有正文、但有能读的附件 → "阅读附件"（去阅读页看第一个能读的附件）；都读不了 → 不显示
function readEntry(body, view, atts) {
  if (body && body.status === 'ok' && view && !view.full) return { label: '阅读全文', attachment: '' };
  const first = (atts || []).find((a) => a.readable);
  return first ? { label: '阅读附件', attachment: String(first.id) } : null;
}

module.exports = {
  reviewStaleNote,
  shortTime,
  todayISO, daysUntil, monthDay, cnDate, weekday, clockText, dueTone, dueInfo, kindClass, statusClass, highlight, ISSUER_WRAP, regionText,
  deadlineNote, verifyingNote, NOTE_UNVERIFIED, NOTE_SEE_ORIGINAL, dueView, VERIFIED_LABEL, EXCERPT_LABEL,
  ONE_SCREEN, OPENING_MAX, aboutChars, opening, attachmentLine, summaryLine, noBodyText, readEntry,
};
