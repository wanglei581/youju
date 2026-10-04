const KIND_LABEL = { all: '全部类型', 申报通知: '申报通知', 名单: '名单与公示', 政策: '政策' };

// 关注条件的一句话说明，如"山东、北京 · 申报通知 · 近 30 天"
function describe(c) {
  const parts = [];
  if (c.q) parts.push(`"${c.q}"`);
  const regions = (c.regions || '').split(',').filter((r) => r && r !== '不限地区');
  parts.push(regions.length ? regions.join('、') : '不限地区');
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

module.exports = { describe, searchUrl };
