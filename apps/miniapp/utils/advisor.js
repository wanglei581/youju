const api = require('./api.js');

const terms = require('../content/advisor-terms.js');
const VERSION = terms.version;
const TERMS_TEXT = terms.text;
const TERMS_FINAL = terms.final && !!terms.text.trim() && !/待.*定稿|占位/.test(terms.text);
// 名字和购买页、奖励规则里的写法保持一致
const KINDS = { compare: '批量名单比对', track: '客户相关通知表', interpret: '政策解读', clause: '条款对照' };
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

// 《人工智能生成合成内容标识办法》要求显式标识：如实写明由 AI 模型生成。
const INTERNAL_PREFIX = '内部测试：';
const INTERNAL_NOTICE = '结果由 AI 模型根据原文生成，每条都附原文引用，请以原文为准，不作为能否申报的判断。';

// 后端 capabilities 的 disclosure 如果带了说明文字（字符串或 text 字段），优先显示后端的；
// 目前线上只返回 modelLabel、filingNo，所以显示固定文字。
function internalNotice(disclosure) {
  let text = '';
  if (typeof disclosure === 'string') text = disclosure;
  else if (disclosure && typeof disclosure.text === 'string') text = disclosure.text;
  text = text.trim();
  if (text.startsWith(INTERNAL_PREFIX)) text = text.slice(INTERNAL_PREFIX.length).trim();
  return text || INTERNAL_NOTICE;
}

// 只保留当前账号有权限的 AI 任务；条款对照还要 features 里有 clause。
function allowedKinds(ai) {
  if (!ai || ai.enabled !== true) return [];
  const features = Array.isArray(ai.features) ? ai.features : [];
  return ['interpret', 'clause'].filter((kind) => features.includes(kind));
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

function openResult(id, options = {}) {
  const exporter = require('./exporter.js');
  const exportsLog = require('./exports.js');
  return api.downloadAdvisorTask(id).then((result) => {
    if (!api.isLive()) return wx.base64ToArrayBuffer(result.contentBase64);
    // 结果文件经接口一段一段取回来再拼上（每段不超过 900 KiB），不用 wx.downloadFile
    const parts = result.parts > 0 ? result.parts : 1; const chunks = [];
    const next = (index) => index >= parts ? Promise.resolve()
      : api.advisorTaskPart(id, index).then((chunk) => { chunks.push(new Uint8Array(chunk)); return next(index + 1); });
    return next(0).then(() => {
      const total = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
      if (!total || (result.size && total !== result.size)) throw new Error('文件没有取完整，请重试');
      const whole = new Uint8Array(total); let offset = 0;
      chunks.forEach((chunk) => { whole.set(chunk, offset); offset += chunk.byteLength; });
      return whole.buffer;
    });
  }).then((buffer) => exporter.save(buffer, 'xlsx').then((filePath) => {
    const task = options.task || {};
    const record = {
      id: `advisor-${id}`, source: 'advisor', taskId: id, kind: task.kind,
      label: KINDS[task.kind] || '工具任务结果', count: task.rowCount || 0,
      format: 'xlsx', size: buffer.byteLength, at: new Date().toISOString(), path: filePath,
    };
    if (!exportsLog.add(record, record.id)) throw new Error('文件已保存，导出记录没能写入，请重试');
    exporter.clearOld(wx.getFileSystemManager(), filePath);
    return exporter.open(filePath, 'xlsx');
  }));
}

module.exports = { VERSION, TERMS_TEXT, TERMS_FINAL, KINDS, STATES, csvRows, pasteRows, taskView, consent, openResult, INTERNAL_PREFIX, INTERNAL_NOTICE, internalNotice, allowedKinds };
