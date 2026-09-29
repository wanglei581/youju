const KIND_LABEL = { all: '全部类型', 申报通知: '申报通知', 名单: '名单与公示', 政策: '政策' };

// 关注条件的一句话说明，如"山东、北京 · 申报通知 · 近 30 天"
function describe(c) {
  const parts = [];
  if (c.q) parts.push(`"${c.q}"`);
  const regions = (c.regions || '').split(',').filter((r) => r && r !== '不限地区');
  parts.push(regions.length ? regions.join('、') : '不限地区');
  parts.push(KIND_LABEL[c.kind] || '全部类型');
  if (c.time) parts.push(c.time);
  return parts.join(' · ');
}

module.exports = { describe };
