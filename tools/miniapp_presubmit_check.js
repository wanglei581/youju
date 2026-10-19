#!/usr/bin/env node
// 默认严格提审；--mode prepare 只检查体验准备，备案办理中仍明确不能提审。
// 完整版本（D-068）提审用 --profile complete-release：页面范围同完整候选，但必须关掉 AI 内测开关、协议升到 2026-11 以后。
// 不输出协议正文、运营者信息或配置值。AI 内测包永远不能通过 submit。
// 发布身份只对照本产品的 tools/release/release-identity.json。不读环境变量，也不核验微信或云托管的线上授权。
const fs = require('node:fs');
const path = require('node:path');
const RELEASE_IDENTITY = (() => {
  const raw = JSON.parse(fs.readFileSync(path.join(__dirname, 'release', 'release-identity.json'), 'utf8'));
  const identity = { appid: raw.appid, cloudEnv: raw.cloudEnv, cloudService: raw.cloudService };
  if (Object.values(identity).some(value => typeof value !== 'string' || value.trim() === '' || value !== value.trim())) {
    throw new Error('发布身份无效');
  }
  return Object.freeze(identity);
})();
// 工具使用说明的版本要和后台一致（backend/src/youju/advisor_tasks.py 的 ADVISOR_AGREEMENT_VERSION）：
// 不一致时用户点了同意也会被后台拒绝。直接读后台的常量，升版本时不用再改这里
const SERVER_TOOL_TERMS_VERSION = (() => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'backend', 'src', 'youju', 'advisor_tasks.py'), 'utf8');
  const m = /^ADVISOR_AGREEMENT_VERSION = "([^"]+)"$/m.exec(source);
  if (!m) throw new Error('读不到后台的工具使用说明版本');
  return m[1];
})();
const FIRST_PAGES = ['home', 'follow', 'files', 'me', 'search', 'detail', 'report', 'lists',
  'agreement', 'feedback', 'follow-new', 'my-data', 'about', 'clients'].map(p => `pages/${p}/index`);
const AI_PAGES = ['advisor', 'advisor-interpret', 'advisor-clause', 'advisor-tasks', 'complaint'].map(p => `pages/${p}/index`);
// 加量体验确认页（D-070 ③，10/17 创始人批准）属于批量工具，从批量工具页进入
const DATA_PAGES = ['advisor-form', 'advisor-terms', 'advisor-trial'].map(p => `pages/${p}/index`);
// 收费第一期（D-058，10/16 创始人决定接进完整版）：购买页、订单页随包，入口只在服务端在售或有订单时出现
const PAY_PAGES = ['pass', 'orders'].map(p => `pages/${p}/index`);
// 公众号"本期清单"页（公众号方案 PR #287 第 3 件事，10/18 创始人同意）：从公众号周报和"我的 → 本周周报"进入
const GZH_PAGES = ['digest'].map(p => `pages/${p}/index`);

function hasPlaceholder(value) {
  if (typeof value === 'string') return value.includes('【');
  if (value && typeof value === 'object') return Object.values(value).some(hasPlaceholder);
  return false;
}
function nonempty(value) { return typeof value === 'string' && value.trim().length > 0; }
function inspect({ legal, config, app, project, toolTerms, payTerms, mode = 'submit', profile = 'first-release' }) {
  if (!['submit', 'prepare'].includes(mode) || !['first-release', 'ai-internal', 'ai-release', 'complete-candidate', 'complete-release'].includes(profile)) {
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
  const releaseIdentityOk = !!project && project.appid === RELEASE_IDENTITY.appid
    && config.cloudEnv === RELEASE_IDENTITY.cloudEnv && config.cloudService === RELEASE_IDENTITY.cloudService;
  add('cloud', '连接正式云托管', config.cloudEnv === RELEASE_IDENTITY.cloudEnv && config.cloudService === RELEASE_IDENTITY.cloudService, '云环境或云服务缺失，或不是本产品已确认的正式云托管');
  add('release-identity', '发布身份与本产品正式包一致', releaseIdentityOk, 'AppID、云环境或云服务缺失，或与本产品已确认的正式发布身份不一致。本检查只做仓库内静态对照，不验证线上授权');
  add('local', '没有本机开发后端', !config.devBaseUrl, '清空 devBaseUrl');
  add('debug', '没有打开调试', app.debug !== true, '关闭 debug');
  const pages = Array.isArray(app.pages) ? app.pages : [];
  const complete = profile === 'complete-candidate' || profile === 'complete-release';
  const expected = profile === 'first-release' ? FIRST_PAGES : FIRST_PAGES.concat(AI_PAGES, complete ? DATA_PAGES.concat(PAY_PAGES, GZH_PAGES) : []);
  // 子包也会进上传包；未审阅的入口不能绕过主 pages 的范围检查。
  add('pages', '页面属于本次版本范围', pages.length === expected.length && new Set(pages).size === pages.length && expected.every(p => pages.includes(p))
    && !(app.subpackages || app.subPackages || []).length,
  '核对历史首发14页、AI版19页或完整版25页（含加量体验确认页、购买页、订单页、本期清单页）；奖励及子包不属于本次范围');
  const tabs = (app.tabBar && app.tabBar.list || []).map(t => t.pagePath);
  add('tabs', '底栏为首页、关注、文件、我的', JSON.stringify(tabs) === JSON.stringify(FIRST_PAGES.slice(0, 4)), '按 D-067 使用四栏');
  if (profile === 'first-release') add('ai-off', '首发包没有AI内测开关', config.aiInternal !== true, '首发不启用 AI 内测');
  else add('ai-mode', 'AI包开关与选择的版本一致', config.aiInternal === (profile === 'ai-internal' || profile === 'complete-candidate'), '内测和完整候选 true，正式（AI 正式版、完整正式包）false；本工具不代替准入验证');
  if (profile === 'ai-release' || profile === 'complete-release') add('ai-terms', '正式AI包使用独立新版协议', /^\d{4}-\d{2}/.test(legal.version || '') && legal.version >= '2026-11', '首发协议不含AI条款；正式AI包必须升至2026-11或后续版本，服务端同意范围也须同步');
  add('internal-submit', '不把内测或完整候选包提交审核', !(mode === 'submit' && ['ai-internal', 'complete-candidate'].includes(profile)), '候选只能 prepare；完整交付须另行核对准入、实际业务验收及创始人批准');
  if (complete) {
    add('tool-terms', '完整版包含独立工具使用说明', !!toolTerms && toolTerms.version === SERVER_TOOL_TERMS_VERSION
      && Array.isArray(toolTerms.paragraphs) && toolTerms.paragraphs.length >= 6 && toolTerms.paragraphs.every(nonempty)
      && !hasPlaceholder(toolTerms), '核对工具协议版本与服务器一致，提供上传、留存、删除、额度和结果边界的完整说明');
    add('tool-review', '工具说明公开发布复核', !!toolTerms && toolTerms.reviewedForRelease === true, '可体验；公开发布复核及实际准入证据仍须单独完成，提审前由创始人确认', true);
    // 付费协议没定稿时购买页的付款按钮不可用（pages/pass 用 utils/pay.js termsFinal 判断），这里只提示
    const payFinal = !!payTerms && payTerms.final === true && nonempty(payTerms.text) && !/待.*定稿|占位/.test(payTerms.text) && !hasPlaceholder(payTerms);
    add('pay-terms', '付费服务协议已定稿', payFinal, '律师定稿前购买按钮保持不可用；收费开关仍须门槛达标后由创始人批准打开', true);
  }
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
    else throw new Error('用法：node tools/miniapp_presubmit_check.js [--mode prepare|submit] [--profile first-release|ai-internal|ai-release|complete-candidate|complete-release] [--json]');
  }
  const root = path.join(__dirname, '..', 'apps', 'miniapp');
  const result = inspect({legal: require(path.join(root, 'content/legal.js')), config: require(path.join(root, 'utils/config.js')),
    app: JSON.parse(fs.readFileSync(path.join(root, 'app.json'), 'utf8')),
    project: JSON.parse(fs.readFileSync(path.join(root, 'project.config.json'), 'utf8')),
    toolTerms: profile.startsWith('complete-') ? require(path.join(root, 'content/advisor-terms.js')) : undefined,
    payTerms: profile.startsWith('complete-') ? require(path.join(root, 'content/pay-terms.js')) : undefined, mode, profile});
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
module.exports = { inspect, SERVER_TOOL_TERMS_VERSION };
