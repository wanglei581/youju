// 数据工具只走真实后端；不在本机解析 XLSX 或保存客户输入。
const api = require('./api.js');
const regions = require('./regions.js');
const terms = require('../content/advisor-terms.js');
const { DATA_KINDS } = require('./advisor.js');

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

// 城市规则与服务端 track.CustomerProfile 一致：只填一个、不超过 32 字、不含分隔符；直辖市填本市按省级；
// 国家部委不带城市。关键词、支持方式仍可用分号填多项。空行参与编号，第 N 行与输入框所见一致。
function pasteRows(kind, text) {
  if (kind === 'compare') return text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  const result = [];
  csvRows(text, true).forEach((row, index) => {
    if (!row.some(Boolean)) return;
    const line = `第 ${index + 1} 行`;
    if (row.length > 5) throw new Error(`${line}最多五列：客户名称,省份,关键词,支持方式,城市；城市只填一个`);
    if (!row[0] || !row[1]) throw new Error(`${line}请填写客户名称和省份`);
    const province = regions.provinceName(row[1]) || row[1];
    let city = regions.cityName(row[4] || '');
    if (['北京', '天津', '上海', '重庆'].includes(province) && city === province) city = '';
    if (city && ['国家', '国家部委'].includes(province)) throw new Error(`${line}省份为国家部委时城市请留空`);
    if (city.length > 32 || /[\/,，;；\n\r]/.test(city)) throw new Error(`${line}城市只填一个，不超过 32 字，不用逗号、分号或斜杠`);
    result.push({ name: row[0], province, keywords: row[2] || '', supports: row[3] || '', city });
  });
  return result;
}

function toolConsent(options = {}) {
  return api.me().then((res) => {
    const agreed = !!res.advisorAgreed && res.advisorAgreed.version === terms.version;
    if (!options.identity) return agreed;
    if (agreed && !/^[a-f0-9]{64}$/.test(res.taskOwner || '')) throw new Error('服务版本尚未支持安全重试，请稍后重试');
    return { agreed, owner: res.taskOwner || '' };
  });
}

function quotaView(status, kind) {
  const quota = status && status.quota && status.quota[kind];
  if (!status || !status.enabled || !quota || !Number.isFinite(quota.limit) || !Number.isFinite(quota.used) || !Number.isFinite(quota.maxRows)) throw new Error('未能读取工具额度，请重试');
  const active = !!(status.pass && status.pass.active);
  const remaining = Math.max(0, quota.limit - quota.used);
  return { maxRows: quota.maxRows, remaining, passActive: active,
    statusLine: active ? `工作台使用期内，每次最多 ${quota.maxRows} 家，不限提交次数` : `每月免费 ${quota.limit} 次，本月剩余 ${remaining} 次；每次最多 ${quota.maxRows} 家` };
}

// 服务端最多 20 MiB、每段 900 KiB。预先限制元数据并逐段校验，避免异常元数据导致无界分配。
const MAX_RESULT_BYTES = 20 * 1024 * 1024;
const PART_BYTES = 900 * 1024;
function openResult(id, options = {}) {
  const exporter = require('./exporter.js');
  const log = require('./exports.js');
  const current = options.current || (() => true);
  const check = () => { if (!current()) throw Object.assign(new Error('下载已暂停，请重新打开结果'), { cancelled: true }); };
  if (!api.isLive()) return Promise.reject(new Error('连接真实服务后才能下载结果'));
  return api.downloadAdvisorTask(id).then((meta) => {
    check();
    if (!Number.isInteger(meta.size) || meta.size <= 0 || meta.size > MAX_RESULT_BYTES || meta.partSize !== PART_BYTES || meta.parts !== Math.ceil(meta.size / PART_BYTES)) throw new Error('结果文件信息无效，请重新生成');
    const whole = new Uint8Array(meta.size);
    const next = (index) => {
      check();
      if (index >= meta.parts) return whole.buffer;
      return api.advisorTaskPart(id, index).then((buffer) => {
        check();
        if (Object.prototype.toString.call(buffer) !== '[object ArrayBuffer]' || buffer.byteLength !== Math.min(PART_BYTES, meta.size - index * PART_BYTES)) throw new Error('文件没有取完整，请重试');
        whole.set(new Uint8Array(buffer), index * PART_BYTES);
        return next(index + 1);
      });
    };
    return next(0);
  }).then((buffer) => {
    check();
    return exporter.save(buffer, 'xlsx').then((filePath) => {
      if (!current()) {
        if (!exporter.remove(filePath)) throw new Error('下载已暂停，但临时文件没能删除，请到文件页清空导出文件');
        check();
      }
      const task = options.task || {};
      const record = { id: `advisor-${id}`, source: 'advisor', taskId: id, kind: task.kind,
        label: DATA_KINDS[task.kind] || '工具任务结果', count: task.rowCount || 0,
        format: 'xlsx', size: buffer.byteLength, at: new Date().toISOString(), path: filePath };
      if (!log.add(record, record.id)) { exporter.remove(filePath); throw new Error('导出记录没能写入，请重试'); }
      exporter.clearOld(wx.getFileSystemManager(), filePath);
      return exporter.open(filePath, 'xlsx');
    });
  });
}
module.exports = { pasteRows, csvRows, toolConsent, quotaView, openResult, MAX_RESULT_BYTES, PART_BYTES };
