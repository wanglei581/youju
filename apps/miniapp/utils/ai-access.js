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
// 同一次打开里，页面显示（切页面、回到前台）时最多每 10 分钟再问一次后台；后台说暂时处理不了时 15 秒后就再问；
// AI 对这个人没开着时 2 分钟（后台打开以后，正在用的人下一次切页面或回到前台时看到第 5 格，重新打开马上看到；
// 首发版所有人都是这个状态，不定时去问、也不每切一次页面都发请求）；没问到（连不上）时 15 秒内不重复问
const RECHECK = 10 * 60 * 1000;
const CLOSED_RECHECK = 2 * 60 * 1000;
const RETRY_AFTER = 15000;

const listeners = new Set();
let state = null;
let pending = null;
let checkedAt = 0;
let failedAt = 0;
// clear() 一次加 1：注销以前发出去、注销以后才回来的答复不用（不能把旧账号的结果画回去）
let generation = 0;

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

// 问一次后台；同时只有一个在途的请求。fresh 为真时在途的那个作废、重新发一个：发现对不上、同意状态变了时，
// 在途的可能是变以前问的（第 12 轮）。作废的那个回来时不用它的答复，等它的页面拿到新发的那个的结果
function refresh(fresh) {
  if (fresh === true) { generation += 1; pending = null; }
  if (pending) return pending;
  const asked = generation;
  pending = check().then((answer) => {
    if (asked !== generation) return pending || current();
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

// 页面显示时调用：这次打开还没问过、或问过超过 10 分钟才再问。
// 后台说暂时处理不了时问过超过 15 秒就再问：恢复以后列表上的按钮、AI 首页不用等 10 分钟（Codex 小程序第 9 轮）；
// 没开着时问过超过 2 分钟就再问（第 11 轮）。刚才没问到（连不上）：不管以前问到过没有，15 秒内都不重复问
function ensure() {
  const now = current();
  const every = !now.visible ? CLOSED_RECHECK : now.available === false ? RETRY_AFTER : RECHECK;
  if (now.confirmed && Date.now() - checkedAt < every) return Promise.resolve(now);
  if (failedAt && Date.now() - failedAt < RETRY_AFTER) return Promise.resolve(now);
  return refresh();
}

// 页面自己问到的能力接口（AI 首页、解读页、对照页）和这里记的对不上：在途的作废，马上重新问，各页面跟着变（第 11、12 轮）。
// - 能力接口说这个人能用（enabled）：就该显示，能不能处理照它的 available，对不上就重问；
// - 说没同意协议（可能刚失效、在别处注销）而这里还显示着：重问，由开放状态接口决定收不收——
//   对所有人开放时没同意的人照样显示，白名单阶段收起；
// - 关了、不在白名单（off）由页面自己重问；连不上（offline）不下结论
function reconcile(caps) {
  const ai = (caps && caps.ai_advisor) || {};
  const now = current();
  if (ai.enabled === true) {
    if (now.confirmed && now.visible && (now.available !== false) === (ai.available !== false)) return;
    refresh(true);
    return;
  }
  if (caps && caps.reason === 'consent' && now.visible) refresh(true);
}

// 协议同意状态变了（刚同意、发现已失效或在别处注销）：在途的查询作废、能力接口的缓存不用，马上重新问；
// 不先收起，问到了再按结果变（白名单里的人刚同意，第 5 格马上出来，第 11 轮）
function recheck() {
  api.forgetCapabilities();
  return refresh(true);
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

// 刚才看的那份不能解读了（下架、失去资格）：不再显示
function forgetDoc() {
  const store = storage();
  if (store) { try { store.removeStorageSync(LAST_DOC_KEY); } catch (e) { /* 下次照样会重新核对 */ } }
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

// 注销、撤回同意以后：擦掉本机记录，各页面收起 AI；在途的查询作废
function clear() {
  generation += 1;
  pending = null;
  api.forgetCapabilities();
  remember(false);
  const store = storage();
  if (store) { try { store.removeStorageSync(LAST_DOC_KEY); } catch (e) { /* 注销时整个存储也会清空 */ } }
  checkedAt = 0;
  failedAt = 0;
  set({ visible: false, confirmed: false, available: false });
}

module.exports = {
  CACHE_KEY, CACHE_FOR, RECHECK, CLOSED_RECHECK, RETRY_AFTER, LAST_DOC_KEY, LAST_DOC_FOR,
  current, subscribe, ensure, refresh, reconcile, recheck, canOpen, clear, rememberDoc, forgetDoc, lastDoc,
};
