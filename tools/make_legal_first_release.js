#!/usr/bin/env node
// 由 content/legal.js（2026-11，含 AI 条款）生成过审版用的协议草稿 content/legal-first-release.js（D-093 ⑤）。
// 过审版（首发 16 页，D-091）里没有 AI、批量工具、加量体验、购买和订单页面，协议只写包里真有的功能
// （微信"所列即所用"；agy 10/25 合规审）。每一处改动都写在下面，按原文逐字查找、只许命中一次；
// 原文改过、对不上时直接报错，不会悄悄漏改。定稿后 legal-first-release.js 就是固定文本，不再覆盖（--force 才覆盖）。
// 用法：node tools/make_legal_first_release.js [--force]
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', 'apps', 'miniapp', 'content');
const TARGET = path.join(ROOT, 'legal-first-release.js');
const VERSION = '2026-10.2';
// 过审版协议里不能出现的说法（发布检查 tools/miniapp_presubmit_check.js 用同一份）
const BANNED = /AI|人工智能|生成内容|大模型|模型|投诉举报|原文链接|使用帮助|我的记录|批量工具|加量体验|工具使用说明|订单|购买页|统一社会信用代码|核对企业主体/;

function build() {
  delete require.cache[require.resolve(path.join(ROOT, 'legal.js'))];
  const legal = JSON.parse(JSON.stringify(require(path.join(ROOT, 'legal.js'))));
  const sec = (doc, head) => {
    const found = doc.sections.find((s) => s.h.startsWith(head));
    if (!found) throw new Error(`没有找到 ${head}`);
    return found;
  };
  // 在一节的段落里把 find 换成 replace（null 表示删掉整段）；必须恰好命中一段
  const edit = (section, find, replace, where) => {
    const hits = section.p.map((p, i) => [p, i]).filter(([p]) => p.includes(find));
    if (hits.length !== 1) throw new Error(`${where}：“${find.slice(0, 24)}…”命中 ${hits.length} 处`);
    const [p, i] = hits[0];
    if (replace === null) section.p.splice(i, 1);
    else section.p[i] = p.replace(find, replace);
  };
  const U = legal.user;
  const P = legal.privacy;

  // ================= 用户协议 =================
  edit(sec(U, '一、'), '公开信息的检索和查看免费。AI 功能的范围、标识和使用要求见第八条。', '公开信息的检索和查看免费。', '一·4');
  // 过审版不给原文链接（D-089、D-091），改成注明出处
  edit(sec(U, '二、'), '1. 来源与原文链接：本小程序所收录、展示的通知与政策均来源于政府公开发布渠道，我们在条目中均附有或提供官方原文链接。',
    '1. 来源与出处：本小程序所收录、展示的通知与政策均来源于政府公开发布渠道，每条注明发文机关、发布日期和来源网站。', '二·1');
  edit(sec(U, '二、'), '我们会合理核对收录的信息并提供官方原文链接', '我们会合理核对收录的信息并注明出处', '二·3');
  edit(sec(U, '三、'), '并删除您的关注、微信提醒授权、纠错记录、工具任务及结果和加量体验记录；', '并删除您的关注、微信提醒授权和纠错记录；', '三·3');
  // 查名单不像征信（10/23 退回原因之一；agy 10/25）；只收企业全称（D-091 ③）
  edit(sec(U, '四、'), '3. 企业名称查询限制：“按企业名称查名单”功能仅用于用户根据公开公示名单核对企业主体信息，您不得输入侵犯个人隐私、国家秘密或其他非法违规的关键词。',
    '3. 查名单：“按企业名称查名单”只用于在政府公开发布的认定、立项、公示、入库、备案类名单中查找某个企业或机构的名称，不提供征信、信用评价或资质核验服务；您不得输入个人姓名、国家秘密或其他违法违规的内容。', '四·3');
  edit(sec(U, '四、'), '5. 上传名单：', null, '四·5');
  U.sections = U.sections.filter((s) => !s.h.startsWith('八、AI'));
  sec(U, '九、').h = sec(U, '九、').h.replace('九、', '八、');
  U.keyPoints = U.keyPoints.filter((k) => !k.includes('AI') && !k.includes('四·5'));
  U.keyParagraphs = U.keyParagraphs.filter((k) => !['5. 上传名单', '2. 标识方法和样式', '3. 您的标识义务', '5. 请您仔细阅读并理解本条'].includes(k));

  // ================= 隐私保护指引 =================
  const one = sec(P, '一、');
  edit(one, '关注、微信提醒、纠错、批量工具、AI 功能和加量体验需要登录', '关注、微信提醒和纠错需要登录', '一·1');
  edit(one, '联系客服也不需要登录（见第 16 项）', '联系客服也不需要登录（见第 13 项）', '一·1');
  edit(one, '；使用批量工具前，您同意的《工具使用说明》的版本、同意时间。目的：确认您已同意；指引、协议或《工具使用说明》更新后，请您重新阅读并同意。',
    '。目的：确认您已同意；指引或协议更新后，请您重新阅读并同意。', '一·3');
  edit(one, '以及从文件详情或 AI 结果页进入时自动带上的那份公开文件的编号', '以及从文件详情页进入时自动带上的那份公开文件的编号', '一·6');
  // 投诉举报页随 AI 才有；购买页、订单页随付费才有（首发包里都没有）
  edit(one, '同一张表单还用于“申请覆盖”（告诉我们想增加的地区和栏目）、投诉举报，以及付费开放后的支付和订单问题（从购买页、订单页进来时，内容前可能自动带上订单号），这些内容都按本项处理。目的：对照原文核实并修正数据；评估是否增加信息来源；处理您的投诉、建议和订单问题。',
    '同一张表单还用于“申请覆盖”（告诉我们想增加的地区和栏目），也按本项处理。目的：对照原文核实并修正数据；评估是否增加信息来源；处理您的建议。', '一·6');
  edit(one, '7. 按企业名称查名单时输入的企业名称或统一社会信用代码：只用于在公开名单中比对，不保存到您的账号下。',
    '7. 按企业名称查名单时输入的企业全称：只用于在政府公开名单中查找，不保存到您的账号下。', '一·7');
  edit(one, '管理后台的操作另记操作内容；您提交、取消、删除批量工具或 AI 任务时另记操作类型和任务编号，开始加量体验时另记操作类型。', '管理后台的操作另记操作内容。', '一·8');
  edit(one, '用于保障运行安全、排查故障和追查网络安全事件，不用于判断能否参加加量体验等活动；', '用于保障运行安全、排查故障和追查网络安全事件；', '一·8');
  edit(one, '关于页的联系邮箱；投诉举报页的联系邮箱（开放 AI 功能后才有）；', '关于页的联系邮箱；', '一·9');
  edit(one, '我们不在服务器上保存。批量工具（开放后登录使用）在服务器上生成的结果文件，见第 13 项。', '我们不在服务器上保存。', '一·10');
  edit(one, '13. 批量工具（批量名单比对、客户相关通知表，开放后登录使用）', null, '一·13');
  edit(one, '14. AI 功能（登录后使用）', null, '一·14');
  edit(one, '15. 加量体验（开放后登录使用）', null, '一·15');
  edit(one, '16. 联系客服（不需要登录）', '13. 联系客服（不需要登录）', '一·16');
  // 首发包里只有"我的"页有联系客服（不带通知卡片）；没有使用帮助页，打不开客服时只给邮箱
  edit(one, '· 按钮会把一个排查用的标识交给微信：您从哪一页进入；在 AI 相关页面上，还可能有通知编号，以及 AI 暂停、暂时用不了或出错的原因代码。标识里没有您填写的企业信息、客户名单、AI 结果和任务编号，我们的服务器不读取、不保存它。在 AI 解读、条款对照页上点时，会话里会备好这份通知的卡片（标题和页面路径），发不发由您决定。',
    '· 按钮会把一个排查用的标识交给微信：您从哪一页进入。标识里没有您填写的信息，我们的服务器不读取、不保存它。', '一·16 标识');
  edit(one, '会改为引导您看使用帮助或发邮件。', '会改为提示您发邮件。', '一·16 打不开客服');
  const three = sec(P, '三、');
  edit(three, '· 批量工具：', null, '三·2 批量工具');
  edit(three, '· AI 任务：', null, '三·2 AI');
  edit(three, '· 加量体验记录：', null, '三·2 加量体验');
  edit(three, '纠错内容、批量工具和 AI 的任务及结果、加量体验记录；访问日志依法保存', '纠错内容；访问日志依法保存', '三·2 注销');
  const four = sec(P, '四、');
  edit(four, '（含申请覆盖、投诉举报、支付和订单问题，已隐去手机号等）', '（含申请覆盖，已隐去手机号等）', '四·2 ③');
  edit(four, '（见第一部分第 16 项）', '（见第一部分第 13 项）', '四·2 ④');
  edit(four, '· AI 模型调用：', null, '四·2 AI');
  const five = sec(P, '五、');
  edit(five, '（账号记录、同意记录、关注、微信提醒授权、纠错、工具任务记录、加量体验的开始和结束时间）', '（账号记录、同意记录、关注、微信提醒授权、纠错）', '五·1');
  edit(five, '（如账号建立时间、《工具使用说明》的同意记录）', '（如账号建立时间）', '五·1');
  edit(five, '随时删除关注和纠错记录，取消还没用上的提醒授权；在“我的记录”里删除批量工具和 AI 的任务及结果（处理中的要等处理结束）。', '随时删除关注和纠错记录，取消还没用上的提醒授权。', '五·3');
  edit(five, '纠错内容、批量工具和 AI 的任务及结果、加量体验记录（', '纠错内容（', '五·4');
  edit(five, '依法须留存的信息（访问日志，以及启用付费后的订单和发票记录）按规定期限保存', '依法须留存的信息（访问日志）按规定期限保存', '五·4');
  edit(sec(P, '六、'), '（关注、微信提醒、纠错、批量工具、AI 功能、加量体验）', '（关注、微信提醒、纠错）', '六');
  edit(sec(P, '七、'), '（如关注、纠错、批量工具、AI 功能、加量体验）', '（如关注、纠错）', '七');
  P.keyPoints = P.keyPoints.filter((k) => !k.includes('AI'));
  P.keyParagraphs = P.keyParagraphs.filter((k) => !['14. AI 功能', '· AI 模型调用'].includes(k));

  legal.version = VERSION;
  legal.final = false;
  legal.status = '草稿（D-093 ⑤ 过审版协议，待创始人过目后定稿）';
  const left = JSON.stringify([legal.user, legal.privacy, legal.disclaimer, legal.status]).match(new RegExp(BANNED.source, 'g'));
  if (left) throw new Error(`还有没去掉的：${[...new Set(left)].join('、')}`);
  return legal;
}

const HEADER = `// 过审版（首发 16 页，D-091）用的用户协议与隐私保护指引：在 content/legal.js（2026-11）的基础上，去掉包里没有的
// AI、批量工具、加量体验、付费相关条款（D-093 ⑤，10/25 创始人定；agy 10/25 合规审），查名单的说法按 D-091 ③ 改。
// 由 tools/make_legal_first_release.js 生成，逐处改动写在那个脚本里；定稿后这份就是固定文本，不再重新生成。
// 只用于 tools/miniapp_pack.js 打的首发包（打包时替换 content/legal.js）；完整版仍用 content/legal.js。
// 版本号排在 2026-11 前面（字符串比较：2026-10 < 2026-10.2 < 2026-11）：只同意过这一版的人用不了 AI 和批量工具
// （后台 AI_AGREEMENT_VERSION、TOOLS_AGREEMENT_VERSION 都要 2026-11），开放时请他们重新同意 2026-11 或之后的版本。
// 上线这一版时后台设 YOUJU_AGREEMENT_MIN_VERSION=2026-10.2（当前版本仍是 2026-11），两种包都能同意。
// 定稿前 final 为 false：发布检查不通过，首发包也不会打开登录。
`;

function main(args) {
  if (fs.existsSync(TARGET) && !args.includes('--force')) {
    const current = require(TARGET);
    if (current.final === true) throw new Error('legal-first-release.js 已经定稿，不再重新生成');
  }
  const legal = build();
  fs.writeFileSync(TARGET, `${HEADER}module.exports = ${JSON.stringify(legal, null, 2)};\n`);
  console.log(`已生成 ${path.relative(process.cwd(), TARGET)}（版本 ${legal.version}，草稿）`);
}

if (require.main === module) {
  try { main(process.argv.slice(2)); } catch (err) { console.error(`没生成：${err.message}`); process.exitCode = 1; }
}

module.exports = { build, VERSION, BANNED };
