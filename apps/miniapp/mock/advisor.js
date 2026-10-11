// 顾问演示接口只保存任务元数据，不把用户选的文件和客户行写进本机缓存。
const VERSION = 'advisor-2026-09-v1';
const STORAGE = 'youjuAdvisorDemo';
let state = { tasks: [], usage: {}, nextId: 1, agreed: null };
try { const saved = wx.getStorageSync(STORAGE); if (saved && saved.tasks) state = saved; } catch (e) { /* 首次演示没有缓存 */ }

function save() { try { wx.setStorageSync(STORAGE, state); } catch (e) { /* 演示仍可在本次启动内继续 */ } }
function copy(value) { return Promise.resolve(JSON.parse(JSON.stringify(value))); }
function error(message, status, code) { const err = new Error(message); err.status = status; if (code) err.code = code; return err; }
function day() { return new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10); }
function usage(kind) {
  const month = day().slice(0, 7);
  return Object.keys(state.usage).filter((key) => key.startsWith(month + '-') && key.endsWith(`:${kind}`))
    .reduce((total, key) => total + state.usage[key], 0);
}
function tick() {
  state.tasks.forEach((task) => {
    if (!['queued', 'running'].includes(task.status)) return;
    const elapsed = Date.now() - Date.parse(task.createdAt);
    if (elapsed >= 6000) { task.status = 'done'; task.finishedAt = new Date(Date.now()).toISOString(); task.hasFile = true; task.resultRows = task.rowCount; }
    else if (elapsed >= 3000) { task.status = 'running'; task.startedAt = new Date(Date.now()).toISOString(); }
  });
  save();
}
function find(id) { tick(); const task = state.tasks.find((item) => item.id === Number(id)); if (!task) throw error('没有找到工具任务', 404); return task; }
function publicTask(task) { const out = Object.assign({}, task); delete out.key; return out; }
function me() { return copy({ agreed: null, advisorAgreed: state.agreed }); }
function agree(version) {
  if (version !== VERSION) return Promise.reject(error('工具使用说明已更新，请重新阅读', 422));
  state.agreed = { version, at: new Date(Date.now()).toISOString() }; save(); return copy(state.agreed);
}
function status() { return copy({ enabled: true, pass: { active: false, expiresAt: null }, quota: { compare: { used: usage('compare'), limit: 3, maxRows: 50 }, track: { used: usage('track'), limit: 3, maxRows: 50 } } }); }

function xmlText(value) {
  return value.replace(/&#x([a-f\d]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, '&');
}
function texts(xml) { return [...xml.matchAll(/<t(?:\s[^>]*)?>([\s\S]*?)<\/t>/g)].map((m) => xmlText(m[1])).join(''); }
function attribute(xml, name) {
  const match = new RegExp(`\\b${name}=["']([^"']*)["']`).exec(xml);
  return match ? xmlText(match[1]) : '';
}
function sheetRows(xml, shared) {
  const { MAX_ROWS, MAX_COLUMNS, MAX_CELLS, MAX_ELEMENTS, fail } = require('../utils/advisor-file.js');
  if ((xml.match(/<[A-Za-z_][^>]*>/g) || []).length > MAX_ELEMENTS) fail('表格内容过多，请减少内容后重试');
  const cells = [...xml.matchAll(/<c\b([^>]*?)(?:\/>|>)/g)];
  if (cells.length > MAX_CELLS) fail('表格单元格过多，请减少内容后重试');
  for (const cell of cells) {
    const ref = /^([A-Z]+)([1-9]\d*)$/.exec(attribute(cell[1], 'r'));
    const column = ref && [...ref[1]].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);
    if (!ref || column > MAX_COLUMNS) fail('表格列数过多或单元格坐标无效，请只保留客户字段');
    if (Number(ref[2]) > MAX_ROWS) fail('表格行号过大，请移除多余空行后重试');
  }
  const matches = [...xml.matchAll(/<row\b([^>]*?)(?:\/>|>([\s\S]*?)<\/row>)/g)];
  if (matches.length > MAX_ROWS) fail('表格行号过大，请移除多余空行后重试');
  const rows = []; let previous = 0;
  for (const match of matches) {
    const index = Number(attribute(match[1], 'r')) || previous + 1;
    if (index > MAX_ROWS) fail('表格行号过大，请移除多余空行后重试');
    previous = index; const row = [];
    for (const cell of (match[2] || '').matchAll(/<c\b([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g)) {
      const ref = /^([A-Z]+)\d+$/.exec(attribute(cell[1], 'r'));
      const column = [...ref[1]].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0) - 1;
      const value = /<v(?:\s[^>]*)?>([\s\S]*?)<\/v>/.exec(cell[2] || '');
      row[column] = attribute(cell[1], 't') === 's' ? shared[Number(value && value[1])] || '' : (value ? xmlText(value[1]) : texts(cell[2] || ''));
    }
    rows[index - 1] = row;
  }
  return Array.from({ length: rows.length }, (_, index) => rows[index] || []);
}
function xlsxRows(files) {
  const { utf8, fail, MAX_CELLS, MAX_ELEMENTS } = require('../utils/advisor-file.js');
  const read = (name) => { if (!files[name]) fail('无法读取 XLSX 文件'); return utf8(files[name]); };
  const types = read('[Content_Types].xml');
  if (![...types.matchAll(/<Override\b([^>]*)\/?\s*>/g)].some((m) => attribute(m[1], 'PartName') === '/xl/workbook.xml' && attribute(m[1], 'ContentType') === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')) return null;
  const relations = [...read('xl/_rels/workbook.xml.rels').matchAll(/<Relationship\b([^>]*)\/?\s*>/g)];
  const sheets = [...read('xl/workbook.xml').matchAll(/<sheet\b([^>]*)\/?\s*>/g)];
  const paths = sheets.map((sheet) => {
    const rel = relations.find((m) => attribute(m[1], 'Id') === attribute(sheet[1], 'r:id') && attribute(m[1], 'Type').endsWith('/worksheet') && attribute(m[1], 'TargetMode') !== 'External');
    if (!rel) return null;
    const target = attribute(rel[1], 'Target'); const parts = [];
    for (const part of (target.startsWith('/') ? target.slice(1) : `xl/${target}`).split('/')) {
      if (part === '..') parts.pop(); else if (part && part !== '.') parts.push(part);
    }
    const name = parts.join('/'); return files[name] ? name : null;
  }).filter(Boolean);
  if (!paths.length) return null;
  const shared = files['xl/sharedStrings.xml'] ? [...read('xl/sharedStrings.xml').matchAll(/<si(?:\s[^>]*)?>([\s\S]*?)<\/si>/g)].map((m) => texts(m[1])) : [];
  // 每个工作表都检查上限，按工作簿顺序读取第一个工作表。
  let first; let cells = 0; let elements = 0;
  paths.forEach((name, index) => {
    const xml = read(name);
    cells += (xml.match(/<c\b[^>]*>/g) || []).length;
    elements += (xml.match(/<[A-Za-z_][^>]*>/g) || []).length;
    if (cells > MAX_CELLS) fail('表格单元格过多，请减少内容后重试');
    if (elements > MAX_ELEMENTS) fail('表格内容过多，请减少内容后重试');
    const rows = sheetRows(xml, shared); if (index === 0) first = rows;
  });
  return first;
}
function fileRows(file) {
  return Promise.resolve().then(() => {
    const { csvText, zipFiles, fail } = require('../utils/advisor-file.js');
    if (!/\.(xlsx|csv)$/i.test(file.name)) fail('只支持 .xlsx 和 .csv 文件');
    let buffer;
    try { buffer = wx.base64ToArrayBuffer(file.content_base64); } catch (e) { fail('文件编码无效'); }
    if (buffer.byteLength > 60 * 1024) fail('文件不能超过 60 KB');
    const bytes = new Uint8Array(buffer);
    if (bytes[0] === 80 && bytes[1] === 75 && bytes[2] === 3 && bytes[3] === 4) {
      try { const files = zipFiles(bytes); const rows = files && xlsxRows(files); if (rows) return rows; }
      catch (err) { if (err.status === 422) throw err; fail('无法读取 XLSX 文件'); }
    }
    const text = csvText(bytes);
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(text)) fail('文件内容不是 CSV 或 XLSX 表格');
    try { return require('../utils/advisor.js').csvRows(text, true); }
    catch (err) { fail(`CSV 格式有误：${err.message}`); }
  });
}

function inputRows(body) {
  if (!state.agreed || state.agreed.version !== VERSION) return Promise.reject(error('请先阅读并同意工具使用说明', 403));
  if (body.rows) return copy(body.rows);
  return fileRows(body.file).then((rows) => {
    if (!rows.length) throw error('文件里没有可提交的客户行', 422);
    const header = rows[0];
    if (body.kind === 'compare') {
      let column = header.findIndex((cell) => ['企业名称', '单位名称', '公司名称'].includes(cell));
      if (column < 0) column = header.findIndex((cell) => ['统一社会信用代码', '信用代码'].includes(cell));
      const hasHeader = column >= 0;
      const values = (hasHeader ? rows.slice(1) : rows).map((row) => row[hasHeader ? column : 0] || '');
      if (values.length > 500) throw error('客户名单最多 500 行', 422);
      return values;
    }
    const name = header.indexOf('客户名称'); const province = header.indexOf('省份');
    if (name < 0 || province < 0) throw error('客户画像必须包含“客户名称”和“省份”表头', 422);
    if (rows.length - 1 > 500) throw error('客户画像最多 500 行', 422);
    return rows.slice(1).filter((row) => row[name] || row[province]).map((row) => ({ name: row[name] || '', province: row[province] || '', keywords: row[header.indexOf('关键词')] || '', supports: row[header.indexOf('支持方式')] || '' }));
  }).then((rows) => { if (!rows.length || (body.kind === 'compare' && !rows.some(Boolean))) throw error('文件里没有可提交的客户行', 422); return rows; });
}
function preview(body) { return inputRows(body).then((rows) => ({ rowCount: rows.length })); }
function create(body, key) {
  const existing = state.tasks.find((task) => task.key === key);
  if (existing) return copy(publicTask(existing));
  return inputRows(body).then((rows) => {
    if (rows.length > 50) throw error('每次最多提交 50 行', 429, 'FREE_ROW_LIMIT');
    if (usage(body.kind) >= 3) throw error('本月免费次数已用完', 429, 'FREE_MONTHLY_LIMIT');
    const task = { id: state.nextId++, key, kind: body.kind, status: 'queued', rowCount: rows.length, attempts: 0, createdAt: new Date(Date.now()).toISOString(), startedAt: null, finishedAt: null, expiresAt: new Date(Date.now() + 30 * 86400000).toISOString(), hasFile: false, error: null };
    state.tasks.unshift(task);
    const usageKey = `${day()}:${body.kind}`;
    state.usage[usageKey] = (state.usage[usageKey] || 0) + 1; save();
    return publicTask(task);
  });
}
function list() { tick(); return copy({ items: state.tasks.map(publicTask) }); }
function get(id) { return Promise.resolve().then(() => publicTask(find(id))); }
function cancel(id) {
  return Promise.resolve().then(() => {
    const task = find(id);
    if (task.status === 'cancelled') return publicTask(task);
    if (task.status !== 'queued') throw error('任务已开始处理，不能取消', 409);
    task.status = 'cancelled'; task.finishedAt = new Date(Date.now()).toISOString();
    const key = `${new Date(Date.parse(task.createdAt) + 8 * 3600000).toISOString().slice(0, 10)}:${task.kind}`;
    state.usage[key] = Math.max(0, (state.usage[key] || 0) - 1); save(); return publicTask(task);
  });
}
function remove(id) {
  return Promise.resolve().then(() => { const task = find(id); if (task.status === 'running') throw error('请等任务结束后再删除', 409); return task.status === 'queued' ? cancel(id) : null; })
    .then(() => { state.tasks = state.tasks.filter((item) => item.id !== Number(id)); save(); });
}
function download(id) {
  return Promise.resolve().then(() => { const task = find(id); if (task.status !== 'done') throw error('结果尚未生成', 410); return { url: 'mock://advisor-result', expiresIn: 600, contentBase64: task.kind === 'track' ? require('./advisor-track-result.js') : require('./advisor-result.js') }; });
}
function myData() {
  tick();
  return copy({ user: state.agreed ? { openid: 'demo-advisor-user', createdAt: state.agreed.at, lastVisitedAt: new Date(Date.now()).toISOString(), agreed: null, advisorAgreed: state.agreed } : null, follows: [], subscriptions: [], subscriptionsTotal: 0, feedback: [], feedbackTotal: 0, events: [], eventsTotal: 0, advisorTasks: state.tasks.map(publicTask), advisorTasksTotal: state.tasks.length, exportedAt: new Date(Date.now()).toISOString(), retention: { feedback: 180, usedSubscriptions: 30, unusedSubscriptions: 365, events: 90 } });
}
function clear() { state = { tasks: [], usage: {}, nextId: 1, agreed: null }; save(); return Promise.resolve(); }

module.exports = { me, agree, status, preview, create, list, get, cancel, remove, download, myData, clear };
