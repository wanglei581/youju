const KIND_LABEL = { all: '全部类型', 申报通知: '申报通知', 名单: '名单与公示', 政策: '政策' };

function split(value) {
  return (value || '').split(',').map((v) => v.trim()).filter(Boolean);
}

// 地区和层级同时选时两项都要满足（与服务端 /api/v1/search 一致）。"国家部委"在两组里是同一类
// 来源；地区只选了省份而层级只选国家部委，或地区只选国家部委而层级只选省级，必然查不到，
// 搜索页在空结果里据此提示调整。
function levelConflict(regions, levels) {
  const picked = split(regions).filter((r) => r !== '不限地区');
  const lv = split(levels);
  if (!picked.length || !lv.length) return false;
  const national = picked.includes('国家部委') && lv.includes('国家部委');
  const provincial = picked.some((r) => r !== '国家部委') && lv.includes('省级');
  return !national && !provincial;
}

// 关注条件的一句话说明，如"山东、北京 · 省级 · 申报通知 · 近 30 天"
function describe(c) {
  const parts = [];
  if (c.q) parts.push(`"${c.q}"`);
  const regions = split(c.regions).filter((r) => r !== '不限地区');
  parts.push(regions.length ? regions.join('、') : '不限地区');
  const levels = split(c.levels);
  if (levels.length) parts.push(levels.join('、'));
  parts.push(KIND_LABEL[c.kind] || '全部类型');
  if (c.time) parts.push(c.time);
  if (c.open === 1 || c.open === '1') parts.push('只看未截止');
  return parts.join(' · ');
}

function searchUrl(c) {
  const params = Object.keys(c || {}).filter((k) => c[k] !== undefined && c[k] !== null && c[k] !== '')
    .map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(c[k])}`);
  return `/pages/search/index?${params.join('&')}`;
}

module.exports = { describe, searchUrl, levelConflict };
