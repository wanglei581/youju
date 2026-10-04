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
  const err = new Error(typeof detail === 'string' ? detail : (validation || `服务出错了（${res ? res.statusCode : '网络'}）`));
  err.status = res ? res.statusCode : 0;
  return err;
}

// 本机记下的已同意协议版本（服务端才是准的；服务端返回 403 时清掉，下次操作重新请求同意）
const AGREED_KEY = 'youjuAgreedVersion';

function forgetAgreed() {
  try { wx.removeStorageSync(AGREED_KEY); } catch (e) { /* 忽略 */ }
}

// options.method 默认 GET；POST、DELETE 时 options.data 按 JSON 发送
function request(path, params, options) {
  const url = path + query(params);
  const method = (options && options.method) || 'GET';
  const data = options && options.data;
  const header = Object.assign(method === 'GET' ? {} : { 'content-type': 'application/json' }, options && options.header);
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => { if (!settled) { settled = true; clearTimeout(timer); fn(value); } };
    // 15 秒没有返回就算超时
    const timer = setTimeout(() => finish(reject, new Error('网络不太好，请稍后再试')), 15000);
    const ok = (code) => code === 200 || code === 201 || code === 202 || code === 204;
    const done = (res) => {
      if (ok(res.statusCode)) { finish(resolve, res.data); return; }
      // 403：服务端没有同意记录（如在别的设备注销过）或协议已更新
      if (res.statusCode === 403) forgetAgreed();
      finish(reject, errorOf(res));
    };
    if (config.cloudEnv) {
      wx.cloud.callContainer({
        config: { env: config.cloudEnv },
        path: url,
        method,
        header: Object.assign({ 'X-WX-SERVICE': config.cloudService }, header),
        data,
        timeout: 15000,
        success: done,
        fail: () => finish(reject, new Error('网络不太好，请稍后再试')),
      });
    } else {
      wx.request({
        url: config.devBaseUrl + url,
        method,
        header,
        data,
        timeout: 15000,
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

function home() {
  if (isLive()) return request('/api/v1/home');
  return copy({ stats: mock.stats, items: mock.home });
}

function search(params) {
  if (isLive()) return request('/api/v1/search', params);
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
  return copy({ days, summary, by_province: by('province'), by_dept: filtered.reduce((out, item) => { const key = deptOf(item.dept); out[key] = (out[key] || 0) + 1; return out; }, {}), total: filtered.length, page, size, items: filtered.slice((page - 1) * size, page * size) });
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
        id: doc.id, kind: doc.kind, tags: [doc.kind], region: doc.region, title: doc.title, issuer: doc.issuer,
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
        : { items, past, total: r.total, pastTotal: r.pastTotal || 0, truncated: r.total > items.length || (r.pastTotal || 0) > past.length };
    });
    return page(0, [], []);
  }
  return copy({ items: mock.calendar.map((d) => Object.assign({ docId: d.id }, d)) });
}

function lists() {
  if (isLive()) return request('/api/v1/lists/recent', { limit: 20 }).then((r) => ({ week_total: r.week_total, total: r.total, items: r.items }));
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
        responseType: 'arraybuffer',
        timeout: 30000,
        success: done,
        fail,
      });
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
  return request('/api/v1/me/agreement', null, { method: 'POST', data: scope ? { version, scope } : { version } });
}

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

let capabilitiesPromise;
let capabilitiesPending;
function advisorCapabilities(options = {}) {
  if (capabilitiesPending) return capabilitiesPending;
  if (capabilitiesPromise && !options.refresh) return capabilitiesPromise;
  const unavailable = () => ({ data_tools: { enabled: false, features: [], remaining: {} }, ai_advisor: { enabled: false, features: [], remaining: {} } });
  // 演示仍保留数据工具；只有后端返回的白名单权限才能打开解读入口。
  const requestResult = isLive() ? request('/api/v1/advisor/capabilities') : advisorMock.status().then((status) => ({
    data_tools: { enabled: status.enabled === true, features: ['compare', 'track'], remaining: Object.entries(status.quota).reduce((remaining, [kind, quota]) => {
      remaining[kind] = Math.max(0, quota.limit - quota.used); return remaining;
    }, {}) },
    ai_advisor: { enabled: false, features: [], remaining: {} },
  }));
  const result = requestResult.then((res) => ({
    data_tools: Object.assign({}, res.data_tools, { enabled: !!res.data_tools && res.data_tools.enabled === true }),
    ai_advisor: Object.assign({}, res.ai_advisor, { enabled: !!res.ai_advisor && res.ai_advisor.enabled === true }),
  })).catch(unavailable);
  capabilitiesPromise = result;
  capabilitiesPending = result;
  result.then(() => { if (capabilitiesPending === result) capabilitiesPending = null; });
  return result;
}

function createAdvisorTask(data, key) {
  if (data.kind === 'interpret') {
    if (!isLive()) return Promise.reject(Object.assign(new Error('暂时没有可用工具'), { status: 404 }));
    data = { kind: 'interpret', doc_id: Number(data.doc_id) };
  }
  return isLive() ? request('/api/v1/advisor/tasks', null, { method: 'POST', data, header: { 'Idempotency-Key': key } }) : advisorMock.create(data, key);
}

function advisorTasks() {
  return isLive() ? request('/api/v1/advisor/tasks') : advisorMock.list();
}

function advisorTask(id) {
  return isLive() ? request(`/api/v1/advisor/tasks/${id}`) : advisorMock.get(id);
}

function cancelAdvisorTask(id) {
  return isLive() ? request(`/api/v1/advisor/tasks/${id}/cancel`, null, { method: 'POST', data: {} }) : advisorMock.cancel(id);
}

function deleteAdvisorTask(id) {
  return isLive() ? request(`/api/v1/advisor/tasks/${id}`, null, { method: 'DELETE' }) : advisorMock.remove(id);
}

function downloadAdvisorTask(id) {
  return isLive() ? request(`/api/v1/advisor/tasks/${id}/download`) : advisorMock.download(id);
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
  isLive, request, coverage, home, search, deadlineReport, listBoard, detail, calendar, lists, lookup, exportFile, feedback,
  AGREED_KEY, forgetAgreed, me, agree, deleteMe, myFollows, createFollow, deleteFollow, seenFollow, followNew, mySubscriptions,
  myData, renameFollow, deleteFeedback, deleteSubscription,
  follow, files, saveSubscription,
  advisorStatus, advisorCapabilities, advisorPreview, createAdvisorTask, advisorTasks, advisorTask, cancelAdvisorTask, deleteAdvisorTask, downloadAdvisorTask,
};
