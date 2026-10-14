// 数据接口：正式版走微信云托管（callContainer），开发时可连本机后端，都没配置时用演示数据。
// 接口返回格式与 mock/data.js 一致，页面不用区分数据来自哪里。
const config = require('./config.js');
const mock = require('../mock/data.js');
const { mockSearch } = require('./mockSearch.js');
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
  const err = new Error(typeof detail === 'string' ? detail : (validation || `服务出错了（${res ? res.statusCode : '网络'}）`));
  err.status = res ? res.statusCode : 0;
  return err;
}

// 本机记下的已同意协议版本（服务端才是准的；服务端返回 403 时清掉，下次操作重新请求同意）
const AGREED_KEY = 'youjuAgreedVersion';

function forgetAgreed() {
  try { wx.removeStorageSync(AGREED_KEY); } catch (e) { /* 忽略 */ }
}

// 冷启动（10/12 诊断）：接口服务空闲时缩到 0 个实例，下次请求要等新实例起来——10/12 实测空闲 25 分钟后
// 67 秒才第一次成功。期间网关可能回 502/503/504、调用很快失败，也可能一直不回到 15 秒超时。读数据（GET）
// 遇到这三种情况都接着重试，间隔 1.5、3、5、8 秒，之后每 10 秒一次，从第一次发出算起超过 90 秒就不再发新的请求；
// 写操作（POST 等）不重试，404、422、500 等也不重试。
const RETRY_DELAYS = [1500, 3000, 5000, 8000, 10000];
const MAX_WAIT = 90000;
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

// token.cancelled 为真时不再发新的重试（admin 先回了，接口服务这边就停，"正在连接"提示也跟着消失）
function withRetry(attempt, method, token) {
  let starting = false;
  const first = Date.now();
  const stopped = () => Boolean(token && token.cancelled);
  const run = (n) => attempt().catch((err) => {
    if (method !== 'GET' || !err || !err.retryable || stopped()) throw err;
    const delay = RETRY_DELAYS[Math.min(n, RETRY_DELAYS.length - 1)];
    if (Date.now() + delay - first >= MAX_WAIT) throw err;
    if (!starting) { starting = true; startingRequests += 1; emitStartup(); }
    return new Promise((resolve) => setTimeout(resolve, delay)).then(() => {
      if (stopped()) throw err;
      return run(n + 1);
    });
  });
  const finish = () => { if (starting) { startingRequests -= 1; emitStartup(); } };
  return run(0).then((value) => { finish(); return value; }, (err) => { finish(); throw err; });
}

// 15 秒没有返回：算超时（读数据的请求在 90 秒内会再试）
function timeoutError() {
  const err = new Error('网络不太好，请稍后再试');
  err.timeout = true;
  err.retryable = true;
  return err;
}

// 调用失败（没有拿到响应，包括云托管自己报超时）：读数据的请求在 90 秒内会再试
function callFailure(res) {
  const err = new Error('网络不太好，请稍后再试');
  err.retryable = true;
  if (/timeout|超时/i.test(String((res && res.errMsg) || ''))) err.timeout = true;
  return err;
}

// 冷启动（10/14 实测空闲后 1–5 分钟才恢复）：不需要身份的只读接口先走常驻的 admin 服务（后端 ADMIN_READ_PATHS，
// 首屏快照 10/12 起就这样）。admin 拦下（404 "Not Found"）、出错（5xx）、调用失败时马上改走接口服务；
// admin 8 秒没回，就同时去问接口服务，两边谁先回用谁（admin 最多等 30 秒），一边回了另一边就不再重试。
// 接口服务冷启动按上面的规则重试；接口自己的答复（如"该文件已下架"、422、429）直接报。
const ADMIN_READS = [
  /^\/api\/v1\/(home|coverage|search|calendar|snapshot)(\?|$)/,
  /^\/api\/v1\/reports\/(deadlines|lists)(\?|$)/,
  /^\/api\/v1\/lists\/(recent|lookup)(\?|$)/,
  /^\/api\/v1\/documents\/\d+(\?|$)/,
];
const ADMIN_SERVICE = 'admin';
const ADMIN_FIRST_WAIT = 8000;
const ADMIN_TIMEOUT = 30000;

function adminRead(url, method) {
  return method === 'GET' && Boolean(config.cloudEnv) && ADMIN_READS.some((re) => re.test(url));
}

function tryApiInstead(err) {
  if (!err || !err.status || err.retryable) return true;
  return err.status >= 500 || (err.status === 404 && err.message === 'Not Found');
}

function request(path, params, options) {
  const url = path + query(params);
  const method = (options && options.method) || 'GET';
  const data = options && options.data;
  const header = method === 'GET' ? {} : { 'content-type': 'application/json' };
  if (!adminRead(url, method)) return withRetry(() => requestOnce(url, method, data, header), method);
  return new Promise((resolve, reject) => {
    const token = { cancelled: false };
    let settled = false;
    let apiStarted = false;
    let timer = null;
    const settle = (fn, value) => {
      if (settled) return;
      settled = true; token.cancelled = true; clearTimeout(timer); fn(value);
    };
    const startApi = () => {
      if (apiStarted) return;
      apiStarted = true;
      withRetry(() => requestOnce(url, method, data, header), method, token)
        .then((value) => settle(resolve, value), (err) => settle(reject, err));
    };
    timer = setTimeout(startApi, ADMIN_FIRST_WAIT);
    requestOnce(url, method, data, header, ADMIN_SERVICE, ADMIN_TIMEOUT).then(
      (value) => settle(resolve, value),
      (err) => {
        if (!tryApiInstead(err)) { settle(reject, err); return; }
        clearTimeout(timer);
        startApi();
      },
    );
  });
}

function requestOnce(url, method, data, header, service, timeout = TIMEOUT) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => { if (!settled) { settled = true; clearTimeout(timer); fn(value); } };
    // 15 秒（走 admin 的只读请求 30 秒，8 秒没回会同时问接口服务）没有返回就算超时
    const timer = setTimeout(() => finish(reject, timeoutError()), timeout);
    const ok = (code) => code === 200 || code === 201 || code === 204;
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
        header: Object.assign({ 'X-WX-SERVICE': service || config.cloudService }, header),
        data,
        timeout,
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

// 首屏快照（10/12 冷启动诊断）：接口服务空闲后要 67 秒才能取到数据；admin 服务常驻、不冷启动，
// 打开时先从它取首页、截止页默认视图、查名单要用的公开数据（后端 /api/v1/snapshot），页面先显示它，
// 接口服务的实时数据回来再换掉。只试一次、不重试；取不到返回 null，页面照常等实时数据。
// 同一次打开里各页共用一份：60 秒内不重复取。
const SNAPSHOT_SERVICE = ADMIN_SERVICE;
const SNAPSHOT_REUSE = 60000;
let snapshotMemo = null;
function snapshotOnce() {
  if (snapshotMemo && Date.now() - snapshotMemo.at < SNAPSHOT_REUSE) return snapshotMemo.promise;
  let promise;
  if (config.cloudEnv) promise = requestOnce('/api/v1/snapshot', 'GET', undefined, {}, SNAPSHOT_SERVICE);
  else if (config.devBaseUrl) promise = requestOnce('/api/v1/snapshot', 'GET', undefined, {});
  else promise = Promise.resolve(null);
  promise = promise.then((snap) => (snap && snap.home ? snap : null), () => null);
  snapshotMemo = { at: Date.now(), promise };
  return promise;
}

// 收录范围：已收录的地区（栏目数、最近更新时间）和还没收录的省份（验收 A8）
function coverage() {
  if (isLive()) return request('/api/v1/coverage');
  return copy(mock.coverage);
}

// group=1（D-055）：同一份部里通知的各地转发版本按组折叠、按组计数（管理台确认过的组才折叠）
// excerpts=1（D-061）：列表带主截止 due，8 天以后到期的可以是"机器摘录"；不带这个参数时接口和以前一样
const EXCERPTS = { excerpts: 1 };
// 首页（D-061）：本周统计和"7 天内截止"（dueSoon 只有核对过的，最多 5 条）来自 /home，
// 下面的列表是最新的 8 份申报通知（和搜索同一口径，已剔除不是企业能报的杂项）。
// 首页上只显示人工核对过的截止：列表那一请求不带 excerpts，不出现机器摘录的日期
function home() {
  if (isLive()) {
    return Promise.all([
      request('/api/v1/home', Object.assign({ group: 1 }, EXCERPTS)),
      request('/api/v1/search', { kind: '申报通知', limit: 8, offset: 0, group: 1 }),
    ]).then(([h, latest]) => Object.assign({}, h, { items: latest.items || [] }));
  }
  return copy({ stats: mock.stats, items: mock.home.filter((d) => d.kind === '申报通知'), dueSoon: [], dueSoonUnverifiedTotal: 0 });
}

function search(params) {
  if (isLive()) return request('/api/v1/search', Object.assign({}, params, { group: 1 }, EXCERPTS));
  return copy(mockSearch(mock.search.items, params));
}

// 截止页的列表。opts.verifiedOnly（"只看已核对"）时不带 excerpts，结果和以前的截止雷达一样只有核对过的
function deadlineReport(params, opts) {
  const verifiedOnly = Boolean(opts && opts.verifiedOnly);
  if (isLive()) return request('/api/v1/reports/deadlines', verifiedOnly ? params : Object.assign({}, params, EXCERPTS));
  const days = Number((params && params.days) || 7);
  const all = ((mock.reports && mock.reports.deadlines) || [])
    .filter((item) => !verifiedOnly || (item.source || 'verified') === 'verified')
    .map((item) => Object.assign({}, item, { remaining_days: daysUntil(item.date, undefined, item.time) }))
    // 演示数据和接口同一口径：机器摘录只给 8 天以后到期的
    .filter((item) => item.remaining_days >= ((item.source || 'verified') === 'excerpt' ? 8 : 0));
  const deptOf = (value) => (/科技/.test(value) ? '科技部门' : (/工业和信息化|经济和信息化|工信|经信/.test(value) ? '工信部门' : '其他部门'));
  const scoped = all
    .filter((item) => !params.province || item.province === params.province)
    .filter((item) => !params.dept || deptOf(item.dept) === params.dept);
  const filtered = scoped.filter((item) => item.remaining_days <= days);
  const summary = { 7: scoped.filter((x) => x.remaining_days <= 7).length, 14: scoped.filter((x) => x.remaining_days <= 14).length, 30: scoped.filter((x) => x.remaining_days <= 30).length };
  const by = (key) => filtered.reduce((out, item) => { out[item[key]] = (out[item[key]] || 0) + 1; return out; }, {});
  const page = Number(params.page || 1); const size = Number(params.size || 20);
  const soon = (mock.reports && mock.reports.unverifiedSoon) || { total: 0, items: [] };
  return copy(Object.assign({ days, summary, verified_total: ((mock.reports && mock.reports.deadlines) || []).filter((x) => (x.source || 'verified') === 'verified').length, by_province: by('province'), by_dept: filtered.reduce((out, item) => { const key = deptOf(item.dept); out[key] = (out[key] || 0) + 1; return out; }, {}), total: filtered.length, page, size, items: filtered.slice((page - 1) * size, page * size) }, verifiedOnly ? {} : { unverifiedSoon: soon }));
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
    let soon = null;
    const page = (offset, acc, closed) => request('/api/v1/calendar', Object.assign({ limit: 200, offset }, EXCERPTS)).then((r) => {
      if (!soon) soon = r.unverifiedSoon || { total: 0, items: [] };
      const items = acc.concat(r.items).slice(0, 1000);
      const past = closed.concat(r.past || []).slice(0, 1000);
      const next = offset + 200;
      return next < Math.max(r.total, r.pastTotal || 0) && next < 1000 && (r.items.length || (r.past || []).length)
        ? page(next, items, past)
        : { items, past, total: r.total, pastTotal: r.pastTotal || 0, verifiedTotal: r.verifiedTotal, unverifiedSoon: soon, truncated: r.total > items.length || (r.pastTotal || 0) > past.length };
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
  // excerpts=1（D-061）：导出的截止列也按首发版规则写，机器摘录的另附原句
  const body = { ids: ids.map(String), format, excerpts: 1 };
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
  const path = `/api/v1/attachments/${encodeURIComponent(id)}/content${query({ v: version, part })}`;
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => { if (!settled) { settled = true; clearTimeout(timer); fn(value); } };
    const timer = setTimeout(() => finish(reject, timeoutError()), TIMEOUT);
    const done = (res) => {
      if (res.statusCode === 200) { finish(resolve, res.data); return; }
      const err = new Error(`附件下载失败（${res.statusCode}）`);
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
  return request('/api/v1/me');
}

function agree(version) {
  return request('/api/v1/me/agreement', null, { method: 'POST', data: { version } });
}

// 注销：删除服务端的用户和关注、订阅、纠错、埋点
function deleteMe() {
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
  return request('/api/v1/me/data');
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
  watchStartup, stopStartup, isLive, request, snapshotOnce, coverage, home, search, deadlineReport, listBoard, detail, calendar, lists, lookup, exportFile, feedback,
  attachmentPart,
  AGREED_KEY, forgetAgreed, me, agree, deleteMe, myFollows, createFollow, deleteFollow, seenFollow, followNew, mySubscriptions,
  myData, renameFollow, deleteFeedback, deleteSubscription,
  follow, files, saveSubscription,
};
