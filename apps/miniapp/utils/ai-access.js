// AI 板块（D-075）显示不显示、能不能点：只听 api 服务，各页面共用这一份。
// - 对所有人开放：/advisor/mode 的 open（不需要身份，没同意协议的新用户也看得到，进了板块再走同意）；
// - 白名单阶段：mode 说没对所有人开放，再看能力接口 ai_advisor.enabled（只有白名单里、同意过协议的人）；
// - 两样都不是（首发版 AI 关着）：不显示，任何页面都没有 AI 字样。
// visible 显示不显示，confirmed 这次打开后台答过没有，available 后台现在能不能处理（admin 的心跳）。
// 本机只记"上次是显示的"和时间，24 小时内启动时先画出底栏第 5 格，免得 4 格变 5 格；
// 后台确认之前（confirmed 为假）板块里的按钮、列表上的"AI 解读"都不能点。
// 后台一说关了或不在白名单，立刻擦掉本机记录、通知各页面收起；注销时 clear() 一并清掉。
const api = require('./api.js');

const CACHE_KEY = 'youjuAiVisibleV1';
const CACHE_FOR = 24 * 3600 * 1000;
// AI 首页"刚才看的通知"：本机只记最近 1 份能解读的通知（编号、标题、发文机关、日期），7 天后不再显示
const LAST_DOC_KEY = 'youjuAiLastDocV1';
const LAST_DOC_FOR = 7 * 24 * 3600 * 1000;
// 同一次打开里，页面显示时最多每 10 分钟再问一次后台；没问到（连不上）时 15 秒内不重复问
const RECHECK = 10 * 60 * 1000;
const RETRY_AFTER = 15000;

const listeners = new Set();
let state = null;
let pending = null;
let checkedAt = 0;
let failedAt = 0;

function storage() {
  return typeof wx === 'undefined' ? null : wx;
}

function cached() {
  const store = storage();
  if (!store) return false;
  try {
    const saved = store.getStorageSync(CACHE_KEY);
    const at = saved && Number(saved.at);
    return Number.isFinite(at) && at <= Date.now() && Date.now() - at < CACHE_FOR;
  } catch (e) { return false; }
}

function remember(visible) {
  const store = storage();
  if (!store) return;
  try {
    if (visible) store.setStorageSync(CACHE_KEY, { at: Date.now() });
    else store.removeStorageSync(CACHE_KEY);
  } catch (e) { /* 记不下来时下次启动先不画，等后台回话 */ }
}

function current() {
  if (!state) {
    const visible = cached();
    state = { visible, confirmed: false, available: visible };
  }
  return Object.assign({}, state);
}

function set(next) {
  const before = current();
  state = Object.assign({}, before, next);
  if (state.visible !== before.visible || state.confirmed !== before.confirmed || state.available !== before.available) {
    const value = current();
    listeners.forEach((fn) => { try { fn(value); } catch (e) { /* 一个页面出错不影响别的页面 */ } });
  }
}

function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// 能直接按回话下结论就返回结果；连不上返回 null（保留本机记的，不擦掉）
function fromCapabilities(caps) {
  const ai = (caps && caps.ai_advisor) || {};
  if (ai.enabled === true) return { visible: true, available: ai.available !== false };
  if (caps && caps.reason === 'offline') return null;
  return { visible: false, available: false };
}

function check() {
  if (!api.isLive()) return Promise.resolve({ visible: false, available: false });
  return api.advisorMode()
    // 旧后台没有这个接口（404）等：当作没对所有人开放，往下看白名单；连不上、服务出错时不下结论
    .catch((err) => (err && err.status >= 400 && err.status < 500 && err.status !== 429 ? null : Promise.reject(err)))
    .then((mode) => {
      if (mode && mode.open === true) return { visible: true, available: mode.available !== false };
      return api.advisorCapabilities({ refresh: true }).then(fromCapabilities);
    })
    .catch(() => null);
}

// 问一次后台；同时只有一个在途的请求
function refresh() {
  if (pending) return pending;
  pending = check().then((answer) => {
    pending = null;
    if (answer) {
      checkedAt = Date.now();
      failedAt = 0;
      remember(answer.visible);
      set({ visible: answer.visible, confirmed: true, available: answer.visible && answer.available });
    } else {
      failedAt = Date.now();
    }
    return current();
  });
  return pending;
}

// 页面显示时调用：这次打开还没问过、或问过超过 10 分钟才再问
function ensure() {
  const now = current();
  if (now.confirmed && Date.now() - checkedAt < RECHECK) return Promise.resolve(now);
  if (!now.confirmed && failedAt && Date.now() - failedAt < RETRY_AFTER) return Promise.resolve(now);
  return refresh();
}

// 列表行、详情页上的"AI 解读"按钮能不能显示：后台确认过、显示板块、后台能处理
function canOpen(value = current()) {
  return value.visible === true && value.confirmed === true && value.available !== false;
}

function rememberDoc(doc) {
  const store = storage();
  if (!store || !doc || !doc.id || !doc.title) return;
  try {
    store.setStorageSync(LAST_DOC_KEY, {
      id: String(doc.id), title: String(doc.title), issuer: String(doc.issuer || ''), date: String(doc.date || ''), at: Date.now(),
    });
  } catch (e) { /* 记不下来就不显示"刚才看的通知" */ }
}

function lastDoc() {
  const store = storage();
  if (!store) return null;
  try {
    const saved = store.getStorageSync(LAST_DOC_KEY);
    const at = saved && Number(saved.at);
    if (!saved || !saved.id || !saved.title || !Number.isFinite(at) || at > Date.now() || Date.now() - at > LAST_DOC_FOR) return null;
    return { id: String(saved.id), title: String(saved.title), issuer: String(saved.issuer || ''), date: String(saved.date || '') };
  } catch (e) { return null; }
}

// 注销、撤回同意以后：擦掉本机记录，各页面收起 AI
function clear() {
  remember(false);
  const store = storage();
  if (store) { try { store.removeStorageSync(LAST_DOC_KEY); } catch (e) { /* 注销时整个存储也会清空 */ } }
  checkedAt = 0;
  failedAt = 0;
  set({ visible: false, confirmed: false, available: false });
}

module.exports = { CACHE_KEY, CACHE_FOR, RECHECK, RETRY_AFTER, LAST_DOC_KEY, LAST_DOC_FOR, current, subscribe, ensure, refresh, canOpen, clear, rememberDoc, lastDoc };
