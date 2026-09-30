// 顾问演示接口只保存任务元数据，不把用户选的文件和客户行写进本机缓存。
const VERSION = 'advisor-2026-09-v1';
const STORAGE = 'youjuAdvisorDemo';
let state = { tasks: [], usage: {}, nextId: 1, agreed: null };
try { const saved = wx.getStorageSync(STORAGE); if (saved && saved.tasks) state = saved; } catch (e) { /* 首次演示没有缓存 */ }

function save() { try { wx.setStorageSync(STORAGE, state); } catch (e) { /* 演示仍可在本次启动内继续 */ } }
function copy(value) { return Promise.resolve(JSON.parse(JSON.stringify(value))); }
function error(message, status) { const err = new Error(message); err.status = status; return err; }
function day() { return new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10); }
function usage(kind) { return state.usage[`${day()}:${kind}`] || 0; }
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
function status() { return copy({ enabled: true, quota: { compare: { used: usage('compare'), limit: 1, maxRows: 50 }, track: { used: usage('track'), limit: 1, maxRows: 50 } } }); }

function xmlText(value) {
  return value.replace(/&#x([a-f\d]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, '&');
}
function texts(xml) { return [...xml.matchAll(/<t(?:\s[^>]*)?>([\s\S]*?)<\/t>/g)].map((m) => xmlText(m[1])).join(''); }
function sheetRows(xml, shared) {
  return [...xml.matchAll(/<row(?:\s[^>]*)?>([\s\S]*?)<\/row>/g)].map((match) => {
    const row = [];
    for (const cell of match[1].matchAll(/<c\b([^>]*)>([\s\S]*?)<\/c>/g)) {
      const ref = /\br="([A-Z]+)\d+"/.exec(cell[1]);
      if (!ref) continue;
      const index = [...ref[1]].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0) - 1;
      const value = /<v>([\s\S]*?)<\/v>/.exec(cell[2]);
      row[index] = /\bt="s"/.test(cell[1]) ? shared[Number(value && value[1])] || '' : (value ? xmlText(value[1]) : texts(cell[2]));
    }
    return row;
  });
}

function xlsxRows(buffer) {
  const fs = wx.getFileSystemManager();
  const base = `${wx.env.USER_DATA_PATH}/advisor-preview-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      try { fs.unlinkSync(`${base}.zip`); } catch (e) { /* 未写入 */ }
      try { fs.rmdirSync(base, true); } catch (e) { /* 未解压 */ }
    };
    try { fs.writeFileSync(`${base}.zip`, buffer); } catch (e) { cleanup(); reject(error('演示表格没能读取，请重试', 422)); return; }
    fs.unzip({ zipFilePath: `${base}.zip`, targetPath: base, success: () => {
      try {
        let shared = [];
        try { shared = [...fs.readFileSync(`${base}/xl/sharedStrings.xml`, 'utf8').matchAll(/<si(?:\s[^>]*)?>([\s\S]*?)<\/si>/g)].map((m) => texts(m[1])); } catch (e) { /* 内联文字无需共享字符串表 */ }
        const rows = sheetRows(fs.readFileSync(`${base}/xl/worksheets/sheet1.xml`, 'utf8'), shared);
        resolve(rows);
      } catch (e) { reject(error('无法读取 XLSX 文件', 422)); }
      finally { cleanup(); }
    }, fail: () => { cleanup(); reject(error('无法读取 XLSX 文件', 422)); } });
  });
}

function fileRows(file) {
  if (!/\.(xlsx|csv)$/i.test(file.name)) return Promise.reject(error('只支持 .xlsx 和 .csv 文件', 422));
  let buffer;
  try { buffer = wx.base64ToArrayBuffer(file.content_base64); } catch (e) { return Promise.reject(error('文件编码无效', 422)); }
  if (buffer.byteLength > 60 * 1024) return Promise.reject(error('文件不能超过 60 KB', 422));
  const bytes = new Uint8Array(buffer);
  if (bytes[0] === 80 && bytes[1] === 75) return xlsxRows(buffer);
  try {
    const encoded = Array.from(bytes, (n) => `%${n.toString(16).padStart(2, '0')}`).join('');
    const text = decodeURIComponent(encoded);
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(text)) throw new Error();
    return Promise.resolve(require('../utils/advisor.js').csvRows(text));
  } catch (e) { return Promise.reject(error('演示模式请使用 UTF-8 CSV 或 XLSX 文件', 422)); }
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
      return (hasHeader ? rows.slice(1) : rows).map((row) => row[hasHeader ? column : 0] || '');
    }
    const name = header.indexOf('客户名称'); const province = header.indexOf('省份');
    if (name < 0 || province < 0) throw error('客户画像必须包含“客户名称”和“省份”表头', 422);
    return rows.slice(1).filter((row) => row[name] || row[province]).map((row) => ({ name: row[name] || '', province: row[province] || '', keywords: row[header.indexOf('关键词')] || '', supports: row[header.indexOf('支持方式')] || '' }));
  }).then((rows) => { if (!rows.length || (body.kind === 'compare' && !rows.some(Boolean))) throw error('文件里没有可提交的客户行', 422); return rows; });
}
function preview(body) { return inputRows(body).then((rows) => ({ rowCount: rows.length })); }
function create(body, key) {
  const existing = state.tasks.find((task) => task.key === key);
  if (existing) return copy(publicTask(existing));
  return inputRows(body).then((rows) => {
    if (rows.length > 50) throw error('每次最多提交 50 行', 429);
    if (usage(body.kind) >= 1) throw error('今天的免费额度已用完，请明天再试', 429);
    const task = { id: state.nextId++, key, kind: body.kind, status: 'queued', rowCount: rows.length, attempts: 0, createdAt: new Date(Date.now()).toISOString(), startedAt: null, finishedAt: null, expiresAt: new Date(Date.now() + 30 * 86400000).toISOString(), hasFile: false, error: null };
    state.tasks.unshift(task); state.usage[`${day()}:${body.kind}`] = usage(body.kind) + 1; save();
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
  return Promise.resolve().then(() => { const task = find(id); if (task.status !== 'done') throw error('结果尚未生成', 410); return { url: 'mock://advisor-result', expiresIn: 600, contentBase64: require('./advisor-result.js') }; });
}
function myData() {
  tick();
  return copy({ user: state.agreed ? { openid: 'demo-advisor-user', createdAt: state.agreed.at, lastVisitedAt: new Date(Date.now()).toISOString(), agreed: null, advisorAgreed: state.agreed } : null, follows: [], subscriptions: [], subscriptionsTotal: 0, feedback: [], feedbackTotal: 0, events: [], eventsTotal: 0, advisorTasks: state.tasks.map(publicTask), advisorTasksTotal: state.tasks.length, exportedAt: new Date(Date.now()).toISOString(), retention: { feedback: 180, usedSubscriptions: 30, unusedSubscriptions: 365, events: 90 } });
}
function clear() { state = { tasks: [], usage: {}, nextId: 1, agreed: null }; save(); return Promise.resolve(); }

module.exports = { me, agree, status, preview, create, list, get, cancel, remove, download, myData, clear };
