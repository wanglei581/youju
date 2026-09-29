// 演示数据模式下的搜索筛选（规则与服务端 /api/v1/search 一致）。接真实接口后只在离线开发时使用。
const { todayISO, daysUntil } = require('./format.js');

const TYPE_KIND = { 申报通知: '申报通知', 名单与公示: '名单', 政策文件: '政策' };
const TIME_DAYS = { '近 7 天': 7, '近 30 天': 30, 今年: 'year', 不限: 0 };

function split(value) {
  return (value || '').split(',').map((v) => v.trim()).filter(Boolean);
}

function matchRegion(doc, regions) {
  const picked = regions.filter((r) => r !== '不限地区');
  if (!picked.length) return true;
  return picked.some((r) => (r === '国家部委' ? doc.region.startsWith('国家') : doc.region.startsWith(r)));
}

function matchType(doc, types) {
  if (!types.length) return true;
  return types.some((t) => (t === '其他' ? !['申报通知', '名单', '政策'].includes(doc.kind) : doc.kind === TYPE_KIND[t]));
}

function deptOf(doc) {
  const s = doc.issuer || '';
  if (/科学技术|科技/.test(s)) return '科技部门';
  if (/工业和信息化|经济和信息化|工信|经信/.test(s)) return '工信部门';
  return '其他部门';
}

function matchTime(doc, time) {
  const v = TIME_DAYS[time];
  if (!v) return true;
  if (v === 'year') return (doc.date || '').slice(0, 4) === todayISO().slice(0, 4);
  return -daysUntil(doc.date) <= v;
}

function matchOpen(doc) {
  return !!(doc.deadline && doc.deadline.verified && daysUntil(doc.deadline.date) >= 0);
}

// params 与接口相同：q, kind, regions, types, status, time, depts, limit, offset
function mockSearch(all, params) {
  const kw = (params.q || '').trim();
  const kind = params.kind || 'all';
  const regions = split(params.regions);
  const types = split(params.types);
  const status = split(params.status);
  const depts = split(params.depts);
  const base = all.filter((d) => (!kw || d.title.includes(kw))
    && (kind === 'all' || d.kind === kind)
    && matchRegion(d, regions) && matchType(d, types)
    && (!status.length || status.includes(d.status))
    && (!params.open || matchOpen(d))
    && (!depts.length || depts.includes(deptOf(d))));
  const timed = base.filter((d) => matchTime(d, params.time || '近 30 天'));
  const offset = params.offset || 0;
  const limit = params.limit || 20;
  return { q: kw, total: timed.length, total_all: base.length, items: timed.slice(offset, offset + limit) };
}

module.exports = { mockSearch };
