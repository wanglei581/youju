// 加量体验（D-070 ③）和申报季加量（⑤）的页面文字。规则和数字都以服务端 /advisor/status 为准（后端 boost.py），
// 这里只拼文字；字段缺了或格式不对就当没有这项活动，不猜。
// 体验规则：每个账号一次（注销即删，不留凭据）；点"开始体验"起连续 72 小时，不能暂停；每次 500 家以内，名单比对和通知表合计每天 10 次
// （北京时间 0 点重新计数）；不占每月免费次数；结束自动回到免费版。开卖之前体验相关的文字不提购买（D-070）。
const format = require('./format.js');

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const STAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|([+-])(\d{2}):?(\d{2}))$/;
const count = (value) => (Number.isInteger(value) && value > 0 ? value : 0);

// 服务端带时区的时间 → 毫秒；格式不对返回 0。不用 Date.parse：各平台对时区和 6 位小数秒的支持不一
function timeMs(iso) {
  const m = typeof iso === 'string' && STAMP.exec(iso.trim());
  if (!m) return 0;
  const offset = m[7] === 'Z' ? 0 : (m[8] === '-' ? -1 : 1) * (Number(m[9]) * 60 + Number(m[10])) * 60000;
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6] || 0)) - offset;
}

// 北京时间的日期（如 2026-10-20）：每天 10 次按它重新计数
function beijingDay(now) {
  return format.todayISO(now);
}

// 服务端的体验状态 → 页面字段。active 要有结束时间才算数，避免显示"到 结束"这样的半句话
function trialView(trial, now) {
  const none = { trialAvailable: false, trialActive: false, trialReason: '', trialLeft: 0, trialDaily: 0, trialRows: 0, trialHours: 0, trialEnd: '', trialEndsMs: 0 };
  if (!trial || typeof trial !== 'object') return none;
  const daily = count(trial.dailyLimit);
  const rows = count(trial.maxRows);
  const hours = count(trial.hours);
  if (!daily || !rows || !hours) return none;
  const end = trial.active === true ? format.clockText(trial.endsAt, now) : '';
  const used = Number.isInteger(trial.usedToday) && trial.usedToday > 0 ? trial.usedToday : 0;
  return {
    trialAvailable: trial.available === true && !end,
    trialActive: !!end,
    trialReason: typeof trial.reason === 'string' ? trial.reason : '',
    trialLeft: end ? Math.max(0, daily - used) : 0,
    trialDaily: daily, trialRows: rows, trialHours: hours, trialEnd: end,
    trialEndsMs: end ? timeMs(trial.endsAt) : 0,
  };
}

// 体验中的额度一行（批量工具页、工具中心共用）
function trialLine(view) {
  return `加量体验中，到 ${view.trialEnd} 结束：每次最多 ${view.trialRows} 家，今天还能提交 ${view.trialLeft} 次（名单比对和通知表合计）；不占本月免费次数`;
}

// 下一个北京时间 0 点（毫秒）：体验每天的次数按它重新计数
function nextMidnightMs(now) {
  const [y, m, d] = beijingDay(now).split('-').map(Number);
  return Date.UTC(y, m - 1, d + 1) - 8 * 3600 * 1000;
}

// 体验当天次数用完时的提示。体验期间不改用每月免费次数（D-070 第二轮），直接说明，免得用户以为免费次数还能用；
// 最后一天体验在明天 0 点之前就结束，不说"明天恢复"，说几点结束、之后回到免费版
function trialCapText(view, now) {
  if (view.trialEndsMs > 0 && view.trialEndsMs <= nextMidnightMs(now)) {
    return `今天的 ${view.trialDaily} 次加量体验已用完；体验到 ${view.trialEnd} 结束，之后回到免费版`;
  }
  return `今天的 ${view.trialDaily} 次加量体验已用完，明天 0 点恢复；体验期间每月免费次数不扣、也不能改用`;
}

// 体验结束后 7 天内，批量工具页额度一行下面说一句已结束（回来的人知道每次 500 家为什么没了）
const ENDED_NOTE_MS = 7 * 24 * 3600 * 1000;
function trialEndedText(trial, now) {
  if (!trial || typeof trial !== 'object' || trial.active === true || trial.reason !== 'used') return '';
  const end = timeMs(trial.endsAt);
  const at = (now || new Date()).getTime();
  if (!end || end > at || at - end > ENDED_NOTE_MS) return '';
  return `加量体验已于 ${format.clockText(trial.endsAt, now)} 结束，已回到免费版`;
}

// 批量工具页上一直在的入口（有资格时）：写清楚是什么、多久、一次性。页面上只有一项工具，"两项"要写出是哪两项
function trialPitch(view) {
  return `加量体验（免费，每个账号一次）：从点“开始体验”起连续 ${view.trialHours / 24} 天，每次最多 ${view.trialRows} 家，名单比对和通知表合计每天 ${view.trialDaily} 次，不占本月免费次数。名单准备好再开始。`;
}

function nextDay(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const next = new Date(Date.UTC(y, m - 1, d + 1));
  const pad = (n) => (n < 10 ? `0${n}` : `${n}`);
  return `${next.getUTCFullYear()}-${pad(next.getUTCMonth() + 1)}-${pad(next.getUTCDate())}`;
}

// 申报季加量的起止日期（批量工具页显示；还没开始的提前显示）
function seasonLine(season) {
  if (!season || typeof season !== 'object' || !DATE.test(season.start) || !DATE.test(season.end)) return '';
  const limit = count(season.limit);
  const normal = count(season.normalLimit);
  const rows = count(season.maxRows);
  if (!limit || !normal || !rows) return '';
  const range = `${format.monthDay(season.start)}至${format.monthDay(season.end)}`;
  // 月中结束时，当月已用的次数照算（用了 4 次的，回到 3 次后这个月不能再用）；在月底结束的下个月重新计数，不用说
  const after = nextDay(season.end);
  const back = `${format.monthDay(after)}起回到每月 ${normal} 次${after.slice(8) === '01' ? '' : '，当月已用的次数照算'}`;
  return season.active === true
    ? `申报季加量：${range}，免费次数每月 ${limit} 次（每次仍 ${rows} 家以内）；${back}`
    : `申报季加量将于${format.monthDay(season.start)}开始：${range}免费次数每月 ${limit} 次（每次仍 ${rows} 家以内）`;
}

// 确认页（点"开始"之前）要写全的每一项（D-070）：几天、不能暂停、每次多少家、每天几次、不用填付款信息、
// 结束后回到免费版、本月还剩几次免费、结果结束后照样能看能导出；再加上"每个账号一次"和预计结束时间
function confirmItems(status, now) {
  const view = trialView(status && status.trial, now);
  if (!view.trialAvailable) return [];
  const quota = (status && status.quota) || {};
  const left = (kind) => {
    const q = quota[kind] || {};
    return Number.isFinite(q.limit) && Number.isFinite(q.used) ? Math.max(0, q.limit - q.used) : null;
  };
  const compare = left('compare');
  const track = left('track');
  const normal = count((quota.compare || {}).maxRows) || 50;
  const stamp = (now || new Date()).getTime() + view.trialHours * 3600 * 1000;
  const end = format.clockText(new Date(stamp).toISOString(), now);
  // 体验期间只用体验的次数，每月免费次数不扣也不能改用（D-070 第二轮）。月底开始会跨月：
  // 本月剩下的免费次数体验期间用不了、也不顺延，开始之前说清楚，不能写"体验结束后照常可用"
  const crosses = beijingDay(new Date(stamp)).slice(0, 7) !== beijingDay(now).slice(0, 7);
  let free;
  if (compare === null || track === null) free = '体验期间只用体验的次数，每月免费次数不扣、也不能改用；体验结束后照常使用。';
  else if (compare + track === 0) free = '本月免费次数已经用完，体验的次数另算。体验期间只用体验的次数，不能改用每月免费次数；体验结束后照常使用。';
  else {
    free = `现在本月免费还剩：名单比对 ${compare} 次、客户相关通知表 ${track} 次。体验期间只用体验的次数，这些不扣、也不能改用`;
    free += crosses ? '。注意：现在开始的话体验会跨过月底，本月剩下的免费次数用不上（不顺延到下个月），想用的话先用完再开始。'
      : '，体验结束后接着用。';
  }
  return [
    { label: `连续 ${view.trialHours / 24} 天`, text: `从你点“开始体验”起连续 ${view.trialHours} 小时，中途不能暂停；现在开始的话，到 ${end}（北京时间）结束。` },
    { label: '每次多少家', text: `批量名单比对、客户相关通知表每次最多 ${view.trialRows} 家（平时每次 ${normal} 家）。` },
    { label: '每天几次', text: `两项合计每天最多 ${view.trialDaily} 次，北京时间 0 点重新计数。` },
    { label: '不占免费次数', text: free },
    { label: '不用付款', text: `不需要填写任何付款信息。体验结束后自动回到免费版：每次 ${normal} 家以内，每月免费次数照常。` },
    { label: '结果', text: '体验期间生成的结果保存 30 天，体验结束后照样能看、能导出。' },
    // 10/18 验收：原来写"同事共用这一次"，和购买页"只限本人、不能几个人共用"矛盾
    { label: '只能一次', text: '每个账号只能体验一次，只限本人使用，不能几个人共用。建议客户名单准备好再开始。' },
  ];
}

// 不能开始时给用户看的原因
function unavailableText(view) {
  if (view.trialActive) return '';
  if (view.trialReason === 'used') return '加量体验已经用过，每个账号只能体验一次。';
  if (view.trialReason === 'pass') return '使用期内不限次数，不需要加量体验；体验留到以后再用。';
  // 其余（开关关着、读不到）不给用户看"暂未开放"，确认页直接回首页（D-074 ⑧）
  return '';
}

module.exports = { trialView, trialLine, trialCapText, trialEndedText, trialPitch, seasonLine, confirmItems, unavailableText, timeMs, beijingDay };
