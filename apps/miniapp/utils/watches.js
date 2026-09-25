/* Locally saved watch queries (关注). There is no background monitoring:
 * a watch is only checked when the user taps 检查更新. Each successful check
 * stores a snapshot of { id, version_no, title } and is compared with the
 * previous *successful* snapshot. A failed check never reads as "no update". */
const api = require('./api');
const config = require('./config');
const labels = require('./labels');
const settings = require('./settings');
const storage = require('./storage');
const format = require('./format');

const KEY = config.STORAGE_KEYS.watches;
const MAX_CHANGE_ROWS = 30;

function load() {
  const saved = storage.read(KEY, []);
  return Array.isArray(saved) ? saved.filter((w) => w && typeof w.id === 'string' && w.filters) : [];
}

function persist(list) {
  return storage.write(KEY, list);
}

function list() {
  return load();
}

function get(id) {
  return load().find((w) => w.id === id) || null;
}

function upsert(watch) {
  const all = load();
  const index = all.findIndex((w) => w.id === watch.id);
  if (index >= 0) all[index] = watch;
  else all.unshift(watch);
  persist(all);
  return watch;
}

function remove(id) {
  persist(load().filter((w) => w.id !== id));
}

function clear() {
  storage.remove(KEY);
}

function sameFilters(a, b) {
  return api.FILTER_KEYS.every((key) => (a[key] || '') === (b[key] || ''));
}

function newId() {
  return 'w' + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36);
}

function snapshotOf(items) {
  return (items || []).map((item) => ({
    id: Number(item.id),
    v: Number(item.version_no !== undefined ? item.version_no : item.versionNo) || 0,
    t: format.truncate(item.title, 80),
  })).filter((row) => Number.isInteger(row.id) && row.id > 0);
}

/**
 * Creates a watch. When `baselineItems` is given (results the user is already
 * looking at), they become the baseline snapshot without another request.
 * Returns { ok, watch, message }.
 */
function create(input, baselineItems) {
  const filters = api.cleanFilters(input && input.filters);
  if (filters.kind && !labels.KIND_LABELS[filters.kind]) return { ok: false, message: '类型无效' };
  const all = load();
  if (all.length >= config.MAX_WATCHES) return { ok: false, message: '最多保存 ' + config.MAX_WATCHES + ' 个关注，请先删除不用的' };
  if (all.some((w) => sameFilters(w.filters, filters))) return { ok: false, message: '已关注相同条件' };
  const name = String((input && input.name) || '').trim().slice(0, 40) || labels.describeFilters(filters) || '全部已发布资料';
  const now = Date.now();
  const watch = {
    id: newId(),
    name,
    filters,
    createdAt: now,
    baseUrl: '',
    lastAttemptAt: 0,
    lastSuccessAt: 0,
    lastError: '',
    lastCount: 0,
    capped: false,
    snapshot: [],
    changes: null,
  };
  if (Array.isArray(baselineItems)) {
    const snapshot = snapshotOf(baselineItems);
    Object.assign(watch, {
      baseUrl: settings.getBaseUrl(),
      lastAttemptAt: now,
      lastSuccessAt: now,
      lastCount: snapshot.length,
      capped: snapshot.length >= config.RESULT_LIMIT,
      snapshot,
      changes: { baseline: true, baseChanged: false, added: [], revised: [], missing: [] },
    });
  }
  upsert(watch);
  return { ok: true, watch };
}

function diff(previous, current) {
  const before = {};
  previous.forEach((row) => { before[row.id] = row; });
  const seen = {};
  const added = [];
  const revised = [];
  current.forEach((row) => {
    seen[row.id] = true;
    const old = before[row.id];
    if (!old) added.push({ id: row.id, title: row.t, version: row.v });
    else if (old.v !== row.v) revised.push({ id: row.id, title: row.t, from: old.v, version: row.v });
  });
  const missing = previous.filter((row) => !seen[row.id]).map((row) => ({ id: row.id, title: row.t, version: row.v }));
  return {
    added: added.slice(0, MAX_CHANGE_ROWS),
    revised: revised.slice(0, MAX_CHANGE_ROWS),
    missing: missing.slice(0, MAX_CHANGE_ROWS),
    addedTotal: added.length,
    revisedTotal: revised.length,
    missingTotal: missing.length,
  };
}

/**
 * Manually checks one watch. Resolves (never rejects) with { ok, watch, error }.
 * The first successful check (or the first after the API address changed)
 * only establishes a baseline and is not counted as new.
 */
function refresh(id) {
  const watch = get(id);
  if (!watch) return Promise.resolve({ ok: false, watch: null, error: new Error('关注不存在') });
  const baseUrl = settings.getBaseUrl();
  const attemptAt = Date.now();
  return api.searchResources(watch.filters).then((data) => {
    const current = snapshotOf(Array.isArray(data.items) ? data.items : []);
    const baseChanged = !!watch.baseUrl && watch.baseUrl !== baseUrl;
    const baseline = !watch.lastSuccessAt || baseChanged;
    const changes = baseline
      ? { baseline: true, baseChanged, added: [], revised: [], missing: [] }
      : Object.assign({ baseline: false, baseChanged: false, previousCapped: !!watch.capped }, diff(watch.snapshot || [], current));
    const updated = Object.assign({}, watch, {
      baseUrl,
      lastAttemptAt: attemptAt,
      lastSuccessAt: attemptAt,
      lastError: '',
      lastCount: current.length,
      capped: current.length >= config.RESULT_LIMIT,
      snapshot: current,
      changes,
    });
    upsert(updated);
    return { ok: true, watch: updated };
  }, (error) => {
    const current = get(id);
    if (!current) return { ok: false, watch: null, error };
    const updated = Object.assign({}, current, { lastAttemptAt: attemptAt, lastError: error.message || '检查失败' });
    upsert(updated);
    return { ok: false, watch: updated, error };
  });
}

/** One-line result for a toast after a check. */
function resultSummary(result) {
  if (!result.ok) return '检查失败，未更新对比结果';
  const c = result.watch.changes || {};
  if (c.baseline) return '已建立基线：' + result.watch.lastCount + ' 条';
  const total = (c.addedTotal || 0) + (c.revisedTotal || 0) + (c.missingTotal || 0);
  if (!total) return '与上次成功检查相比无变化';
  return '新增 ' + (c.addedTotal || 0) + ' · 修订 ' + (c.revisedTotal || 0) + ' · 未再出现 ' + (c.missingTotal || 0);
}

module.exports = { list, get, create, remove, clear, refresh, resultSummary, sameFilters };
