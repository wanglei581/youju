// 关注、订阅提醒、纠错之前，先确认用户已同意用户协议和隐私保护指引（服务端记录，本机缓存版本号）。
// 协议还是草案（legal.final 为 false）时不开放同意，这些功能的入口都不显示（D-056）。
const legal = require('../content/legal.js');
const config = require('./config.js');
const api = require('./api.js');
const exportsLog = require('./exports.js');
const { removeFiles } = require('./exporter.js');
const aiAccess = require('./ai-access.js');

// AI 内测包（config.aiInternal 为 true，只上传体验版、给白名单内测，D-062）：协议还是草案也开放同意和登录，
// 好让内测成员同意后试用 AI 页面。首发版和 AI 正式版都是 false，只看 legal.final（D-056、D-066）。
function aiInternal() {
  return config.aiInternal === true;
}

// 协议已定稿（或 AI 内测包）、连着真实接口时，才开放同意和需要同意的功能
function consentOpen() {
  return Boolean(legal.final || aiInternal()) && api.isLive();
}

// 需要同意协议的功能（登录、关注、订阅提醒、纠错、申请覆盖）这个版本显不显示入口（D-056）：
// 协议定稿前不显示，定稿后照旧（没连接口时仍显示演示数据）。只看代码包里的 legal.final（AI 内测包例外），不是服务端开关
function loginOpen() {
  return legal.final === true || aiInternal();
}

// 服务端记的版本不早于本机代码包里的协议版本，就算已同意：版本号按时间可排序（2026-10 < 2026-11）。
// 用户在另一台手机的新版小程序里同意过更新的版本时，这里不再请他同意——不然会把服务端记录改回旧版本，
// 新功能失效、两台手机来回请用户同意（Codex 审查）
function covers(version) {
  return typeof version === 'string' && /^\d{4}-\d{2}/.test(version) && version >= legal.version;
}

// 同意没成功：小程序带的协议版本后台已不再认（422，小程序太旧）时弹窗请用户更新，其余照旧提示
function agreeFailed(err) {
  if (err && err.status === 422) {
    wx.showModal({
      title: '需要更新小程序',
      content: err.message || '协议已更新，请关闭小程序后重新打开，更新到新版本后再同意',
      showCancel: false,
      confirmText: '知道了',
    });
    return;
  }
  wx.showToast({ title: (err && err.message) || '网络不太好，请稍后再试', icon: 'none' });
}

// 订阅消息模板申请下来之前（utils/config.js 的 templates 为空）没有微信提醒，提示里不写"微信提醒"
// （10/15 模拟用户走查：同意弹窗、同意成功的提示、我的数据、注销都还写着）
function remindAny() {
  const templates = config.templates || {};
  return Boolean(templates.newDocs || templates.deadline);
}

function featureNames() {
  return remindAny() ? '关注、微信提醒和纠错' : '关注和纠错';
}

function agreedHere() {
  try { return wx.getStorageSync(api.AGREED_KEY) === legal.version; } catch (e) { return false; }
}

function remember() {
  try { wx.setStorageSync(api.AGREED_KEY, legal.version); } catch (e) { /* 缓存写不进去下次再问 */ }
}

function forget() {
  api.forgetAgreed();
}

// 本机没有同意记录时（如换了设备、还没进过"我的"），每次启动向服务端核对一次：
// 同意的是当前版本就补上本机记录。查询 /me 不会建用户记录。返回 true 表示已同意当前版本。
let pending = null;
let notAgreed = false; // 本次启动已核对过：服务端没有当前版本的同意，不再重复查
let outdated = false; // 服务端记录的是旧版本的同意：协议更新了，要重新阅读并同意
let epoch = 0; // 本机每次同意或注销加 1：在这之前发出的 /me 查询结果已过时，不再覆盖本机状态

function consentEpoch() {
  return epoch;
}

// 按服务端 /me 返回的同意记录更新本机状态（"我的"和 syncConsent 共用，保证两处一致）：
// 同意的是当前版本就记下；旧版本标记"协议已更新"；没有记录或旧版本都清掉本机记录。返回是否已同意当前版本。
// startedAt 是发出查询时的 consentEpoch()：之后本机又同意或注销过，这个结果已过时，不改状态
function applyServer(agreed, startedAt) {
  if (startedAt !== epoch) return agreedHere();
  const before = agreedHere();
  // valid 为假：服务端已不认这份同意（仍认的最早版本调高了）——即使版本号和本机的一样也要重新同意，
  // 太旧的小程序同意时会看到"需要更新小程序"（Codex 审查）。旧后台不带 valid 时只看版本号
  const ok = !!agreed && agreed.valid !== false && covers(agreed.version);
  if (ok) remember();
  else forget();
  outdated = !!agreed && !ok;
  notAgreed = !ok;
  // 和本机记的不一样了（在别的手机同意过、同意已失效、在别处注销）：AI 板块马上重新问（D-075，第 11 轮）
  if (ok !== before) aiAccess.recheck();
  return ok;
}

function syncConsent() {
  if (!consentOpen()) return Promise.resolve(false);
  if (agreedHere()) return Promise.resolve(true);
  if (notAgreed) return Promise.resolve(false);
  if (!pending) {
    const started = epoch;
    pending = api.me()
      .then((res) => applyServer(res.agreed, started))
      .catch(() => false)
      .then((ok) => { pending = null; return ok; });
  }
  return pending;
}

// 打开小程序后台填写的《用户隐私保护指引》（微信官方页面，基础库 2.32.3 起）。
// 协议定稿前（D-056）"我的"和"关于有据"用它代替草案页面；打不开或基础库太低时轻提示
function openPrivacy() {
  const fail = () => wx.showToast({ title: '暂时打不开，请稍后再试', icon: 'none' });
  if (typeof wx.openPrivacyContract !== 'function') { fail(); return; }
  wx.openPrivacyContract({ fail });
}

// 返回 true 表示已同意，可以继续。协议定稿前这些功能的入口都不显示（D-056），走到这里只是兜底。
// purpose 为 'ai'：从 AI 板块来的（AI 首页、解读页、对照页），见 askConsent
function ensureConsent(options) {
  const purpose = options && options.purpose;
  if (!loginOpen()) {
    wx.showToast({ title: '这个功能暂未开放', icon: 'none' });
    return Promise.resolve(false);
  }
  if (!api.isLive()) {
    wx.showToast({ title: '演示数据不能使用这项功能', icon: 'none' });
    return Promise.resolve(false);
  }
  if (agreedHere()) return Promise.resolve(true);
  return syncConsent().then((synced) => (synced ? true : askConsent(purpose)));
}

// 读全文并同意的页面：依次显示两份文件全文，同意按钮在最后
const CONSENT_PAGE = '/pages/agreement/index?consent=1';
// 从 AI 板块来的：同意以后的提示不写"关注和纠错"
const AI_CONSENT_PAGE = `${CONSENT_PAGE}&for=ai`;

function askConsent(purpose) {
  const ai = purpose === 'ai';
  // 协议更新后、或从 AI 板块来（两份文件里有 AI 功能的约定）：只能先读全文，在全文最后同意，
  // 弹窗写明是为了用什么（Codex 小程序第 13 轮）
  if (outdated || ai) {
    const what = ai ? 'AI 解读和条款对照' : featureNames();
    wx.showModal({
      title: outdated ? '协议已更新' : '请先阅读并同意',
      content: outdated
        ? `《用户协议》和《隐私保护指引》已更新。请先阅读更新后的全文，在全文最后同意后，再使用${what}。`
        : `使用${what}前，请先阅读《用户协议》和《隐私保护指引》全文（里面有 AI 功能的约定），在全文最后同意。`,
      confirmText: '阅读全文',
      cancelText: '暂不',
      success: (res) => { if (res.confirm) wx.navigateTo({ url: ai ? AI_CONSENT_PAGE : CONSENT_PAGE }); },
    });
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    wx.showModal({
      title: '请先阅读并同意',
      content: `使用${featureNames()}前，请阅读并同意《用户协议》和《隐私保护指引》。我们只保存你的微信用户标识和你主动设置、提交的内容，可随时在\u201c我的\u201d里注销并删除。`,
      confirmText: '同意',
      cancelText: '先看协议',
      success: (res) => {
        if (!res.confirm) {
          wx.navigateTo({ url: CONSENT_PAGE });
          resolve(false);
          return;
        }
        api.agree(legal.version)
          .then(() => { markAgreed(); resolve(true); })
          .catch((err) => { agreeFailed(err); resolve(false); });
      },
      fail: () => resolve(false),
    });
  });
}

// 服务端记录同意之后调用：记下本机记录，清掉"协议已更新"
function markAgreed() {
  remember();
  outdated = false;
  notAgreed = false;
  epoch += 1;
  // 白名单阶段同意以后才看得到 AI 板块：马上重新问，不等下一次（D-075，第 11 轮）
  aiAccess.recheck();
}

// 注销：撤回同意，删除服务端的全部个人信息。用户确认并删除成功时返回 true
function withdraw() {
  return new Promise((resolve) => {
    wx.showModal({
      title: '注销并删除数据',
      content: `将撤回你对用户协议和隐私保护指引的同意，并删除你在有据的关注、${remindAny() ? '微信提醒、' : ''}纠错记录、工具输入、任务与结果，以及本机的导出记录和文件，删除后不能恢复。`,
      confirmText: '注销',
      confirmColor: '#E5484D',
      success: (res) => {
        if (!res.confirm) { resolve(false); return; }
        api.deleteMe()
          .then(() => {
            exportsLog.clear();
            // 文件缺失或无法删除不影响注销；清空本机缓存也涵盖后续新增的个人数据。
            try { removeFiles(); } catch (e) { /* 忽略文件清理失败 */ }
            try { wx.removeStorageSync('youjuDataTaskKeysV1'); } catch (e) { /* 下方清空存储再次清理 */ }
            try { wx.clearStorageSync(); } catch (e) { forget(); }
            // AI 板块记的结果（本机和这次打开里的）一并擦掉，底栏第 5 格随即收起（D-075）；
            // 再马上问一次：对所有人开放时，没同意的人也看得到板块（第 11 轮）
            aiAccess.clear();
            aiAccess.refresh();
            outdated = false;
            notAgreed = true;
            epoch += 1;
            wx.showToast({ title: '已注销，数据已删除', icon: 'none' });
            resolve(true);
          })
          .catch((err) => {
            wx.showToast({ title: err.message || '注销失败，请稍后再试', icon: 'none' });
            resolve(false);
          });
      },
      fail: () => resolve(false),
    });
  });
}

module.exports = {
  ensureConsent, syncConsent, applyServer, consentEpoch, consentOpen, loginOpen, openPrivacy, agreedHere, remember, forget, withdraw, markAgreed, CONSENT_PAGE, AI_CONSENT_PAGE,
  covers, agreeFailed, remindAny, featureNames,
};
