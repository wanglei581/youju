// 数据工具只走真实后端；不在本机解析 XLSX 或保存客户输入。
const api = require('./api.js');
const regions = require('./regions.js');
const terms = require('../content/advisor-terms.js');
const { DATA_KINDS } = require('./advisor.js');
const boost = require('./boost.js');

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

// 列顺序：客户名称,省份,关键词,支持方式,城市,区县（旧四列、五列位置不变）。城市、区县规则与服务端 track.CustomerProfile 一致：
// 各只填一个、不超过 32 字、不含 / \ , ， ; ； 、 | 和制表换行；国家部委不带城市和区县。直辖市城市填本市：没填区县按省级，
// 填了区县时城市记本市简称（北京,北京,朝阳区）；区县必须同时填所属城市，区县名照录，不去掉"区""县"。
// 关键词、支持方式仍可用分号填多项。空行参与编号，第 N 行与输入框所见一致；报错只写第几行和原因，不回显客户内容。
// 没填区县的行不带 district，提交内容（和安全重试摘要）与加区县之前完全相同。
function pasteRows(kind, text) {
  if (kind === 'compare') return text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  const result = [];
  csvRows(text, true).forEach((row, index) => {
    if (!row.some(Boolean)) return;
    const line = `第 ${index + 1} 行`;
    if (row.length > 6) throw new Error(`${line}最多六列：客户名称,省份,关键词,支持方式,城市,区县；城市、区县各只填一个`);
    if (!row[0] || !row[1]) throw new Error(`${line}请填写客户名称和省份`);
    const province = regions.provinceName(row[1]) || row[1];
    const district = row[5] || '';
    let city = regions.cityName(row[4] || '');
    if (regions.MUNICIPALITIES.includes(province) && city === province && !district) city = '';
    if ((city || district) && ['国家', '国家部委'].includes(province)) throw new Error(`${line}省份为国家部委时城市请留空，区县也请留空`);
    if (city.length > 32 || regions.SEPARATORS.test(city)) throw new Error(`${line}城市只填一个，不超过 32 字，不用逗号、分号、顿号、竖线或斜杠`);
    if (district && !city) throw new Error(`${line}填写区县时请同时填写所属城市；直辖市的城市填本市，如北京`);
    if (district && regions.MUNICIPALITIES.includes(province) && city !== province) throw new Error(`${line}直辖市填写区县时，城市填本市，如${province}`);
    if (district.length > 32 || regions.SEPARATORS.test(district)) throw new Error(`${line}区县只填一个，不超过 32 字，不用逗号、分号、顿号、竖线或斜杠`);
    result.push(Object.assign({ name: row[0], province, keywords: row[2] || '', supports: row[3] || '', city }, district ? { district } : {}));
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

// 怎么算次数（D-074：顾问算不清扣几次）：免费和加量体验都按"交一份名单算一次"，系统出错没出结果的不算（会退回）
const COUNT_RULE = '交一份名单算一次，系统出错、没出结果的不算';
// 额度一行和加量体验、申报季加量的状态（D-070）；体验期间不占每月免费次数，remaining 仍是本月免费剩余
function quotaView(status, kind, now) {
  const quota = status && status.quota && status.quota[kind];
  if (!status || !status.enabled || !quota || !Number.isFinite(quota.limit) || !Number.isFinite(quota.used) || !Number.isFinite(quota.maxRows)) throw new Error('未能读取工具额度，请重试');
  const active = !!(status.pass && status.pass.active);
  const remaining = Math.max(0, quota.limit - quota.used);
  const trial = boost.trialView(status.trial, now);
  const statusLine = active ? `批量工具使用期内，每次最多 ${quota.maxRows} 家，不限提交次数`
    : trial.trialActive ? boost.trialLine(trial)
      : `每月免费 ${quota.limit} 次，本月剩余 ${remaining} 次；每次最多 ${quota.maxRows} 家`;
  return { maxRows: quota.maxRows, remaining, passActive: active, statusLine, countRule: active ? '' : COUNT_RULE, seasonLine: boost.seasonLine(status.season), ...trial,
    trialPitch: trial.trialAvailable ? boost.trialPitch(trial) : '', trialCap: trial.trialActive ? boost.trialCapText(trial, now) : '',
    trialEnded: active ? '' : boost.trialEndedText(status.trial, now), quotaDay: boost.beijingDay(now) };
}

// 页面一直开着（电脑版微信常这样）跨过北京时间 0 点、或过了体验结束时间：页面上的额度可能已经变了，
// 挡住提交之前先重新读一次（体验每天 10 次按北京时间重新计数，到期回到免费量）
function quotaStale(view, now) {
  const at = now || new Date();
  return view.quotaDay !== boost.beijingDay(at) || (view.trialActive === true && view.trialEndsMs > 0 && at.getTime() >= view.trialEndsMs);
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
module.exports = { pasteRows, csvRows, toolConsent, quotaView, quotaStale, openResult, MAX_RESULT_BYTES, PART_BYTES };
