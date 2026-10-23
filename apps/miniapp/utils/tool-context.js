// 解读页、对照页带上当前通知（编号、标题）。原来还能带着通知打开工具中心（url），10/22 工具中心改成只放批量工具，去掉了
function decode(value) {
  try { return decodeURIComponent(value); } catch (e) { return value; }
}

// 详情页的"AI 解读"直接进解读页（10/17：不再先进工具页）；解读结果下面"和我的企业对一下"进条款对照
function pageUrl(page, properties = {}) {
  const docId = properties.docId;
  const title = properties.docTitle || properties.title || '';
  return `/pages/${page}/index?docId=${encodeURIComponent(docId)}&title=${encodeURIComponent(title)}`;
}
function interpretUrl(properties) { return pageUrl('advisor-interpret', properties); }
function clauseUrl(properties) { return pageUrl('advisor-clause', properties); }

function read(query = {}) {
  // 链接损坏时忽略上下文，入口页照常打开。
  if (!query || !query.docId) return null;
  const title = decode(query.title || '');
  return { docId: decode(String(query.docId)), title, label: `当前通知：${title}` };
}

module.exports = { interpretUrl, clauseUrl, read };
