const { INDUSTRIES, NATIONAL, requestScope } = require('./conditions.js');
const { PROVINCES, MUNICIPALITIES, SEPARATORS, areaParts, areaLabel, withoutCityProvinces, withoutDistrictCities } = require('./regions.js');
const { dueView } = require('./format.js');

// 我的客户（D-061）：客户卡片只存在本机，名称不上传；检索只发送地区和产业方向。
// 区县（districts，"省/市/区县"）是后加的字段：旧记录没有这一项时按空处理，地区、城市、产业方向原样保留，键名不变
const STORAGE_KEY = 'youjuLocalClientsV1';

const MAX_CLIENTS = 20;
// 每组检索条件取最新的 50 份（接口一页最多 50）；合并后最多列 200 份（导出一次最多 200 份）
const PER_QUERY = 50;
const MAX_DOCS = 200;
const TIME = '近 90 天';

const unique = (values) => [...new Set(values)];
const strings = (values) => Array.isArray(values) ? values.filter((v) => typeof v === 'string') : [];
const single = (value) => value.length > 0 && value.length <= 32 && !SEPARATORS.test(value);

// 区县必须带所属省、市；直辖市的市写本市简称
function validDistrict(key) {
  const parts = key.split('/');
  const { province, city, district } = areaParts(key);
  return parts.length === 3 && PROVINCES.includes(province) && single(city) && single(district)
    && (!MUNICIPALITIES.includes(province) || city === province);
}

// 具体区县替换所属城市和全省，具体城市替换全省（与搜索页选择时的规则一致）
function normalize(value) {
  const c = value && typeof value === 'object' ? value : {};
  const districts = unique(strings(c.districts).filter(validDistrict));
  const cities = withoutDistrictCities(unique(strings(c.cities).filter((v) => {
    const parts = v.split('/');
    return parts.length === 2 && PROVINCES.includes(parts[0]) && single(parts[1]);
  })), districts);
  return {
    id: typeof c.id === 'string' ? c.id : '',
    name: typeof c.name === 'string' ? c.name.trim().slice(0, 40) : '',
    regions: withoutCityProvinces(unique(strings(c.regions).filter((v) => PROVINCES.includes(v))), cities, districts),
    cities,
    districts,
    industries: unique(strings(c.industries).filter((v) => INDUSTRIES.includes(v))),
  };
}

function loadClients() {
  const stored = wx.getStorageSync(STORAGE_KEY);
  if (!stored) return [];
  if (!Array.isArray(stored)) throw new Error('本机客户记录无法读取');
  return stored.slice(0, MAX_CLIENTS).map(normalize).filter((c) => c.id);
}

function saveClients(clients) {
  if (clients.length > MAX_CLIENTS) throw new Error('最多保存 20 个客户');
  const next = clients.map(normalize);
  if (next.some((c) => !c.id)) throw new Error('本机客户记录无法保存');
  wx.setStorageSync(STORAGE_KEY, next);
  return next;
}

// 没填名称的按顺序叫"客户 1、客户 2"
const displayName = (client, index) => client.name || `客户 ${index + 1}`;

// 一家客户的检索条件：近 90 天的申报通知，去掉已核对、确认已截止的。
// 具体城市替换同省的全省条件、具体区县替换所属城市和全省；其他省份仍可并选。没有选择地区时全国检索，始终包含国家部委。
function clientParams(client) {
  const c = normalize(client);
  const scope = c.regions.length ? requestScope(c.regions, [NATIONAL])
    : { regions: c.cities.length || c.districts.length ? NATIONAL : '不限地区', levels: undefined };
  return {
    kind: '申报通知', exclude_closed: true, time: TIME, regions: scope.regions, levels: scope.levels,
    cities: c.cities.length ? c.cities.join(',') : undefined,
    // 没选区县时不带这一项，和加区县之前的请求一样
    districts: c.districts.length ? c.districts.join(',') : undefined,
    industries: c.industries.length ? c.industries.join(',') : undefined,
  };
}

function cardSummary(client) {
  const c = normalize(client);
  const regions = c.regions.concat(c.cities.map(areaLabel), c.districts.map(areaLabel));
  return `${regions.length ? `${regions.join('、')}（含国家部委）` : '不限地区（含国家部委）'} · ${c.industries.length ? c.industries.join('、') : '不限产业方向'}`;
}

// 条件相同的客户只查一次：返回 [{ key, params, ids }]
function queryGroups(clients) {
  const groups = new Map();
  clients.forEach((c) => {
    const params = clientParams(c);
    const key = JSON.stringify(params);
    if (!groups.has(key)) groups.set(key, { key, params, ids: [] });
    groups.get(key).ids.push(c.id);
  });
  return [...groups.values()];
}

// 排序用：核对过的和机器摘录的按剩余天数；可能 7 天内截止、没核对的排在 7 天内的后面；
// 其余（截止见原文、没核对）排最后，按发布日期从新到旧
function urgency(doc, now) {
  const view = doc && doc.due ? dueView(doc.due, now) : null;
  if (view && view.source !== 'unverified' && view.days >= 0) return view.days;
  if (doc && doc.dueSoonUnverified) return 7.5;
  return Infinity;
}

// 把各组的检索结果按通知合并：一条通知一行，记下涉及哪几家客户（按客户卡片的顺序）
// results: [{ ids: [客户编号], items: [通知] }]
function mergeRelated(clients, results, now) {
  const order = new Map(clients.map((c, i) => [c.id, i]));
  const merged = new Map();
  results.forEach(({ ids, items }) => {
    (items || []).forEach((doc) => {
      const id = String(doc.id);
      if (!merged.has(id)) merged.set(id, { id, doc, ids: new Set() });
      ids.forEach((v) => merged.get(id).ids.add(v));
    });
  });
  const rows = [...merged.values()].map((row) => {
    const ids = [...row.ids].filter((v) => order.has(v)).sort((a, b) => order.get(a) - order.get(b));
    return {
      id: row.id, doc: row.doc, ids, key: urgency(row.doc, now),
      names: ids.map((v) => displayName(clients[order.get(v)], order.get(v))).join('、'),
    };
  });
  rows.sort((a, b) => (a.key - b.key) || ((b.doc.date || '') > (a.doc.date || '') ? 1 : (b.doc.date || '') < (a.doc.date || '') ? -1 : 0));
  return rows;
}

module.exports = {
  PROVINCES, INDUSTRIES, MAX_CLIENTS, STORAGE_KEY, PER_QUERY, MAX_DOCS, TIME,
  normalize, loadClients, saveClients, displayName, clientParams, cardSummary, queryGroups, mergeRelated,
};
