#!/usr/bin/env node
// 默认严格提审；--mode prepare 只检查体验准备，备案办理中仍明确不能提审。
// 不输出协议正文、运营者信息或配置值。AI 内测包永远不能通过 submit。
const fs = require('node:fs');
const path = require('node:path');
const FIRST_PAGES = ['home', 'follow', 'files', 'me', 'search', 'detail', 'report', 'lists',
  'agreement', 'feedback', 'follow-new', 'my-data', 'about', 'clients'].map(p => `pages/${p}/index`);
const AI_PAGES = ['advisor', 'advisor-interpret', 'advisor-clause', 'advisor-tasks', 'complaint'].map(p => `pages/${p}/index`);

function hasPlaceholder(value) {
  if (typeof value === 'string') return value.includes('【');
  if (value && typeof value === 'object') return Object.values(value).some(hasPlaceholder);
  return false;
}
function nonempty(value) { return typeof value === 'string' && value.trim().length > 0; }
function inspect({ legal, config, app, mode = 'submit', profile = 'first-release' }) {
  if (!['submit', 'prepare'].includes(mode) || !['first-release', 'ai-internal', 'ai-release'].includes(profile)) {
    throw new Error('参数不支持');
  }
  const checks = [];
  const add = (id, name, ok, fix, warning = false) => checks.push({ id, name, state: ok ? 'pass' : warning ? 'warning' : 'fail', fix });
  const op = legal.operator || {};
  const filed = nonempty(op.icp) && !hasPlaceholder(op.icp);
  add('agreement', '协议已定稿', legal.final === true, '按已确认版本定稿协议，不能以备案办理中跳过协议检查');
  add('placeholders', '协议没有未填占位', !hasPlaceholder(legal), '补齐协议占位内容');
  add('filing', '关于页有备案号', filed, '备案办理中；号下来后填 operator.icp。体验准备通过不等于可提审', mode === 'prepare');
  add('email', '关于页有联系邮箱', nonempty(op.email) && !hasPlaceholder(op.email), '填写真实联系邮箱');
  add('cloud', '连接正式云托管', nonempty(config.cloudEnv) && nonempty(config.cloudService), '填写云托管环境和服务');
  add('local', '没有本机开发后端', !config.devBaseUrl, '清空 devBaseUrl');
  add('debug', '没有打开调试', app.debug !== true, '关闭 debug');
  const pages = Array.isArray(app.pages) ? app.pages : [];
  const expected = profile === 'first-release' ? FIRST_PAGES : FIRST_PAGES.concat(AI_PAGES);
  // 子包也会进上传包；未审阅的入口不能绕过主 pages 的范围检查。
  add('pages', '页面属于本次版本范围', pages.length === expected.length && new Set(pages).size === pages.length && expected.every(p => pages.includes(p))
    && !(app.subpackages || app.subPackages || []).length,
  '核对首发14页或AI版19页，不把批量、支付、奖励等未确认页面带入');
  const tabs = (app.tabBar && app.tabBar.list || []).map(t => t.pagePath);
  add('tabs', '底栏为首页、关注、文件、我的', JSON.stringify(tabs) === JSON.stringify(FIRST_PAGES.slice(0, 4)), '按 D-067 使用四栏');
  if (profile === 'first-release') add('ai-off', '首发包没有AI内测开关', config.aiInternal !== true, '首发不启用 AI 内测');
  else add('ai-mode', 'AI包开关与选择的版本一致', config.aiInternal === (profile === 'ai-internal'), '内测 true，正式 false；本工具不代替准入验证');
  if (profile === 'ai-release') add('ai-terms', '正式AI包使用独立新版协议', /^\d{4}-\d{2}/.test(legal.version || '') && legal.version >= '2026-11', '首发协议不含AI条款；正式AI包必须升至2026-11或后续版本，服务端同意范围也须同步');
  add('internal-submit', '不把AI内测包提交审核', !(mode === 'submit' && profile === 'ai-internal'), '内测包只能 prepare，手续与质量验证完成后另做 AI 正式包');
  for (const [id, name] of [['newDocs', '关注新文件'], ['deadline', '截止前']]) {
    add(`template-${id}`, `${name}提醒模板`, nonempty((config.templates || {})[id]), `该提醒模板未配置，对应入口保持隐藏`, true);
  }
  const failed = checks.filter(c => c.state === 'fail').length;
  return { mode, profile, ok: failed === 0, readyToSubmit: false, externalEvidence: 'not_checked',
    notice: mode === 'prepare' ? '仅体验准备检查；未核验平台后台、生产部署、真机或AI准入，不能据此提交审核' : '仅静态提审检查；还须核对备案后台、同包CI/生产/真机证据及创始人批准，本工具不授予提交权限', checks };
}
function main(args) {
  let mode = 'submit', profile = 'first-release', json = false;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--mode' && args[i + 1]) mode = args[++i];
    else if (args[i] === '--profile' && args[i + 1]) profile = args[++i];
    else if (args[i] === '--json') json = true;
    else throw new Error('用法：node tools/miniapp_presubmit_check.js [--mode prepare|submit] [--profile first-release|ai-internal|ai-release] [--json]');
  }
  const root = path.join(__dirname, '..', 'apps', 'miniapp');
  const result = inspect({legal: require(path.join(root, 'content/legal.js')), config: require(path.join(root, 'utils/config.js')),
    app: JSON.parse(fs.readFileSync(path.join(root, 'app.json'), 'utf8')), mode, profile});
  if (json) console.log(JSON.stringify(result, null, 2));
  else {
    console.log(result.notice + '\n');
    result.checks.forEach(c => console.log(`${{pass:'通过', warning:'提示', fail:'没过'}[c.state]}  ${c.name}${c.state === 'pass' ? '' : `\n      → ${c.fix}`}`));
    console.log(result.ok ? '\n本阶段检查通过' : '\n存在未通过项');
  }
  return result.ok ? 0 : 1;
}
if (require.main === module) {
  try { process.exitCode = main(process.argv.slice(2)); }
  catch (_) { console.error('无法完成检查：请核对参数和配置文件；未通过，不可提审。'); process.exitCode = 2; }
}
module.exports = { inspect };
