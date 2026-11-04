// 关注公众号（公众号方案 PR #287）：用户自己点了才打开公众号主页（wx.openOfficialAccountProfile，
// 基础库 3.7.10 起，微信限定"仅在点击行为时调用"），不弹窗、不挡功能、不给奖励。
// 公众号原始 ID 填在 utils/config.js 的 officialAccount 里；没填、基础库低于 3.7.10 或没有这个接口时入口不显示。
const config = require('./config.js');

const MIN_SDK = '3.7.10';
// 原始 ID 是 gh_ 加一串字母数字；写错了（如填成了公众号名称）就当没填，免得点了打不开
const USERNAME = /^gh_[0-9A-Za-z]{6,32}$/;

// 版本号逐段比较：'3.10.0' 比 '3.7.10' 新；取不到版本号按最低算
function compareVersion(a, b) {
  const x = String(a || '').split('.');
  const y = String(b || '').split('.');
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    const diff = (parseInt(x[i], 10) || 0) - (parseInt(y[i], 10) || 0);
    if (diff) return diff > 0 ? 1 : -1;
  }
  return 0;
}

function sdkVersion() {
  try {
    if (typeof wx.getAppBaseInfo === 'function') return wx.getAppBaseInfo().SDKVersion || '';
  } catch (e) { /* 取不到当作版本太低，不显示入口 */ }
  return '';
}

function account() {
  const oa = config.officialAccount || {};
  const username = typeof oa.username === 'string' ? oa.username.trim() : '';
  if (!USERNAME.test(username)) return null;
  return { username, name: typeof oa.name === 'string' ? oa.name.trim() : '' };
}

// "关注公众号"入口显示不显示
function canFollow() {
  return Boolean(account()) && typeof wx.openOfficialAccountProfile === 'function' && compareVersion(sdkVersion(), MIN_SDK) >= 0;
}

// 首页底部微信自带的关注条（<official-account>）：微信只在扫码打开等场景显示，其余场景自动不显示；
// 公众号还没注册、没填原始 ID 时不放
function showBar() {
  return Boolean(account());
}

// 只在点击事件里调用；打不开时提示在微信里搜名称
function openProfile() {
  const oa = account();
  if (!oa || typeof wx.openOfficialAccountProfile !== 'function') return;
  wx.openOfficialAccountProfile({
    username: oa.username,
    fail: () => wx.showToast({ title: oa.name ? `没能打开公众号，可以在微信里搜「${oa.name}」` : '没能打开公众号，请稍后再试', icon: 'none' }),
  });
}

module.exports = { MIN_SDK, compareVersion, account, canFollow, showBar, openProfile };
