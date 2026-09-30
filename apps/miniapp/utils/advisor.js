const api = require('./api.js');

const terms = require('../content/advisor-terms.js');
const VERSION = terms.version;
const TERMS_TEXT = terms.text;
const TERMS_FINAL = terms.final && !!terms.text.trim() && !/待.*定稿|占位/.test(terms.text);
const KINDS = { compare: '名单比对', track: '在报项目跟进表' };
const STATES = { queued: '排队中', running: '处理中', done: '已完成', failed: '失败', cancelled: '已取消' };

// 和文件解析一样保留 CSV 引号里的逗号，避免把客户名称拆成两列。
function csvRows(text, keepBlank = false) {
  const rows = []; let row = []; let cell = ''; let quoted = false; let closed = false;
  const value = text.replace(/^\uFEFF/, '');
  for (let i = 0; i < value.length; i += 1) {
    const ch = value[i];
    if (closed && ch !== ',' && ch !== '\n' && ch !== '\r') throw new Error('闭合引号后只能是逗号或换行，请检查内容');
    if (ch === '"') {
      if (quoted && value[i + 1] === '"') { cell += '"'; i += 1; }
      else if (!cell || quoted) { closed = quoted; quoted = !quoted; }
      else cell += ch;
    } else if (!quoted && (ch === ',' || ch === '\n' || ch === '\r')) {
      row.push(cell.trim()); cell = ''; closed = false;
      if (ch !== ',') { rows.push(row); row = []; if (ch === '\r' && value[i + 1] === '\n') i += 1; }
    } else cell += ch;
  }
  if (quoted) throw new Error('引号没有闭合，请检查粘贴内容');
  if (row.length || cell || (value.length && !/[\r\n]$/.test(value))) { row.push(cell.trim()); rows.push(row); }
  return keepBlank ? rows : rows.filter((cells) => cells.some(Boolean));
}

function pasteRows(kind, text) {
  if (kind === 'compare') return text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  return csvRows(text).map((row, index) => {
    if (row.length < 2 || row.length > 4 || !row[0] || !row[1]) throw new Error(`第 ${index + 1} 行请填写客户名称和省份，最多四列`);
    return { name: row[0], province: row[1], keywords: row[2] || '', supports: row[3] || '' };
  });
}

function taskView(task) {
  const stamp = task.createdAt && Date.parse(task.createdAt);
  return Object.assign({}, task, {
    kindLabel: KINDS[task.kind] || task.kind,
    stateLabel: STATES[task.status] || task.status,
    time: Number.isFinite(stamp) ? new Date(stamp + 8 * 3600000).toISOString().slice(0, 16).replace('T', ' ') : '',
    terminal: ['done', 'failed', 'cancelled'].includes(task.status),
  });
}

function consent() {
  // 每次向服务端核对；换设备、注销或版本更新后不会继续使用旧缓存。
  const general = api.isLive() ? require('./consent.js') : null;
  const epoch = general && general.consentEpoch();
  return api.me().then((res) => {
    const advisorOk = !!res.advisorAgreed && res.advisorAgreed.version === VERSION;
    if (general && !general.applyServer(res.agreed, epoch)) {
      return general.ensureConsent().then((ok) => !!ok && advisorOk);
    }
    return advisorOk;
  });
}

function openResult(id) {
  return api.downloadAdvisorTask(id).then((result) => new Promise((resolve, reject) => {
    const open = (filePath) => wx.openDocument({ filePath, fileType: 'xlsx', showMenu: true, success: resolve, fail: () => reject(new Error('文件没能打开，请重试')) });
    if (!api.isLive()) {
      // 演示结果也是有效 XLSX，在本机写入后走同一个文档查看器。
      const filePath = `${wx.env.USER_DATA_PATH}/advisor-demo-${id}.xlsx`;
      wx.getFileSystemManager().writeFile({ filePath, data: result.contentBase64, encoding: 'base64', success: () => open(filePath), fail: () => reject(new Error('演示文件没能保存，请重试')) });
      return;
    }
    wx.downloadFile({ url: result.url, success: (res) => {
      if (res.statusCode !== 200) { reject(new Error('文件下载失败，请重试')); return; }
      open(res.tempFilePath);
    }, fail: () => reject(new Error('文件下载失败，请重试')) });
  }));
}

module.exports = { VERSION, TERMS_TEXT, TERMS_FINAL, KINDS, STATES, csvRows, pasteRows, taskView, consent, openResult };
