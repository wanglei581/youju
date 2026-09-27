// 数据接口：正式版走微信云托管（callContainer），开发时可连本机后端，都没配置时用演示数据。
// 接口返回格式与 mock/data.js 一致，页面不用区分数据来自哪里。
const config = require('./config.js');
const mock = require('../mock/data.js');
const { mockSearch } = require('./mockSearch.js');

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
  const err = new Error(typeof detail === 'string' ? detail : `服务出错了（${res ? res.statusCode : '网络'}）`);
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
  const header = method === 'GET' ? {} : { 'content-type': 'application/json' };
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => { if (!settled) { settled = true; clearTimeout(timer); fn(value); } };
    // 15 秒没有返回就算超时
    const timer = setTimeout(() => finish(reject, new Error('网络不太好，请稍后再试')), 15000);
    const ok = (code) => code === 200 || code === 201 || code === 204;
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

function home() {
  if (isLive()) return request('/api/v1/home');
  return copy({ stats: mock.stats, items: mock.home });
}

function search(params) {
  if (isLive()) return request('/api/v1/search', params);
  return copy(mockSearch(mock.search.items, params));
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
        date: doc.date, url: doc.url, deadline: null, points: [['发文机关', doc.issuer]], excerpt: null, attachments: [], checked: null,
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
    const page = (offset, acc) => request('/api/v1/calendar', { limit: 200, offset }).then((r) => {
      const items = acc.concat(r.items);
      return items.length < r.total && r.items.length && items.length < 1000 ? page(items.length, items) : { items, total: r.total };
    });
    return page(0, []);
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
  isLive, request, home, search, detail, calendar, lists, lookup, exportFile, feedback,
  AGREED_KEY, forgetAgreed, me, agree, deleteMe, myFollows, createFollow, deleteFollow, seenFollow, followNew, mySubscriptions,
  follow, files, saveSubscription,
};
