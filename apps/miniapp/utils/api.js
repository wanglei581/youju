/* Public API client for services/api (server.py). Only public endpoints are used:
 *   GET  /api/health
 *   GET  /api/public/sources
 *   GET  /api/public/resources?q=&kind=&industry=&region=
 *   GET  /api/public/resources/{id}
 *   POST /api/public/exports  {ids:[int], format:'xlsx'|'docx'} -> Office file bytes
 * Every call returns a Promise; rejected errors carry `kind`
 * ('network' | 'timeout' | 'domain' | 'abort' | 'http' | 'format') and, for HTTP errors, `status`. */
const config = require('./config');
const settings = require('./settings');

const FILTER_KEYS = ['q', 'kind', 'industry', 'region'];

function makeError(kind, message, extra) {
  const error = new Error(message);
  error.kind = kind;
  if (extra) Object.keys(extra).forEach((key) => { error[key] = extra[key]; });
  return error;
}

function describeFail(errMsg, baseUrl) {
  const msg = String(errMsg || '');
  if (/abort/i.test(msg)) return makeError('abort', '请求已取消', { detail: msg });
  if (/domain list|合法域名|not in domain/i.test(msg)) {
    return makeError('domain', '该地址未列入小程序合法域名。开发者工具请在「详情 → 本地设置」勾选「不校验合法域名」；真机需打开「开发调试」。', { detail: msg });
  }
  if (/timeout|timed out|超时/i.test(msg)) {
    return makeError('timeout', '连接 ' + settings.shortLabel(baseUrl) + ' 超时，请确认本地服务已启动。', { detail: msg });
  }
  return makeError('network', '无法连接 ' + settings.shortLabel(baseUrl) + '。请确认本地服务已启动、地址和端口正确；真机需填写电脑的局域网地址。', { detail: msg });
}

function headerValue(headers, name) {
  const target = name.toLowerCase();
  const source = headers || {};
  for (const key in source) {
    if (Object.prototype.hasOwnProperty.call(source, key) && key.toLowerCase() === target) return String(source[key]);
  }
  return '';
}

/** Minimal UTF-8 decoder (TextDecoder is not available in every mini-program runtime). */
function utf8Decode(buffer, maxBytes) {
  const bytes = new Uint8Array(buffer, 0, Math.min(buffer.byteLength, maxBytes || buffer.byteLength));
  let out = '';
  let i = 0;
  while (i < bytes.length) {
    const b = bytes[i++];
    let cp = 0xfffd;
    if (b < 0x80) cp = b;
    else if (b >= 0xc0 && b < 0xe0 && i < bytes.length) cp = ((b & 0x1f) << 6) | (bytes[i++] & 0x3f);
    else if (b >= 0xe0 && b < 0xf0 && i + 1 < bytes.length) {
      cp = ((b & 0x0f) << 12) | ((bytes[i] & 0x3f) << 6) | (bytes[i + 1] & 0x3f);
      i += 2;
    } else if (b >= 0xf0 && b < 0xf8 && i + 2 < bytes.length) {
      cp = ((b & 0x07) << 18) | ((bytes[i] & 0x3f) << 12) | ((bytes[i + 1] & 0x3f) << 6) | (bytes[i + 2] & 0x3f);
      i += 3;
    }
    if (cp > 0xffff) {
      cp -= 0x10000;
      out += String.fromCharCode(0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff));
    } else {
      out += String.fromCharCode(cp);
    }
  }
  return out;
}

function isArrayBuffer(value) {
  return !!value && typeof value.byteLength === 'number' && typeof value.slice === 'function';
}

/** Office Open XML files are ZIP archives and start with "PK\x03\x04". */
function looksLikeZip(buffer) {
  if (!isArrayBuffer(buffer) || buffer.byteLength < 4) return false;
  const head = new Uint8Array(buffer, 0, 4);
  return head[0] === 0x50 && head[1] === 0x4b && head[2] === 0x03 && head[3] === 0x04;
}

function encodeQuery(params) {
  const parts = [];
  Object.keys(params || {}).forEach((key) => {
    const value = params[key];
    if (value === undefined || value === null || value === '') return;
    parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
  });
  return parts.length ? '?' + parts.join('&') : '';
}

/** Keeps only non-empty, trimmed, length-bounded filter values understood by the server. */
function cleanFilters(filters) {
  const result = {};
  FILTER_KEYS.forEach((key) => {
    const value = String((filters && filters[key]) || '').trim().slice(0, config.FILTER_MAX_LENGTH);
    if (value) result[key] = value;
  });
  return result;
}

/**
 * Low-level request. Returns a Promise with an `abort()` method.
 * Resolves with the raw wx.request success result (any HTTP status).
 */
function request(options) {
  const baseUrl = options.baseUrl || settings.getBaseUrl();
  let task = null;
  let aborted = false;
  const binary = options.responseType === 'arraybuffer';
  const promise = new Promise((resolve, reject) => {
    task = wx.request({
      url: baseUrl + options.path,
      method: options.method || 'GET',
      data: options.data,
      header: Object.assign({ Accept: 'application/json' }, options.header || {}),
      timeout: options.timeout || config.REQUEST_TIMEOUT,
      dataType: binary ? 'binary' : 'json',
      responseType: binary ? 'arraybuffer' : 'text',
      success: resolve,
      fail(err) {
        reject(aborted ? makeError('abort', '请求已取消') : describeFail(err && err.errMsg, baseUrl));
      },
    });
  });
  promise.abort = () => {
    aborted = true;
    if (task && typeof task.abort === 'function') task.abort();
  };
  return promise;
}

function parseJson(res) {
  const status = res.statusCode;
  let data = res.data;
  if (typeof data === 'string') {
    try { data = JSON.parse(data); } catch (error) { data = null; }
  }
  if (status < 200 || status >= 300) {
    if (data && typeof data.error === 'string' && data.error) throw makeError('http', data.error, { status });
    if (status === 404) throw makeError('http', '接口不存在（HTTP 404）。请确认服务地址指向行业资料 API，而不是其他网页服务。', { status });
    throw makeError('http', '服务返回错误（HTTP ' + status + '）', { status });
  }
  if (!data || typeof data !== 'object') {
    throw makeError('format', '服务返回的不是有效 JSON，请确认地址指向行业资料 API。', { status });
  }
  return data;
}

function getJson(path, options) {
  const req = request(Object.assign({ path }, options || {}));
  const result = req.then(parseJson);
  result.abort = req.abort;
  return result;
}

/**
 * Checks /api/health. Never rejects: resolves with
 * { ok, baseUrl, latency, product, message, kind, checkedAt }.
 */
function checkHealth(baseUrl) {
  const target = baseUrl || settings.getBaseUrl();
  const started = Date.now();
  return getJson('/api/health', { baseUrl: target, timeout: 8000 }).then((data) => {
    const latency = Date.now() - started;
    if (data.status !== 'ok' || data.product !== config.EXPECTED_PRODUCT) {
      return {
        ok: false, baseUrl: target, latency, kind: 'format', checkedAt: Date.now(),
        product: String(data.product || ''),
        message: '该地址有响应，但不是行业资料 API（未返回 ' + config.EXPECTED_PRODUCT + '）。',
      };
    }
    return { ok: true, baseUrl: target, latency, product: data.product, message: '已连接', kind: '', checkedAt: Date.now() };
  }, (error) => ({
    ok: false, baseUrl: target, latency: Date.now() - started, product: '',
    message: error.message, kind: error.kind || 'network', checkedAt: Date.now(),
  }));
}

function searchResources(filters) {
  return getJson('/api/public/resources' + encodeQuery(cleanFilters(filters)));
}

function getResource(id) {
  return getJson('/api/public/resources/' + encodeURIComponent(String(id)));
}

function listSources() {
  return getJson('/api/public/sources');
}

/**
 * Requests an export of the selected published records.
 * Resolves with { data: ArrayBuffer, contentType, size }.
 */
function exportRecords(ids, format) {
  if (format !== 'xlsx' && format !== 'docx') return Promise.reject(makeError('format', '只支持 xlsx 或 docx'));
  const cleanIds = (ids || []).map(Number).filter((id) => Number.isInteger(id) && id > 0);
  if (!cleanIds.length) return Promise.reject(makeError('format', '请先选择要导出的资料'));
  return request({
    path: '/api/public/exports',
    method: 'POST',
    data: JSON.stringify({ ids: cleanIds, format }),
    header: {
      'content-type': 'application/json',
      Accept: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/vnd.openxmlformats-officedocument.wordprocessingml.document, application/json',
    },
    responseType: 'arraybuffer',
    timeout: config.EXPORT_TIMEOUT,
  }).then((res) => {
    const status = res.statusCode;
    const body = res.data;
    if (status < 200 || status >= 300) {
      let message = '';
      if (isArrayBuffer(body) && body.byteLength) {
        try {
          const parsed = JSON.parse(utf8Decode(body, 8192));
          if (parsed && typeof parsed.error === 'string') message = parsed.error;
        } catch (error) { /* non-JSON error body */ }
      }
      throw makeError('http', message || '导出失败（HTTP ' + status + '）', { status });
    }
    if (!isArrayBuffer(body) || !body.byteLength) throw makeError('format', '服务返回了空文件');
    const contentType = headerValue(res.header, 'Content-Type');
    if (contentType.indexOf('openxmlformats') === -1 || !looksLikeZip(body)) {
      throw makeError('format', '服务返回的不是 Office 文件，请确认服务地址。');
    }
    return { data: body, contentType, size: body.byteLength };
  });
}

module.exports = {
  FILTER_KEYS,
  cleanFilters,
  encodeQuery,
  utf8Decode,
  looksLikeZip,
  checkHealth,
  searchResources,
  getResource,
  listSources,
  exportRecords,
};
