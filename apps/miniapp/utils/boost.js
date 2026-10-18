// 加量体验（D-070 ③）和申报季加量（⑤）的页面文字。规则和数字都以服务端 /advisor/status 为准（后端 boost.py），
// 这里只拼文字；字段缺了或格式不对就当没有这项活动，不猜。
// 体验规则：每个微信一次；点"开始体验"起连续 72 小时，不能暂停；每次 500 家以内，名单比对和通知表合计每天 10 次
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

// 体验当天次数用完时的提示（和服务端 TRIAL_DAILY_LIMIT 的说法一致）
function trialCapText(view) {
  return `今天的 ${view.trialDaily} 次加量体验已用完，明天 0 点恢复；本月免费次数不受影响，体验结束后照常可用`;
}

// 批量工具页上一直在的入口（有资格时）：写清楚是什么、多久、一次性
function trialPitch(view) {
  return `加量体验（免费，每个微信一次）：从点“开始体验”起连续 ${view.trialHours / 24} 天，每次最多 ${view.trialRows} 家，两项合计每天 ${view.trialDaily} 次，不占本月免费次数。名单准备好再开始。`;
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
  const back = `${format.monthDay(nextDay(season.end))}起回到每月 ${normal} 次，本月已用的次数照算`;
  return season.active === true
    ? `申报季加量：${range}，免费次数每月 ${limit} 次（每次仍 ${rows} 家以内）；${back}`
    : `申报季加量将于${format.monthDay(season.start)}开始：${range}免费次数每月 ${limit} 次（每次仍 ${rows} 家以内）`;
}

// 确认页（点"开始"之前）要写全的每一项（D-070）：几天、不能暂停、每次多少家、每天几次、不用填付款信息、
// 结束后回到免费版、本月还剩几次免费、结果结束后照样能看能导出；再加上"每个微信一次"和预计结束时间
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
  const free = compare === null || track === null ? '本月剩余的免费次数'
    : (compare + track === 0 ? '本月免费次数已经用完（体验另算，不受影响）'
      : `本月免费还剩：名单比对 ${compare} 次、客户相关通知表 ${track} 次`);
  return [
    { label: `连续 ${view.trialHours / 24} 天`, text: `从你点“开始体验”起连续 ${view.trialHours} 小时，中途不能暂停；现在开始的话，到 ${end}（北京时间）结束。` },
    { label: '每次多少家', text: `批量名单比对、客户相关通知表每次最多 ${view.trialRows} 家（平时每次 ${normal} 家）。` },
    { label: '每天几次', text: `两项合计每天最多 ${view.trialDaily} 次，北京时间 0 点重新计数。` },
    { label: '不占免费次数', text: `${free}。体验期间不扣，体验结束后照常可用。` },
    { label: '不用付款', text: `不需要填写任何付款信息。体验结束后自动回到免费版：每次 ${normal} 家以内，每月免费次数照常。` },
    { label: '结果', text: '体验期间生成的结果保存 30 天，体验结束后照样能看、能导出。' },
    { label: '只能一次', text: '跟着微信号走：每个微信号只能体验一次，用同一个微信号的同事共用这一次。建议客户名单准备好再开始。' },
  ];
}

// 不能开始时给用户看的原因
function unavailableText(view) {
  if (view.trialActive) return '';
  if (view.trialReason === 'used') return '加量体验已经用过，每个微信只能体验一次。';
  if (view.trialReason === 'pass') return '使用期内不限次数，不需要加量体验；体验留到以后再用。';
  return '加量体验暂未开放。';
}

module.exports = { trialView, trialLine, trialCapText, trialPitch, seasonLine, confirmItems, unavailableText, timeMs, beijingDay };
