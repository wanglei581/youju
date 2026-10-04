// AI 内测入口带上当前通知（编号、标题）；没有通知时打开入口页，只能看任务。
function decode(value) {
  try { return decodeURIComponent(value); } catch (e) { return value; }
}

function url(properties = {}) {
  const base = '/pages/advisor/index';
  if (properties.docId) return `${base}?docId=${encodeURIComponent(properties.docId)}&title=${encodeURIComponent(properties.docTitle || '')}`;
  return base;
}

function read(query = {}) {
  // 链接损坏时忽略上下文，入口页照常打开。
  if (!query || !query.docId) return null;
  const title = decode(query.title || '');
  return { docId: decode(String(query.docId)), title, label: `当前通知：${title}` };
}

module.exports = { url, read };
