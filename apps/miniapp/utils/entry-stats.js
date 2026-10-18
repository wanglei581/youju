// 来源统计（推广与获客方案第 2 件，创始人 10/18 同意；不给用户看）：用户从哪个入口打开小程序。
// - 冷启动时只在手机上读出：场景值（微信给的打开方式，如 1008 群聊卡片、1047 扫小程序码、1053 搜一搜）、
//   链接或小程序码里带的来源编号 from、第一个打开的页面路径。别的参数（文件编号、检索词、企业名称）不读也不发，
//   也不发任何用户编号。后台只按天累加次数（backend/src/youju/entry_stats.py），保存 400 天。
// - 来源编号只认渠道或活动级的短编号：小写字母、数字、-、_，最长 24 位（如 gzh1103），不给每位顾问单独编号；
//   不合格的当作没带。小程序码（getUnlimited 生成的）把参数放在 query.scene 里，也从那里认 from。
// - 打开 3 秒后发（首屏一般 1 秒内就出来），3 秒内就切到后台的，切走时发；只发一次，不重试：
//   断网、超时就算了，不提示。发给常驻的 admin 服务：它不认身份，也不会为了统计叫醒按需启动的接口服务。
// - 只统计正式版：开发版、体验版不发，团队测试不混进来；连本机后端开发（devBaseUrl）时也发，方便自测。
// - 只记冷启动：小程序还在后台时从卡片再进来（热启动）不计。
const config = require('./config.js');

const PATH = '/api/v1/entry-stats';
const SERVICE = 'admin';
const DELAY = 3000;
const TIMEOUT = 10000;
const CODE = /^[a-z0-9_-]{1,24}$/;
const PAGE = /^pages\/[a-z0-9-]{1,40}\/index$/;

let captured = false;
let pending = null;
let timer = null;

function decode(value) {
  try { return decodeURIComponent(String(value)); } catch (e) { return String(value); }
}

// 来源编号：query.from；没有时看小程序码的 query.scene（形如 from=gzh1103，可能还没解码）
function codeOf(query) {
  const q = query || {};
  let raw = q.from;
  if ((raw === undefined || raw === null || raw === '') && q.scene) {
    for (const part of decode(q.scene).split('&')) {
      const at = part.indexOf('=');
      if (at > 0 && part.slice(0, at) === 'from') { raw = part.slice(at + 1); break; }
    }
  }
  if (raw === undefined || raw === null) return '';
  const code = decode(raw).trim().toLowerCase();
  return CODE.test(code) ? code : '';
}

function pageOf(path) {
  const page = String(path || '').replace(/^\//, '');
  return PAGE.test(page) ? page : '';
}

function sceneOf(scene) {
  const n = Number(scene);
  return Number.isInteger(n) && n >= 1000 && n <= 9999 ? n : 0;
}

function released() {
  try { return wx.getAccountInfoSync().miniProgram.envVersion === 'release'; } catch (e) { return false; }
}

function enabled() {
  if (config.cloudEnv) return Boolean(wx.cloud && wx.cloud.callContainer) && released();
  return Boolean(config.devBaseUrl);
}

// App.onLaunch 调用：只在手机上记下这次怎么打开的，过 3 秒再发
function capture(options) {
  if (captured) return;
  captured = true;
  try {
    if (!enabled()) return;
    const launch = options || (wx.getLaunchOptionsSync ? wx.getLaunchOptionsSync() : null) || {};
    pending = { scene: sceneOf(launch.scene), from: codeOf(launch.query), page: pageOf(launch.path) };
    timer = setTimeout(send, DELAY);
  } catch (e) {
    pending = null;
  }
}

function send() {
  clearTimeout(timer);
  timer = null;
  const data = pending;
  pending = null;
  if (!data) return;
  const ignore = () => {};
  const header = { 'content-type': 'application/json' };
  try {
    if (config.cloudEnv) {
      wx.cloud.callContainer({
        config: { env: config.cloudEnv },
        path: PATH,
        method: 'POST',
        header: Object.assign({ 'X-WX-SERVICE': SERVICE }, header),
        data,
        timeout: TIMEOUT,
        success: ignore,
        fail: ignore,
      });
    } else {
      wx.request({ url: config.devBaseUrl + PATH, method: 'POST', header, data, timeout: TIMEOUT, success: ignore, fail: ignore });
    }
  } catch (e) {
    // 发不出去就算了
  }
}

// App.onHide 调用：3 秒内就切到后台的，现在发
function flush() {
  if (pending) send();
}

module.exports = { capture, flush, codeOf, pageOf, sceneOf };
