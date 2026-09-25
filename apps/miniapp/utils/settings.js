/* API base URL setting. Stored locally; defaults to the local pilot service. */
const config = require('./config');
const storage = require('./storage');

const KEY = config.STORAGE_KEYS.settings;
const HOST_PATTERN = /^[a-z0-9.\-]+$|^\[[0-9a-f:.]+\]$/i;

/**
 * Normalizes user input into `scheme://host[:port][/path]` without a trailing slash.
 * A missing scheme is treated as http:// because the default target is a local service.
 */
function normalizeBaseUrl(input) {
  let text = String(input || '').trim();
  if (!text) return { ok: false, error: '请输入服务地址' };
  if (!/^[a-z][a-z0-9+.\-]*:\/\//i.test(text)) text = 'http://' + text;
  const match = /^(https?):\/\/([^\s/?#@]+)(\/[^\s?#]*)?$/i.exec(text);
  if (!match) {
    return { ok: false, error: '地址须为 http:// 或 https:// 开头，且不含账号、空格、查询参数，例如 http://127.0.0.1:8766' };
  }
  const authority = match[2];
  const portMatch = /^(.*?)(?::(\d{1,5}))?$/.exec(authority);
  const host = portMatch[1];
  const port = portMatch[2];
  if (!host || !HOST_PATTERN.test(host)) return { ok: false, error: '主机名无效' };
  if (port !== undefined && (Number(port) < 1 || Number(port) > 65535)) return { ok: false, error: '端口须在 1–65535 之间' };
  const path = (match[3] || '').replace(/\/+$/, '');
  return { ok: true, value: match[1].toLowerCase() + '://' + authority.toLowerCase() + path };
}

function isLocalHost(baseUrl) {
  return /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(\/|$)/i.test(baseUrl);
}

function getBaseUrl() {
  const saved = storage.read(KEY, {});
  const normalized = normalizeBaseUrl(saved && saved.baseUrl);
  return normalized.ok ? normalized.value : config.DEFAULT_BASE_URL;
}

function setBaseUrl(input) {
  const normalized = normalizeBaseUrl(input);
  if (!normalized.ok) return normalized;
  const saved = storage.read(KEY, {});
  storage.write(KEY, Object.assign({}, saved, { baseUrl: normalized.value }));
  return normalized;
}

function resetBaseUrl() {
  return setBaseUrl(config.DEFAULT_BASE_URL);
}

/** Short label for UI, e.g. "127.0.0.1:8766". */
function shortLabel(baseUrl) {
  return String(baseUrl || '').replace(/^https?:\/\//i, '');
}

module.exports = { normalizeBaseUrl, isLocalHost, getBaseUrl, setBaseUrl, resetBaseUrl, shortLabel };
