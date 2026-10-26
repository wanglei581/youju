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
  'agreement', 'feedback', 'follow-new', 'my-data', 'about', 'clients', 'reader', 'results'].map(p => `pages/${p}/index`);
// 底栏四格的名字，和 FIRST_PAGES 前四页一一对应（app.json 的 tabBar 和 custom-tab-bar 都是这四个字）
const TAB_NAMES = ['首页', '关注', '导出', '我的'];
const AI_PAGES = ['advisor', 'advisor-interpret', 'advisor-clause', 'advisor-tasks', 'complaint'].map(p => `pages/${p}/index`);
// 加量体验确认页（D-070 ③，10/17 创始人批准）属于批量工具，从批量工具页进入
const DATA_PAGES = ['advisor-form', 'advisor-terms', 'advisor-trial'].map(p => `pages/${p}/index`);
// 收费第一期（D-058，10/16 创始人决定接进完整版）：购买页、订单页随包，入口只在服务端在售或有订单时出现
const PAY_PAGES = ['pass', 'orders'].map(p => `pages/${p}/index`);
// AI 板块（D-075，10/19 创始人定）：板块首页和使用帮助随完整版。底栏第 5 项是板块首页，
// 自定义底栏只在 api 说 AI 对这个人开着时才画出来，关着时用户看到的还是四栏
const AI_SECTION_PAGES = ['ai', 'help'].map(p => `pages/${p}/index`);
// 过审版加 AI（D-098，10/26 创始人定"这次提审就带上 AI"）：首发 16 页加 AI 首页、解读、对照、我的记录、投诉举报、使用帮助，
// 底栏五格（最后一格 AI）；不带工具中心、批量工具、购买、订单页面
const FIRST_AI_PAGES = ['ai', 'advisor-interpret', 'advisor-clause', 'advisor-tasks', 'complaint', 'help'].map(p => `pages/${p}/index`);
// 后台第一次认 AI 条款的协议版本（backend/src/youju/ai/interpret.py 的 AI_AGREEMENT_VERSION）：
// 过审版的协议不含 AI 条款，版本号要排在它前面，只同意过过审版协议的人才不会被当成同意过 AI 条款
const SERVER_AI_TERMS_VERSION = (() => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'backend', 'src', 'youju', 'ai', 'interpret.py'), 'utf8');
  const m = /^AI_AGREEMENT_VERSION = "([^"]+)"$/m.exec(source);
  if (!m) throw new Error('读不到后台的 AI 条款版本');
  return m[1];
})();
// 过审版协议里不能出现的说法（包里没有的功能和页面、原文链接、征信感的说法）：和生成脚本用同一份
const FIRST_RELEASE_TERMS_BANNED = require('./make_legal_first_release.js').BANNED;
const PROFILES = ['first-release', 'first-release-ai', 'ai-internal', 'ai-release', 'complete-candidate', 'complete-release'];
// 每种版本该有哪些页面；tools/miniapp_pack.js 打过审版（首发 16 页）时也用这一份
function pagesFor(profile) {
  if (!PROFILES.includes(profile)) throw new Error('参数不支持');
  const complete = profile === 'complete-candidate' || profile === 'complete-release';
  if (profile === 'first-release-ai') return FIRST_PAGES.concat(FIRST_AI_PAGES);
  return profile === 'first-release' ? FIRST_PAGES.slice() : FIRST_PAGES.concat(AI_PAGES, complete ? DATA_PAGES.concat(PAY_PAGES, AI_SECTION_PAGES) : []);
}

function hasPlaceholder(value) {
  if (typeof value === 'string') return value.includes('【');
  if (value && typeof value === 'object') return Object.values(value).some(hasPlaceholder);
  return false;
}
function nonempty(value) { return typeof value === 'string' && value.trim().length > 0; }
function inspect({ legal, config, app, project, toolTerms, payTerms, mode = 'submit', profile = 'first-release' }) {
  if (!['submit', 'prepare'].includes(mode) || !PROFILES.includes(profile)) {
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
  const expected = pagesFor(profile);
  // 子包也会进上传包；未审阅的入口不能绕过主 pages 的范围检查。
  add('pages', '页面属于本次版本范围', pages.length === expected.length && new Set(pages).size === pages.length && expected.every(p => pages.includes(p))
    && !(app.subpackages || app.subPackages || []).length,
  '核对首发16页、过审版加AI 22页、AI版21页或完整版28页（均含站内阅读与结果页；过审版加AI另含AI首页、解读、对照、我的记录、投诉和帮助；完整版另含批量工具、购买、订单、AI板块和帮助）；奖励及子包不属于本次范围');
  // 底栏的页面和名字：D-067 四栏；第三格打开的是导出记录，叫"导出"（D-087，原"文件"）；完整版最后加 AI 一格（D-075）
  const tabList = (app.tabBar && app.tabBar.list) || [];
  const tabs = tabList.map(t => t.pagePath);
  const tabNames = tabList.map(t => t.text);
  // 完整版和过审版加 AI（D-098）都有 AI 一格
  const aiTab = complete || profile === 'first-release-ai';
  const expectedTabs = FIRST_PAGES.slice(0, 4).concat(aiTab ? ['pages/ai/index'] : []);
  const expectedNames = TAB_NAMES.concat(aiTab ? ['AI'] : []);
  add('tabs', aiTab ? '底栏为首页、关注、导出、我的，另有 AI 一格（AI 开着时才显示）' : '底栏为首页、关注、导出、我的',
    JSON.stringify(tabs) === JSON.stringify(expectedTabs) && JSON.stringify(tabNames) === JSON.stringify(expectedNames),
    aiTab ? '按 D-067 使用四栏、第三格叫"导出"（D-087），D-075 在最后加 pages/ai/index（自定义底栏按 api 决定画不画）' : '按 D-067 使用四栏，第三格叫"导出"（D-087）');
  if (profile === 'first-release') add('ai-off', '首发包没有AI内测开关', config.aiInternal !== true, '首发不启用 AI 内测');
  if (profile === 'first-release') {
    const text = JSON.stringify([legal.user, legal.privacy, legal.disclaimer]);
    add('first-terms', '过审版协议只写包里有的功能（不含 AI 条款），版本排在 AI 条款版本之前', !FIRST_RELEASE_TERMS_BANNED.test(text)
      && /^\d{4}-\d{2}/.test(legal.version || '') && legal.version < SERVER_AI_TERMS_VERSION,
    '用 tools/miniapp_pack.js 打包（换上 content/legal-first-release.js，D-093 ⑤）；协议只写包里有的功能（不写 AI、批量工具、加量体验、付费、投诉举报页、原文链接），版本号要早于后台的 AI 条款版本');
  }
  else add('ai-mode', 'AI包开关与选择的版本一致', config.aiInternal === (profile === 'ai-internal' || profile === 'complete-candidate'), '内测和完整候选 true，正式（AI 正式版、完整正式包）false；本工具不代替准入验证');
  if (profile === 'ai-release' || profile === 'complete-release' || profile === 'first-release-ai') add('ai-terms', '正式AI包使用独立新版协议', /^\d{4}-\d{2}/.test(legal.version || '') && legal.version >= '2026-11', '首发协议不含AI条款；正式AI包必须升至2026-11或后续版本，服务端同意范围也须同步');
  add('internal-submit', '不把内测或完整候选包提交审核', !(mode === 'submit' && ['ai-internal', 'complete-candidate'].includes(profile)), '候选只能 prepare；完整交付须另行核对准入、实际业务验收及创始人批准');
  if (complete) {
    add('tool-terms', '完整版包含独立工具使用说明', !!toolTerms && toolTerms.version === SERVER_TOOL_TERMS_VERSION
      && Array.isArray(toolTerms.paragraphs) && toolTerms.paragraphs.length >= 6 && toolTerms.paragraphs.every(nonempty)
      && !hasPlaceholder(toolTerms), '核对工具协议版本与服务器一致，提供上传、留存、删除、额度和结果边界的完整说明');
    add('tool-review', '工具说明公开发布复核', !!toolTerms && toolTerms.reviewedForRelease === true, '可体验；公开发布复核及实际准入证据仍须单独完成，提审前由创始人确认', true);
    // 付费协议没定稿时购买页的付款按钮不可用（pages/pass 用 utils/pay.js termsFinal 判断），这里只提示
    const payFinal = !!payTerms && payTerms.final === true && nonempty(payTerms.text) && !/待.*定稿|占位/.test(payTerms.text) && !hasPlaceholder(payTerms);
    add('pay-terms', '付费服务协议已定稿', payFinal, '协议定稿前（D-065：Codex、Grok 审阅，Claude 核对）购买按钮保持不可用；收费开关仍须门槛达标后由创始人批准打开', true);
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
  // --root：检查打好的包（tools/miniapp_pack.js 的输出目录），默认检查仓库里的 apps/miniapp
  let root = path.join(__dirname, '..', 'apps', 'miniapp');
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--mode' && args[i + 1]) mode = args[++i];
    else if (args[i] === '--profile' && args[i + 1]) profile = args[++i];
    else if (args[i] === '--root' && args[i + 1]) root = path.resolve(args[++i]);
    else if (args[i] === '--json') json = true;
    else throw new Error('用法：node tools/miniapp_presubmit_check.js [--mode prepare|submit] [--profile first-release|first-release-ai|ai-internal|ai-release|complete-candidate|complete-release] [--root 包目录] [--json]');
  }
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
module.exports = { inspect, pagesFor, PROFILES, FIRST_PAGES, FIRST_AI_PAGES, TAB_NAMES, SERVER_TOOL_TERMS_VERSION, SERVER_AI_TERMS_VERSION, FIRST_RELEASE_TERMS_BANNED };
