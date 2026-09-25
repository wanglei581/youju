/* Records selected for export, kept across searches and pages and persisted
 * locally. Holds only { id, title }; the server re-checks publication on export. */
const config = require('./config');
const storage = require('./storage');

const KEY = config.STORAGE_KEYS.selection;
let cache = null;

function valid(entry) {
  return entry && Number.isInteger(entry.id) && entry.id > 0;
}

function load() {
  if (!cache) {
    const saved = storage.read(KEY, []);
    cache = Array.isArray(saved) ? saved.filter(valid) : [];
  }
  return cache;
}

function save() {
  storage.write(KEY, cache);
}

function list() {
  return load().slice();
}

function ids() {
  return load().map((entry) => entry.id);
}

function count() {
  return load().length;
}

function has(id) {
  const target = Number(id);
  return load().some((entry) => entry.id === target);
}

/** Returns { ok, reason } — reason is 'limit' when MAX_SELECTED is reached. */
function add(id, title) {
  const target = Number(id);
  if (!Number.isInteger(target) || target < 1) return { ok: false, reason: 'invalid' };
  if (has(target)) return { ok: true };
  if (load().length >= config.MAX_SELECTED) return { ok: false, reason: 'limit' };
  cache.push({ id: target, title: String(title || '').slice(0, 120) });
  save();
  return { ok: true };
}

function remove(id) {
  const target = Number(id);
  const before = load().length;
  cache = load().filter((entry) => entry.id !== target);
  if (cache.length !== before) save();
  return cache.length !== before;
}

function toggle(id, title) {
  if (has(id)) {
    remove(id);
    return { ok: true, selected: false };
  }
  const result = add(id, title);
  return { ok: result.ok, reason: result.reason, selected: result.ok };
}

/** Adds as many as the limit allows; returns { added, limited }. */
function addMany(items) {
  let added = 0;
  let limited = false;
  (items || []).forEach((item) => {
    if (limited || has(item.id)) return;
    const result = add(item.id, item.title);
    if (result.ok) added += 1;
    else if (result.reason === 'limit') limited = true;
  });
  return { added, limited };
}

function removeMany(idList) {
  const drop = {};
  (idList || []).forEach((id) => { drop[Number(id)] = true; });
  cache = load().filter((entry) => !drop[entry.id]);
  save();
}

function clear() {
  cache = [];
  save();
}

module.exports = { list, ids, count, has, add, remove, toggle, addMany, removeMany, clear };
