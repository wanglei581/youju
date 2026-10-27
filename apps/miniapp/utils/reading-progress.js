// 阅读页记住读到哪一页（D-097）：每份通知的正文、每个附件各记一条（内容版本、续读位置、第几页）。
// 内容版本变了这一条就作废（阅读接口回 409，阅读页从第 1 页开始）。只存在本机，最多记 MAX 条，
// 最久没读的先删；读到第 1 页或读不了的不记。
const KEY = 'readerProgress';
const MAX = 100;

function store() {
  return typeof wx !== 'undefined' && wx && typeof wx.getStorageSync === 'function' ? wx : null;
}

function all() {
  const s = store();
  if (!s) return {};
  try {
    const value = s.getStorageSync(KEY);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch (e) {
    return {};
  }
}

function write(map) {
  const s = store();
  if (!s) return;
  try { s.setStorageSync(KEY, map); } catch (e) { /* 存不了下次从第 1 页读，不影响阅读 */ }
}

function keyOf(docId, part) {
  return `${docId}:${part || 'body'}`;
}

// 记着的位置：内容版本、续读游标（上一页最后一段的段号）、第几页；没有或格式不对给 null
function get(docId, part) {
  const item = all()[keyOf(docId, part)];
  if (!item || typeof item.revision !== 'string' || !/^[0-9a-f]{64}$/.test(item.revision)) return null;
  if (typeof item.cursor !== 'string' || !/^[1-9][0-9]{0,8}$/.test(item.cursor)) return null;
  if (!Number.isInteger(item.page) || item.page < 2) return null;
  return { revision: item.revision, cursor: item.cursor, page: item.page };
}

function save(docId, part, revision, cursor, page) {
  const map = all();
  const key = keyOf(docId, part);
  if (!(page > 1) || cursor === '0') {
    if (map[key]) { delete map[key]; write(map); }
    return;
  }
  map[key] = { revision, cursor, page, at: Date.now() };
  const keys = Object.keys(map);
  if (keys.length > MAX) {
    keys.sort((a, b) => (Number(map[a].at) || 0) - (Number(map[b].at) || 0))
      .slice(0, keys.length - MAX).forEach((k) => { delete map[k]; });
  }
  write(map);
}

function clear(docId, part) {
  const map = all();
  const key = keyOf(docId, part);
  if (map[key]) { delete map[key]; write(map); }
}

module.exports = { get, save, clear, KEY, MAX };
