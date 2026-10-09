const { INDUSTRIES, NATIONAL, requestScope } = require('./conditions.js');

const STORAGE_KEY = 'youjuLocalClientsV1';
const PROVINCES = ['北京', '天津', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江', '上海', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北', '湖南', '广东', '广西', '海南', '重庆', '四川', '贵州', '云南', '西藏', '陕西', '甘肃', '青海', '宁夏', '新疆'];
const MAX_CLIENTS = 20;

const unique = (values) => [...new Set(values)];
const strings = (values) => Array.isArray(values) ? values.filter((v) => typeof v === 'string') : [];

function normalize(value) {
  const c = value && typeof value === 'object' ? value : {};
  return {
    id: typeof c.id === 'string' ? c.id : '',
    name: typeof c.name === 'string' ? c.name.trim().slice(0, 40) : '',
    regions: unique(strings(c.regions).filter((v) => PROVINCES.includes(v))),
    cities: unique(strings(c.cities).filter((v) => {
      const parts = v.split('/');
      return parts.length === 2 && PROVINCES.includes(parts[0]) && parts[1].length > 0 && parts[1].length <= 32;
    })),
    industries: unique(strings(c.industries).filter((v) => INDUSTRIES.includes(v))),
    keywords: unique(strings(c.keywords).map((v) => v.trim()).filter(Boolean)),
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
  next.forEach((c) => {
    if (!c.id || c.keywords.length > 5 || c.keywords.some((v) => Array.from(v).length > 12)) {
      throw new Error('关键词最多 5 个，每个不超过 12 字');
    }
  });
  wx.setStorageSync(STORAGE_KEY, next);
  return next;
}

function clientParams(client, ignoreIndustry) {
  const c = normalize(client);
  // 省和城市取并集；没有选择地区时全国检索，始终包含国家部委。
  const scope = c.regions.length ? requestScope(c.regions, [NATIONAL])
    : { regions: c.cities.length ? NATIONAL : '不限地区', levels: undefined };
  return {
    kind: '申报通知', open: 1, time: '不限', regions: scope.regions, levels: scope.levels,
    cities: c.cities.length ? c.cities.join(',') : undefined,
    industries: !ignoreIndustry && c.industries.length ? c.industries.join(',') : undefined,
  };
}

function subtitle(client, ignoreIndustry) {
  const c = normalize(client);
  const regions = c.regions.concat(c.cities.map((v) => v.replace('/', '·')));
  return `${regions.length ? regions.join('、') + '（含国家部委）' : '不限地区（含国家部委）'} · ${!ignoreIndustry && c.industries.length ? c.industries.join('、') : '不限产业方向'} · 申报通知 · 只看未截止 · 不限时间`;
}

// 输出纯文本片段，避免把关键词或官方原句作为富文本执行；重叠关键词的区间取并集。
function highlightWords(text, keywords) {
  const marked = new Array(text.length).fill(false);
  normalize({ keywords }).keywords.slice(0, 5).filter((v) => Array.from(v).length <= 12).forEach((word) => {
    let from = 0;
    let at;
    while ((at = text.indexOf(word, from)) !== -1) {
      for (let i = at; i < at + word.length; i += 1) marked[i] = true;
      from = at + 1;
    }
  });
  const segments = [];
  for (let i = 0; i < text.length; i += 1) {
    const hit = marked[i];
    const last = segments[segments.length - 1];
    if (last && last.hit === hit) last.text += text[i];
    else segments.push({ text: text[i], hit });
  }
  return segments;
}

function conditionQuotes(doc) {
  // 当前公开详情接口只返回已人工确认的 apply_conditions；其他字段不作为条件展示。
  const value = doc && doc.apply_conditions;
  if (!value || !value.checked || !Array.isArray(value.items)) return [];
  return unique(value.items.filter((v) => v && typeof v.quote === 'string' && v.quote).map((v) => v.quote));
}

module.exports = { PROVINCES, INDUSTRIES, MAX_CLIENTS, STORAGE_KEY, normalize, loadClients, saveClients, clientParams, subtitle, highlightWords, conditionQuotes };
