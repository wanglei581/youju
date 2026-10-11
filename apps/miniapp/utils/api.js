// 数据接口：正式版走微信云托管（callContainer），开发时可连本机后端，都没配置时用演示数据。
// 接口返回格式与 mock/data.js 一致，页面不用区分数据来自哪里。
const config = require('./config.js');
const mock = require('../mock/data.js');
const { mockSearch } = require('./mockSearch.js');
const advisorMock = require('../mock/advisor.js');
const { daysUntil } = require('./format.js');

const TYPES = ['高新技术企业', '专精特新', '科技型中小企业', '其他'];

function copy(value) {
  return Promise.resolve(JSON.parse(JSON.stringify(value)));
}

function isLive() {
  return Boolean(config.cloudEnv || config.devBaseUrl);
}

function query(params) {
  const parts = Object.keys(params || {})
    .filter((k) => params[k] !== undefined && params[k] !== null && params[k] !== '')
    .map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(params[k])}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

function errorOf(res) {
  const detail = res && res.data && res.data.detail;
  const validation = res && res.statusCode === 422 ? '输入内容或筛选条件不符合要求，请检查后重试' : '';
  const err = new Error(typeof detail === 'string' ? detail : ((detail && detail.message) || validation || `服务出错了（${res ? res.statusCode : '网络'}）`));
  err.status = res ? res.statusCode : 0;
  err.detail = detail;
  if (detail && typeof detail === 'object' && typeof detail.code === 'string') err.code = detail.code;
  return err;
}

// 本机记下的已同意协议版本（服务端才是准的；服务端返回 403 时清掉，下次操作重新请求同意）
const AGREED_KEY = 'youjuAgreedVersion';

function forgetAgreed() {
  try { wx.removeStorageSync(AGREED_KEY); } catch (e) { /* 忽略 */ }
}

// 冷启动网关错误或单次 5 秒内的调用失败：GET 最多再试 4 次，等待共 17.5 秒。
// 单次 15 秒未返回（包括平台报超时）不重试；POST 等写操作不重试。
const RETRY_DELAYS = [1500, 3000, 5000, 8000];
const TIMEOUT = 15000;
const GATEWAY_CODES = [502, 503, 504];
const startupPages = new Map();
let startingRequests = 0;

function emitStartup() {
  startupPages.forEach((page) => page.setData({ serviceStarting: startingRequests > 0 }));
}
function watchStartup(page) {
  startupPages.set(page, page);
  page.setData({ serviceStarting: startingRequests > 0 });
}
function stopStartup(page) {
  startupPages.delete(page);
}

function withRetry(attempt, method) {
  let starting = false;
  const run = (n) => {
    const sent = Date.now();
    return attempt().catch((err) => {
      if (method !== 'GET' || !err || !err.retryable || err.timeout || n >= RETRY_DELAYS.length) throw err;
      if (!GATEWAY_CODES.includes(err.status) && Date.now() - sent > 5000) throw err;
      if (!starting) { starting = true; startingRequests += 1; emitStartup(); }
      return new Promise((resolve) => setTimeout(resolve, RETRY_DELAYS[n])).then(() => run(n + 1));
    });
  };
  const finish = () => { if (starting) { startingRequests -= 1; emitStartup(); } };
  return run(0).then((value) => { finish(); return value; }, (err) => { finish(); throw err; });
}

// 15 秒没有返回：算超时，不再重试
function timeoutError() {
  const err = new Error('网络不太好，请稍后再试');
  err.timeout = true;
  return err;
}

// 调用失败（没有拿到响应）：很快失败的可以重试；云托管自己报超时的不重试
function callFailure(res) {
  const err = new Error('网络不太好，请稍后再试');
  err.retryable = true;
  if (/timeout|超时/i.test(String((res && res.errMsg) || ''))) err.timeout = true;
  return err;
}

function request(path, params, options) {
  const url = path + query(params);
  const method = (options && options.method) || 'GET';
  const data = options && options.data;
  const header = Object.assign(method === 'GET' ? {} : { 'content-type': 'application/json' }, options && options.header);
  return withRetry(() => requestOnce(url, method, data, header), method);
}

function requestOnce(url, method, data, header) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => { if (!settled) { settled = true; clearTimeout(timer); fn(value); } };
    // 15 秒没有返回就算超时
    const timer = setTimeout(() => finish(reject, timeoutError()), TIMEOUT);
    const ok = (code) => code === 200 || code === 201 || code === 202 || code === 204;
    const done = (res) => {
      if (ok(res.statusCode)) { finish(resolve, res.data); return; }
      // 403：服务端没有同意记录（如在别的设备注销过）或协议已更新
      if (res.statusCode === 403) forgetAgreed();
      const err = errorOf(res);
      if (GATEWAY_CODES.includes(res.statusCode)) err.retryable = true;
      finish(reject, err);
    };
    if (config.cloudEnv) {
      wx.cloud.callContainer({
        config: { env: config.cloudEnv },
        path: url,
        method,
        header: Object.assign({ 'X-WX-SERVICE': config.cloudService }, header),
        data,
        timeout: TIMEOUT,
        success: done,
        fail: (res) => finish(reject, callFailure(res)),
      });
    } else {
      wx.request({
        url: config.devBaseUrl + url,
        method,
        header,
        data,
        timeout: TIMEOUT,
        success: done,
        fail: () => finish(reject, new Error('连不上开发用的后端，请确认 youju serve 已启动')),
      });
    }
  });
}

// 收录范围：已收录的地区（栏目数、最近更新时间）和还没收录的省份（验收 A8）
function coverage() {
  if (isLive()) return request('/api/v1/coverage');
  return copy(mock.coverage);
}

// group=1（D-055）：同一份部里通知的各地转发版本按组折叠、按组计数（管理台确认过的组才折叠）
function home() {
  if (isLive()) return request('/api/v1/home', { group: 1 });
  return copy({ stats: mock.stats, items: mock.home });
}

function search(params) {
  if (isLive()) return request('/api/v1/search', Object.assign({}, params, { group: 1 }));
  return copy(mockSearch(mock.search.items, params));
}

function deadlineReport(params) {
  if (isLive()) return request('/api/v1/reports/deadlines', params);
  const days = Number((params && params.days) || 7);
  const all = ((mock.reports && mock.reports.deadlines) || [])
    .map((item) => Object.assign({}, item, { remaining_days: daysUntil(item.date, undefined, item.time) }))
    .filter((item) => item.remaining_days >= 0);
  const deptOf = (value) => (/科技/.test(value) ? '科技部门' : (/工业和信息化|经济和信息化|工信|经信/.test(value) ? '工信部门' : '其他部门'));
  const scoped = all
    .filter((item) => !params.province || item.province === params.province)
    .filter((item) => !params.dept || deptOf(item.dept) === params.dept)
    .filter((item) => !params.support || (item.support || []).includes(params.support));
  const filtered = scoped.filter((item) => item.remaining_days <= days);
  const summary = { 7: scoped.filter((x) => x.remaining_days <= 7).length, 14: scoped.filter((x) => x.remaining_days <= 14).length, 30: scoped.filter((x) => x.remaining_days <= 30).length };
  const by = (key) => filtered.reduce((out, item) => { out[item[key]] = (out[item[key]] || 0) + 1; return out; }, {});
  const page = Number(params.page || 1); const size = Number(params.size || 20);
  return copy({ days, summary, verified_total: ((mock.reports && mock.reports.deadlines) || []).length, by_province: by('province'), by_dept: filtered.reduce((out, item) => { const key = deptOf(item.dept); out[key] = (out[key] || 0) + 1; return out; }, {}), total: filtered.length, page, size, items: filtered.slice((page - 1) * size, page * size) });
}

function listBoard(params) {
  if (isLive()) return request('/api/v1/reports/lists', params);
  const demo = mock.listBoard || {
    recent: { total: 0, items: [] },
    stats: { by_year: [], by_province: [], by_type: [] },
  };
  const page = Number((params && params.page) || 1);
  const size = Number((params && params.size) || 20);
  const allRecords = demo.recent.items || [];
  const year = params && params.year ? String(params.year) : '';
  const province = (params && params.province) || '';
  const type = (params && params.type) || '';
  const records = allRecords.filter((item) => (!year || String(item.year) === year)
    && (!province || item.province === province)
    && (!type || item.type === type));
  const statRows = (field) => {
    const grouped = {};
    records.forEach((item) => {
      const key = item[field];
      if (key === null || key === undefined) return;
      if (!grouped[key]) grouped[key] = { [field]: key, lists: 0, entries: 0 };
      grouped[key].lists += 1;
      if (item.count !== null && item.count !== undefined) grouped[key].entries += item.count;
    });
    return Object.values(grouped);
  };
  const stats = {
    by_year: statRows('year').sort((a, b) => b.year - a.year),
    by_province: statRows('province').sort((a, b) => a.province.localeCompare(b.province)),
    by_type: statRows('type').sort((a, b) => TYPES.indexOf(a.type) - TYPES.indexOf(b.type)),
  };
  return copy({
    days: Number((params && params.days) || 30),
    year: year ? Number(year) : null,
    province,
    type,
    recent: { total: records.length, page, size, items: records.slice((page - 1) * size, page * size) },
    stats,
  });
}

// 演示数据只有辽宁这一份完整详情；其他文件用列表里的信息拼出基本详情
function mockDetail(id) {
  if (mock.detail[id]) return copy(mock.detail[id]);
  const pools = [mock.home, mock.search.items, mock.lists.items, mock.follow.changes];
  for (const pool of pools) {
    const doc = pool.find((d) => d.id === id);
    if (doc) {
      return copy({
        id: doc.id, kind: doc.kind, tags: [doc.kind], category: doc.category || null, region: doc.region, title: doc.title, issuer: doc.issuer,
        date: doc.date, url: doc.url, status: doc.status || null, app_status: doc.app_status || null,
        deadline: null, points: [['发文机关', doc.issuer]], excerpt: null, attachments: [], checked: null,
      });
    }
  }
  return Promise.reject(new Error('没有找到这份文件'));
}

function detail(id) {
  if (isLive()) return request(`/api/v1/documents/${encodeURIComponent(id)}`);
  return mockDetail(id);
}

// 日历每条的 id 是截止记录编号，打开详情用 docId（演示数据里两者相同）
// 日历要按全部事项计算"全部"份数：按 total 翻页取完（最多 1000 条）
function calendar() {
  if (isLive()) {
    const page = (offset, acc, closed) => request('/api/v1/calendar', { limit: 200, offset }).then((r) => {
      const items = acc.concat(r.items).slice(0, 1000);
      const past = closed.concat(r.past || []).slice(0, 1000);
      const next = offset + 200;
      return next < Math.max(r.total, r.pastTotal || 0) && next < 1000 && (r.items.length || (r.past || []).length)
        ? page(next, items, past)
        : { items, past, total: r.total, pastTotal: r.pastTotal || 0, verifiedTotal: r.verifiedTotal, truncated: r.total > items.length || (r.pastTotal || 0) > past.length };
    });
    return page(0, [], []);
  }
  return copy({ items: mock.calendar.map((d) => Object.assign({ docId: d.id }, d)), verifiedTotal: mock.calendar.filter((d) => d.verified).length });
}

function lists() {
  if (isLive()) return request('/api/v1/lists/recent', { limit: 20, group: 1 }).then((r) => ({ week_total: r.week_total, total: r.total, items: r.items }));
  return copy(Object.assign({ total: mock.lists.week_total }, mock.lists));
}

// 查名单：按企业全称或统一社会信用代码与名单原文比对；名称相近的另列（similar），不当作同一家。
// 演示数据只对虚构的"演示科技有限公司"给出结果，页面会标明"演示数据"
const DEMO_COMPANY = '演示科技有限公司';
function lookup(q) {
  if (isLive()) return request('/api/v1/lists/lookup', { q });
  const hit = q.replace(/\s+/g, '') === DEMO_COMPANY;
  const items = hit
    ? mock.lists.items.slice(0, 2).map((d, i) => Object.assign({}, d, {
      matched: { name: DEMO_COMPANY, category: '科技型中小企业', batch: `第${i + 4}批`, sequence: String(12 + i), creditCode: null, region: null, via: 'name' },
    }))
    : [];
  return copy({ query: q, items, similar: [], ambiguous: false, truncated: false, coverage: { lists: mock.lists.week_total, since: '2026-07-01' } });
}

// 导出 Excel / Word：返回文件内容（ArrayBuffer）。文件由服务端生成，小于 1000 KiB（云托管返回上限）
const EXPORT_ERRORS = {
  413: '文件太大，请少选几份再导出',
  422: '有的文件已不存在或超过 200 份，请刷新后再试',
};
function exportFile(ids, format) {
  const body = { ids: ids.map(String), format };
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => { if (!settled) { settled = true; clearTimeout(timer); fn(value); } };
    const timer = setTimeout(() => finish(reject, new Error('网络不太好，请稍后再试')), 30000);
    const done = (res) => (res.statusCode === 200
      ? finish(resolve, res.data)
      : finish(reject, new Error(EXPORT_ERRORS[res.statusCode] || `导出失败（${res.statusCode}）`)));
    const fail = () => finish(reject, new Error('网络不太好，请稍后再试'));
    if (config.cloudEnv) {
      wx.cloud.callContainer({
        config: { env: config.cloudEnv },
        path: '/api/v1/exports',
        method: 'POST',
        header: { 'X-WX-SERVICE': config.cloudService, 'content-type': 'application/json' },
        data: body,
        // 二进制内容：dataType 要设成 other，否则 SDK 会把内容当文字解析而报错（同附件下载）
        dataType: 'other',
        responseType: 'arraybuffer',
        timeout: 30000,
        success: done,
        fail,
      });
    } else {
      wx.request({
        url: `${config.devBaseUrl}/api/v1/exports`,
        method: 'POST',
        header: { 'content-type': 'application/json' },
        data: body,
        dataType: 'other',
        responseType: 'arraybuffer',
        timeout: 30000,
        success: done,
        fail,
      });
    }
  });
}

// 附件副本的一段（ArrayBuffer）：服务端存有副本的附件按段取（每段不超过云托管返回上限 1000 KiB），
// version 是详情接口给的版本，对不上（副本已更新）时服务端返回 409
function attachmentPart(id, version, part) {
  return withRetry(() => attachmentPartOnce(id, version, part), 'GET');
}

function attachmentPartOnce(id, version, part) {
  return binaryOnce(`/api/v1/attachments/${encodeURIComponent(id)}/content${query({ v: version, part })}`, '附件下载失败');
}

// 批量工具结果文件的一段（原始字节）。和附件一样经我们自己的接口分段取：
// 不让手机直接去对象存储下载（那要在小程序后台配 downloadFile 合法域名，10/10 真机试用前发现结果因此打不开）。
function advisorTaskPart(id, part) {
  const path = `/api/v1/advisor/tasks/${encodeURIComponent(id)}/file${query({ part })}`;
  return withRetry(() => binaryOnce(path, '结果文件下载失败'), 'GET');
}

// 取一段二进制内容（附件、工具结果共用）
function binaryOnce(path, failText) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => { if (!settled) { settled = true; clearTimeout(timer); fn(value); } };
    const timer = setTimeout(() => finish(reject, timeoutError()), TIMEOUT);
    const done = (res) => {
      if (res.statusCode === 200) { finish(resolve, res.data); return; }
      const err = new Error(`${failText}（${res.statusCode}）`);
      err.status = res.statusCode;
      if (GATEWAY_CODES.includes(res.statusCode)) err.retryable = true;
      finish(reject, err);
    };
    const fail = (res) => finish(reject, callFailure(res));
    if (config.cloudEnv) {
      wx.cloud.callContainer({
        config: { env: config.cloudEnv },
        path,
        method: 'GET',
        header: { 'X-WX-SERVICE': config.cloudService },
        // 二进制内容：dataType 要设成 other，否则 SDK 会把内容当文字解析而报错（10/7 开发者工具实测）
        dataType: 'other',
        responseType: 'arraybuffer',
        timeout: TIMEOUT,
        success: done,
        fail,
      });
    } else {
      wx.request({ url: config.devBaseUrl + path, method: 'GET', dataType: 'other', responseType: 'arraybuffer', timeout: TIMEOUT, success: done, fail });
    }
  });
}

// 纠错：只提交文字（最多 500 字）和文件编号，不收联系方式；服务端先做内容安全校验
function feedback(docId, content) {
  if (!isLive()) return Promise.reject(new Error('演示数据不能提交，连上接口后可用'));
  return request('/api/v1/feedback', null, { method: 'POST', data: { docId: docId || null, content } });
}

// ---- 用户（#30）：同意用户协议和隐私保护指引后，服务端才保存用户和其关注、订阅、纠错 ----

function me() {
  if (!isLive()) return advisorMock.me();
  return request('/api/v1/me');
}

function agree(version, scope) {
  if (!isLive() && scope === 'advisor') return advisorMock.agree(version);
  const invite = require('./invite.js');
  const data = scope ? { version, scope } : { version };
  const general = !scope || scope === 'general';
  const code = general && invite.read();
  if (code) data.invite_code = code;
  return request('/api/v1/me/agreement', null, { method: 'POST', data }).then((res) => {
    // 发出请求后可能又打开另一张卡片，只清除这次成功注册所用的码。
    if (general && invite.read() === code) invite.clear();
    return res;
  });
}

function myRewards() { return request('/api/v1/me/rewards'); }
function inviteInfo() { return request('/api/v1/me/invite'); }
function submitDeadlineClue(data) { return request('/api/v1/me/deadline-clues', null, { method: 'POST', data }); }
function myDeadlineClues() { return request('/api/v1/me/deadline-clues'); }
function deleteDeadlineClue(id) { return request(`/api/v1/me/deadline-clues/${encodeURIComponent(id)}`, null, { method: 'DELETE' }); }

// 注销：删除服务端的用户和关注、订阅、纠错、埋点
function deleteMe() {
  if (!isLive()) return advisorMock.clear().then(forgetAgreed);
  return request('/api/v1/me', null, { method: 'DELETE' }).then(forgetAgreed);
}

function myFollows() {
  return request('/api/v1/me/follows');
}

function createFollow(name, conditions) {
  return request('/api/v1/me/follows', null, { method: 'POST', data: { name, conditions } });
}

function deleteFollow(id) {
  return request(`/api/v1/me/follows/${id}`, null, { method: 'DELETE' });
}

function seenFollow(id) {
  return request(`/api/v1/me/follows/${id}/seen`, null, { method: 'POST', data: {} });
}

// 我的数据：我们保存的本人全部个人信息（查阅、复制）
function myData() {
  if (!isLive()) return advisorMock.myData();
  return request('/api/v1/me/data');
}

const AI_DISABLED = { enabled: false, features: [], remaining: {}, disclosure: null };
let capabilitiesPromise;
let capabilitiesPending;
function advisorCapabilities(options = {}) {
  // 本次启动内共用一个请求；开启的结果一直缓存，关闭或失败的结果只缓存 15 秒；AI 页面进入时 refresh 重新取额度。
  if (capabilitiesPending) return capabilitiesPending;
  if (capabilitiesPromise && !options.refresh) return capabilitiesPromise;
  const source = isLive() ? request('/api/v1/advisor/capabilities') : Promise.resolve({});
  const result = source.then((res) => {
    const ai = (res && res.ai_advisor) || {};
    if (ai.enabled !== true) return { ai_advisor: Object.assign({}, AI_DISABLED) };
    return { ai_advisor: {
      enabled: true,
      features: Array.isArray(ai.features) ? ai.features : [],
      remaining: ai.remaining || {},
      disclosure: ai.disclosure == null ? null : ai.disclosure,
    } };
  }).catch(() => ({ ai_advisor: Object.assign({}, AI_DISABLED) }));
  capabilitiesPromise = result;
  capabilitiesPending = result;
  result.then((res) => {
    if (capabilitiesPending === result) capabilitiesPending = null;
    // “关闭”的结果只缓存 15 秒：登录前查到的关闭（403）不能一直记着，登录或加进白名单后回到页面要能重新查到
    if (!(res && res.ai_advisor && res.ai_advisor.enabled === true)) {
      setTimeout(() => { if (capabilitiesPromise === result) capabilitiesPromise = null; }, 15000);
    }
  });
  return result;
}

function advisorStatus(options = {}) {
  // 悬浮入口在本次启动内共用一个请求，失败也缓存；工具页主动刷新额度。
  const cache = typeof getApp === 'function' ? getApp().globalData : null;
  if (cache && cache.advisorStatusPending) return cache.advisorStatusPending;
  if (cache && cache.advisorStatusPromise && !options.refresh) return cache.advisorStatusPromise;
  const result = isLive() ? request('/api/v1/advisor/status') : advisorMock.status();
  if (cache) {
    cache.advisorStatusPromise = result;
    cache.advisorStatusPending = result;
    const settled = () => { if (cache.advisorStatusPending === result) cache.advisorStatusPending = null; };
    result.then(settled, settled);
  }
  return result;
}

function advisorPreview(data) {
  return isLive() ? request('/api/v1/advisor/preview', null, { method: 'POST', data }) : advisorMock.preview(data);
}

function createAdvisorTask(data, key) {
  if (data.kind !== 'interpret' && data.kind !== 'clause') {
    return isLive() ? request('/api/v1/advisor/tasks', null, { method: 'POST', data, header: { 'Idempotency-Key': key } }) : advisorMock.create(data, key);
  }
  if (!isLive()) {
    return Promise.reject(Object.assign(new Error('暂时没有可用工具'), { status: 404 }));
  }
  const body = data.kind === 'clause'
    ? { kind: 'clause', doc_id: Number(data.doc_id), company: data.company || {} }
    : { kind: 'interpret', doc_id: Number(data.doc_id) };
  return request('/api/v1/advisor/tasks', null, { method: 'POST', data: body, header: { 'Idempotency-Key': key } }).catch((err) => {
    if (err.status === 422 && err.detail != null && typeof err.detail !== 'string') {
      err.message = Array.isArray(err.detail)
        ? err.detail.map((item) => (item && item.msg) || JSON.stringify(item)).join('\n') : JSON.stringify(err.detail);
    }
    throw err;
  });
}

function advisorTasks() {
  return isLive() ? request('/api/v1/advisor/tasks') : advisorMock.list();
}

function advisorTask(id) {
  return isLive() ? request(`/api/v1/advisor/tasks/${encodeURIComponent(id)}`) : advisorMock.get(id);
}

function cancelAdvisorTask(id) {
  return isLive() ? request(`/api/v1/advisor/tasks/${encodeURIComponent(id)}/cancel`, null, { method: 'POST', data: {} }) : advisorMock.cancel(id);
}

function deleteAdvisorTask(id) {
  return isLive() ? request(`/api/v1/advisor/tasks/${encodeURIComponent(id)}`, null, { method: 'DELETE' }) : advisorMock.remove(id);
}

function downloadAdvisorTask(id) {
  return isLive() ? request(`/api/v1/advisor/tasks/${id}/download`) : advisorMock.download(id);
}

// 客户工作台 30 天（默认关闭）：接口 404 或空列表时页面不显示付费内容；演示数据不出售。
function payProducts() {
  return isLive() ? request('/api/v1/pay/products') : Promise.resolve({ items: [] });
}

// data：{ product_id, code }，code 来自 wx.login，只用于服务端签名，不保存。
function createPayOrder(data) {
  if (!isLive()) return Promise.reject(new Error('演示数据不能支付'));
  return request('/api/v1/pay/orders', null, { method: 'POST', data });
}

// 订单状态和使用期以服务端为准。
function payOrder(id) {
  if (!isLive()) return Promise.reject(new Error('演示数据不能支付'));
  return request(`/api/v1/pay/orders/${encodeURIComponent(id)}`);
}

function payOrders() {
  return isLive() ? request('/api/v1/pay/orders') : Promise.resolve({ items: [] });
}

function refundPayOrder(id) {
  if (!isLive()) return Promise.reject(new Error('演示数据不能支付'));
  return request(`/api/v1/pay/orders/${encodeURIComponent(id)}/refund`, null, { method: 'POST' });
}

// 改关注名称（小程序的 wx.request 不支持 PATCH，用 PUT）
function renameFollow(id, name) {
  return request(`/api/v1/me/follows/${id}`, null, { method: 'PUT', data: { name } });
}

function deleteFeedback(id) {
  return request(`/api/v1/me/feedback/${id}`, null, { method: 'DELETE' });
}

// 取消还没发出的提醒授权
function deleteSubscription(id) {
  return request(`/api/v1/me/subscriptions/${id}`, null, { method: 'DELETE' });
}

// 未用的订阅授权（还没发出的提醒）
function mySubscriptions() {
  return request('/api/v1/me/subscriptions');
}

function followNew(id, params) {
  return request(`/api/v1/me/follows/${id}/new`, params);
}

// 关注页的演示数据（没连接口时）
function follow() {
  return copy(mock.follow);
}

function files() {
  return copy(mock.files);
}

// 保存一次性订阅授权：newDocs 对应某个关注，deadline 对应某份文件的截止
function saveSubscription(kind, target) {
  if (!isLive()) return Promise.reject(new Error('演示数据不能订阅'));
  const data = { kind, templateId: config.templates[kind] };
  if (kind === 'newDocs') data.followId = target.followId;
  else data.docId = Number(target.docId);
  return request('/api/v1/me/subscriptions', null, { method: 'POST', data });
}

module.exports = {
  watchStartup, stopStartup, isLive, request, coverage, home, search, deadlineReport, listBoard, detail, calendar, lists, lookup, exportFile, feedback,
  attachmentPart,
  AGREED_KEY, forgetAgreed, me, agree, deleteMe, myFollows, createFollow, deleteFollow, seenFollow, followNew, mySubscriptions,
  myData, renameFollow, deleteFeedback, deleteSubscription,
  myRewards, inviteInfo, submitDeadlineClue, myDeadlineClues, deleteDeadlineClue,
  follow, files, saveSubscription,
  advisorCapabilities, advisorStatus, advisorPreview, createAdvisorTask, advisorTasks, advisorTask, cancelAdvisorTask, deleteAdvisorTask, downloadAdvisorTask,
  payProducts, createPayOrder, payOrder, payOrders, refundPayOrder, advisorTaskPart,
};
