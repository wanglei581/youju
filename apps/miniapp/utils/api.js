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
  return new Error(typeof detail === 'string' ? detail : `服务出错了（${res ? res.statusCode : '网络'}）`);
}

function request(path, params) {
  const url = path + query(params);
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => { if (!settled) { settled = true; clearTimeout(timer); fn(value); } };
    // 15 秒没有返回就算超时
    const timer = setTimeout(() => finish(reject, new Error('网络不太好，请稍后再试')), 15000);
    const done = (res) => (res.statusCode === 200 ? finish(resolve, res.data) : finish(reject, errorOf(res)));
    if (config.cloudEnv) {
      wx.cloud.callContainer({
        config: { env: config.cloudEnv },
        path: url,
        method: 'GET',
        header: { 'X-WX-SERVICE': config.cloudService },
        timeout: 15000,
        success: done,
        fail: () => finish(reject, new Error('网络不太好，请稍后再试')),
      });
    } else {
      wx.request({
        url: config.devBaseUrl + url,
        method: 'GET',
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

// 关注和导出记录需要登录（#30）后由服务端保存，目前用演示数据
function follow() {
  return copy(mock.follow);
}

function files() {
  return copy(mock.files);
}

// 保存一次性订阅（#30 服务端实现前一律失败，界面不会误报"已订阅"）
function saveSubscription() {
  return Promise.reject(new Error('订阅服务尚未上线'));
}

module.exports = { isLive, home, search, detail, calendar, lists, lookup, follow, files, saveSubscription };
