/* Display vocabulary and view-model builders. Codes mirror services/api/store.py
 * (KINDS, RIGHTS, review_status). Nothing here invents data: unknown codes are
 * shown as-is, missing fields are shown as "未标注"/"待核". */
const format = require('./format');

const KIND_OPTIONS = [
  { value: '', label: '全部类型' },
  { value: 'policy', label: '政策' },
  { value: 'application', label: '申报通知' },
  { value: 'list', label: '企业名单' },
  { value: 'statistics', label: '统计数据' },
  { value: 'report', label: '研究报告' },
  { value: 'other', label: '其他' },
];

const KIND_LABELS = {};
KIND_OPTIONS.forEach((option) => { if (option.value) KIND_LABELS[option.value] = option.label; });

/** Task shortcuts on the 找答案 page. Each maps to a real server-side `kind` filter. */
const TEMPLATES = [
  { kind: 'policy', title: '查政策条件', hint: '政策法规', glyph: '政', tone: 'deep' },
  { kind: 'application', title: '查申报节点', hint: '申报通知', glyph: '报', tone: 'copper' },
  { kind: 'list', title: '查企业名单', hint: '认定与名单', glyph: '单', tone: 'deep' },
  { kind: 'statistics', title: '查行业指标', hint: '统计数据', glyph: '数', tone: 'copper' },
  { kind: 'report', title: '找研究报告', hint: '题录与原站', glyph: '研', tone: 'deep' },
];

const RIGHTS = {
  directory_only: {
    label: '仅题录',
    long: '仅题录与原站链接',
    tone: 'muted',
    desc: '该来源只获准展示标题、日期与原站链接。本库未阅读或概括正文，请到原站核对原文。',
  },
  short_excerpt: {
    label: '题录＋短摘',
    long: '题录与 150 字内短摘要',
    tone: 'copper',
    desc: '该来源获准展示 150 字以内的短摘要，不提供答案结论。摘要不能替代原文。',
  },
  evidence_use: {
    label: '可用证据',
    long: '可用证据与答案',
    tone: 'ok',
    desc: '该来源已登记证据使用依据。答案须由管理员核对证据位置并填写审核说明后才会发布。',
  },
};

const STATUS = {
  verified: { label: '已核验答案', tone: 'ok' },
  metadata: { label: '仅题录', tone: 'muted' },
  pending: { label: '待审', tone: 'warn' },
};

function rightsInfo(code) {
  return RIGHTS[code] || { label: code ? String(code) : '权限未标注', long: code ? String(code) : '权限未标注', tone: 'warn', desc: '服务未返回可识别的使用范围，请以原站为准。' };
}

function statusInfo(code) {
  return STATUS[code] || { label: code ? String(code) : '状态未知', tone: 'warn' };
}

function kindLabel(code) {
  return KIND_LABELS[code] || (code ? String(code) : '未分类');
}

function kindIndex(code) {
  const index = KIND_OPTIONS.findIndex((option) => option.value === code);
  return index < 0 ? 0 : index;
}

/**
 * Coverage of a single published record, following the spec's
 * 有依据 / 部分覆盖 / 未覆盖 vocabulary at record level.
 */
function coverage(item) {
  if (item.review_status === 'verified' && item.answer) {
    return { label: '有依据', tone: 'ok', desc: '管理员已核对证据位置并发布答案。请结合原站与证据位置使用。' };
  }
  if (item.summary) {
    return { label: '题录＋短摘', tone: 'copper', desc: '只有题录与摘要，本库没有核验答案；摘要不能替代原文。' };
  }
  return { label: '仅题录', tone: 'muted', desc: '只有题录：本库未核验正文答案，请打开原站阅读原文，不以此代替原文件。' };
}

/** Human summary of a filter set, e.g. "申报通知 · 青岛 · 医疗 · “专精特新”". */
function describeFilters(filters) {
  const parts = [];
  const f = filters || {};
  if (f.kind) parts.push(kindLabel(f.kind));
  if (f.region) parts.push(f.region);
  if (f.industry) parts.push(f.industry);
  if (f.q) parts.push('“' + f.q + '”');
  return parts.join(' · ');
}

function filterChips(filters) {
  const f = filters || {};
  const chips = [];
  if (f.q) chips.push({ key: 'q', text: '关键词 ' + f.q });
  if (f.kind) chips.push({ key: 'kind', text: kindLabel(f.kind) });
  if (f.industry) chips.push({ key: 'industry', text: '行业 ' + f.industry });
  if (f.region) chips.push({ key: 'region', text: '地区 ' + f.region });
  return chips;
}

/** Flattens a public resource into the fields the WXML needs. */
function decorateResource(raw) {
  const item = raw || {};
  const rights = rightsInfo(item.rights_level);
  const status = statusInfo(item.review_status);
  const cover = coverage(item);
  const verified = item.review_status === 'verified' && !!item.answer;
  return {
    id: Number(item.id),
    title: String(item.title || '（无标题）'),
    kind: String(item.kind || ''),
    kindLabel: kindLabel(item.kind),
    industry: String(item.industry || ''),
    region: String(item.region || ''),
    publishedAt: String(item.published_at || ''),
    dateLabel: item.published_at ? '发布 ' + item.published_at : '日期待核',
    verified,
    answer: verified ? String(item.answer) : '',
    summary: String(item.summary || ''),
    evidenceLocator: String(item.evidence_locator || ''),
    publisher: String(item.publisher || '发布机构未标注'),
    sourceName: String(item.source_name || '来源未标注'),
    rightsLevel: String(item.rights_level || ''),
    rightsLabel: rights.label,
    rightsLong: rights.long,
    rightsTone: rights.tone,
    rightsDesc: rights.desc,
    statusLabel: status.label,
    statusTone: status.tone,
    coverageLabel: cover.label,
    coverageTone: cover.tone,
    coverageDesc: cover.desc,
    versionNo: Number(item.version_no) || 0,
    reviewedDay: format.isoDay(item.reviewed_at),
    originalUrl: String(item.original_url || ''),
    originalHost: format.urlHost(item.original_url),
    homepageUrl: String(item.homepage_url || ''),
  };
}

function decorateSource(raw) {
  const source = raw || {};
  const rights = rightsInfo(source.rights_level);
  return {
    id: Number(source.id),
    name: String(source.name || '未命名来源'),
    publisher: String(source.publisher || '发布机构未标注'),
    homepageUrl: String(source.homepage_url || ''),
    homepageHost: format.urlHost(source.homepage_url),
    rightsLabel: rights.label,
    rightsLong: rights.long,
    rightsTone: rights.tone,
    rightsDesc: rights.desc,
  };
}

module.exports = {
  KIND_OPTIONS,
  KIND_LABELS,
  TEMPLATES,
  RIGHTS,
  kindLabel,
  kindIndex,
  rightsInfo,
  statusInfo,
  coverage,
  describeFilters,
  filterChips,
  decorateResource,
  decorateSource,
};
