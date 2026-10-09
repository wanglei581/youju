const FILTER_KEYS = ['regions', 'levels', 'types', 'categories', 'supports', 'industries', 'status', 'time', 'depts', 'open'];
function decode(value) {
  try { return decodeURIComponent(value); } catch (e) { return value; }
}

function searchContext(q, kind, picked) {
  const values = picked || {};
  return {
    q: (q || '').trim(), kind: kind || 'all',
    regions: (values.region || []).join(','), types: (values.type || []).join(','),
    levels: (values.level || []).join(','), categories: (values.category || []).join(','),
    supports: (values.support || []).join(','), industries: (values.industry || []).join(','),
    status: (values.status || []).join(','), time: (values.time || [])[0] || '',
    depts: (values.dept || []).join(','), open: (values.open || []).includes('只看未截止') ? 1 : 0,
  };
}

function describe(filters) {
  const kind = { all: '全部类型', 名单: '名单与公示' };
  const parts = [];
  if (filters.q) parts.push(`“${filters.q}”`);
  parts.push(kind[filters.kind] || filters.kind || '全部类型');
  FILTER_KEYS.forEach((key) => {
    if (key === 'open') { if (filters.open) parts.push('只看未截止'); }
    else if (filters[key]) parts.push(filters[key].split(',').join('、'));
  });
  return parts.join(' · ');
}

function url(properties) {
  const base = '/pages/advisor/index';
  if (properties.docId) return `${base}?docId=${encodeURIComponent(properties.docId)}&title=${encodeURIComponent(properties.docTitle || '')}`;
  if (properties.source === 'search') {
    return `${base}?filters=${encodeURIComponent(JSON.stringify(searchContext(properties.keyword, properties.kind, properties.filters)))}`;
  }
  return base;
}

function read(query = {}) {
  // 链接损坏时直接忽略上下文，仍可使用数据工具。
  try {
    if (query.docId) {
      const title = decode(query.title || '');
      return { docId: decode(query.docId), title, label: `当前通知：${title}` };
    }
    if (query.filters) {
      let input;
      try { input = JSON.parse(query.filters); } catch (e) { input = JSON.parse(decode(query.filters)); }
      const filters = { q: typeof input.q === 'string' ? input.q : '', kind: typeof input.kind === 'string' ? input.kind : 'all' };
      FILTER_KEYS.forEach((key) => { filters[key] = key === 'open' ? (input[key] === 1 ? 1 : 0) : (typeof input[key] === 'string' ? input[key] : ''); });
      return { filters, label: `当前筛选：${describe(filters)}` };
    }
  } catch (e) { /* 上下文只作提示，不能阻止打开工具页 */ }
  return null;
}

module.exports = { searchContext, describe, url, read };
