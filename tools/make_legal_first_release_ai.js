#!/usr/bin/env node
// 由 content/legal.js（2026-11，完整版）生成"过审版加 AI"用的协议草稿 content/legal-first-release-ai.js（D-098）。
// 这次提审的包（tools/miniapp_pack.js --profile first-release-ai，22 页）有 AI 首页、AI 解读、条款对照、我的记录、
// 投诉举报、使用帮助，没有批量工具、加量体验、购买和订单页面。协议只写包里真有的功能（微信"所列即所用"）：
// 保留 AI 条款；去掉批量工具、加量体验、付费；原文链接、查名单的说法和过审版（2026-10.2，D-094）一样改；
// 适用人群写上申报服务人员（D-093 ④）；AI 结果也送微信内容安全检查（上线门槛第 4 道）。
// 每一处改动都写在下面，按原文逐字查找、只许命中一次；原文改过、对不上时直接报错，不会悄悄漏改。
// 定稿后 legal-first-release-ai.js 就是固定文本，不再覆盖（--force 才覆盖）。
// 用法：node tools/make_legal_first_release_ai.js [--force]
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', 'apps', 'miniapp', 'content');
const TARGET = path.join(ROOT, 'legal-first-release-ai.js');
// 排在 2026-11 后面（字符串比较：2026-11 < 2026-11.1 < 2026-12）：同意这一版就算同意过 AI 条款（后台 AI_AGREEMENT_VERSION）。
// 这一版去掉了批量工具和加量体验，后台 agreements.WITHOUT_TOOLS_TERMS 里要有它，不然会被当成同意过工具条款
const VERSION = '2026-11.1';
// "过审版加 AI"协议里不能出现的说法（包里没有的功能和页面、原文链接、征信感的说法）。
// 发布检查（tools/miniapp_presubmit_check.js）用同一份。"购买"只出现在一·4 以后收费的说明里（购买前单独展示），不拦
const BANNED = /批量工具|批量名单|名单比对|通知表|上传名单|客户名单|选中的文件|加量体验|申报季加量|工具使用说明|工具任务|订单|购买页|支付|发票|原文链接|统一社会信用代码|核对企业主体/;

function build() {
  delete require.cache[require.resolve(path.join(ROOT, 'legal.js'))];
  const legal = JSON.parse(JSON.stringify(require(path.join(ROOT, 'legal.js'))));
  if (legal.version !== '2026-11') throw new Error(`content/legal.js 的版本是 ${legal.version}，这个脚本按 2026-11 写的，先核对`);
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
    else {
      const next = p.replace(find, replace);
      if (next === p) throw new Error(`${where}：没有改动`);
      section.p[i] = next;
    }
  };
  const U = legal.user;
  const P = legal.privacy;

  // 要点（keyPoints）里把 find 换成 replace；必须恰好命中一条
  const point = (doc, find, replace, where) => {
    const hits = doc.keyPoints.filter((k) => k.includes(find));
    if (hits.length !== 1) throw new Error(`${where}：要点“${find.slice(0, 24)}…”命中 ${hits.length} 条`);
    doc.keyPoints = doc.keyPoints.map((k) => (k.includes(find) ? k.replace(find, replace) : k));
  };

  // ================= 用户协议 =================
  // 包里有 AI：服务说明和未登录说明写上（agy 10/27）
  edit(sec(U, '一、'), '登录后还可以使用关注、微信提醒和纠错反馈。', '登录后还可以使用关注、微信提醒、纠错反馈和 AI 功能（政策解读、材料清单和条款对照，见第八条）。', '一·1');
  edit(sec(U, '三、'), '关注、微信提醒和纠错需要先同意本协议和《隐私保护指引》。', '关注、微信提醒、纠错和 AI 功能需要先同意本协议和《隐私保护指引》。', '三·2');
  // 要点写准确（Grok 10/27）：包里没有收费功能；"不承担间接损失"只在服务中断那一款（六·2）
  point(U, '收费功能另行购买，购买前单独展示价格、期限和协议（六·3）', '现阶段免费；以后新增收费功能，购买前单独展示价格、期限和协议（六·3）', '要点 六·3');
  point(U, '因我们故意、重大过失或依法应由我们承担责任的情形造成的损失依法承担，其他原因造成的间接损失不承担（二·3、六·2）',
    '因我们故意、重大过失或依法应由我们承担责任的情形造成的损失依法承担（二·3）；系统维护、故障等导致服务中断的，除上述情形外不承担间接损失（六·2）', '要点 二·3');
  // 包里不给原文链接（D-089、D-091），改成注明出处（和过审版 2026-10.2 一样）
  edit(sec(U, '二、'), '1. 来源与原文链接：本小程序所收录、展示的通知与政策均来源于政府公开发布渠道，我们在条目中均附有或提供官方原文链接。',
    '1. 来源与出处：本小程序所收录、展示的通知与政策均来源于政府公开发布渠道，每条注明发文机关、发布日期和来源网站。', '二·1');
  edit(sec(U, '二、'), '我们会合理核对收录的信息并提供官方原文链接', '我们会合理核对收录的信息并注明出处', '二·3');
  edit(sec(U, '三、'), '并删除您的关注、微信提醒授权、纠错记录、工具任务及结果和加量体验记录；', '并删除您的关注、微信提醒授权、纠错记录和 AI 的任务及结果；', '三·3');
  // 查名单不像征信、只收企业全称（D-091 ③，和过审版一样）
  edit(sec(U, '四、'), '3. 企业名称查询限制：“按企业名称查名单”功能仅用于用户根据公开公示名单核对企业主体信息，您不得输入侵犯个人隐私、国家秘密或其他非法违规的关键词。',
    '3. 查名单：“按企业名称查名单”只用于在政府公开发布的认定、立项、公示、入库、备案类名单中查找某个企业或机构的名称，不提供征信、信用评价或资质核验服务；您不得输入个人姓名、国家秘密或其他违法违规的内容。', '四·3');
  edit(sec(U, '四、'), '5. 上传名单：', null, '四·5');
  // 适用人群：企业经办人员和申报服务人员（D-093 ④，AI 上线那一版协议里改）
  edit(sec(U, '八、'), '适用于企业里负责项目申报的经办人员，不面向未成年人。',
    '适用于企业里负责项目申报的经办人员和为企业提供申报服务的人员，不面向未成年人。', '八·1');
  U.keyPoints = U.keyPoints.filter((k) => !k.includes('四·5'));
  U.keyParagraphs = U.keyParagraphs.filter((k) => k !== '5. 上传名单');

  // ================= 隐私保护指引 =================
  // 第一部分去掉第 13 项（批量工具）、第 15 项（加量体验）：AI 功能 14 → 13，联系客服 16 → 14
  const one = sec(P, '一、');
  edit(one, '关注、微信提醒、纠错、批量工具、AI 功能和加量体验需要登录', '关注、微信提醒、纠错和 AI 功能需要登录', '一·1');
  edit(one, '联系客服也不需要登录（见第 16 项）', '联系客服也不需要登录（见第 14 项）', '一·1 客服');
  // 微信用户标识的用途写上 AI（agy 10/27）：关联 AI 任务和次数；AI 结果送内容安全检查（上线门槛第 4 道）
  edit(one, '按您的授权发送提醒；您通过纠错表单提交内容（见第 6 项）时，随内容一起送微信内容安全接口检查。',
    '按您的授权发送提醒；关联您的 AI 任务和每天的使用次数（见第 13 项）；您通过纠错表单提交内容（见第 6 项）、AI 生成解读和对照结果时，随内容一起送微信内容安全接口检查。', '一·2 目的');
  edit(one, '；使用批量工具前，您同意的《工具使用说明》的版本、同意时间。目的：确认您已同意；指引、协议或《工具使用说明》更新后，请您重新阅读并同意。',
    '。目的：确认您已同意；指引或协议更新后，请您重新阅读并同意。', '一·3');
  // 投诉举报页随 AI 一起在包里；购买页、订单页不在
  edit(one, '同一张表单还用于“申请覆盖”（告诉我们想增加的地区和栏目）、投诉举报，以及付费开放后的支付和订单问题（从购买页、订单页进来时，内容前可能自动带上订单号），这些内容都按本项处理。目的：对照原文核实并修正数据；评估是否增加信息来源；处理您的投诉、建议和订单问题。',
    '同一张表单还用于“申请覆盖”（告诉我们想增加的地区和栏目）和投诉举报，这些内容都按本项处理。目的：对照原文核实并修正数据；评估是否增加信息来源；处理您的投诉和建议。', '一·6');
  edit(one, '7. 按企业名称查名单时输入的企业名称或统一社会信用代码：只用于在公开名单中比对，不保存到您的账号下。',
    '7. 按企业名称查名单时输入的企业全称：只用于在政府公开名单中查找，不保存到您的账号下。', '一·7');
  edit(one, '您提交、取消、删除批量工具或 AI 任务时另记操作类型和任务编号，开始加量体验时另记操作类型。', '您提交、取消、删除 AI 任务时另记操作类型和任务编号。', '一·8 操作');
  edit(one, '用于保障运行安全、排查故障和追查网络安全事件，不用于判断能否参加加量体验等活动；', '用于保障运行安全、排查故障和追查网络安全事件；', '一·8 用途');
  edit(one, '投诉举报页的联系邮箱（开放 AI 功能后才有）；', '投诉举报页的联系邮箱；', '一·9');
  edit(one, '我们不在服务器上保存。批量工具（开放后登录使用）在服务器上生成的结果文件，见第 13 项。', '我们不在服务器上保存。', '一·10');
  edit(one, '13. 批量工具（批量名单比对、客户相关通知表，开放后登录使用）', null, '一·13');
  edit(one, '14. AI 功能（登录后使用）', '13. AI 功能（登录后使用）', '一·14');
  edit(one, '15. 加量体验（开放后登录使用）', null, '一·15');
  edit(one, '16. 联系客服（不需要登录）', '14. 联系客服（不需要登录）', '一·16');
  // 包里没有上传客户名单的功能；"我的客户"填的是客户名称（只存在手机上）
  edit(one, '标识里没有您填写的企业信息、客户名单、AI 结果和任务编号', '标识里没有您填写的企业信息、客户名称、AI 结果和任务编号', '一·16 标识');
  const three = sec(P, '三、');
  edit(three, '· 批量工具：', null, '三·2 批量工具');
  edit(three, '· 加量体验记录：', null, '三·2 加量体验');
  edit(three, '纠错内容、批量工具和 AI 的任务及结果、加量体验记录；访问日志依法保存', '纠错内容、AI 的任务及结果；访问日志依法保存', '三·2 注销');
  const four = sec(P, '四、');
  // 内容安全：AI 生成的解读、对照结果在显示前也送微信检查（上线门槛第 4 道）
  edit(four, '③ 内容安全检查，涉及微信用户标识和纠错表单提交的内容（含申请覆盖、投诉举报、支付和订单问题，已隐去手机号等）；',
    '③ 内容安全检查，涉及微信用户标识、纠错表单提交的内容（含申请覆盖、投诉举报，已隐去手机号等）和 AI 生成的解读、对照结果；', '四·2 ③');
  edit(four, '（见第一部分第 16 项）', '（见第一部分第 14 项）', '四·2 ④');
  const five = sec(P, '五、');
  edit(five, '（账号记录、同意记录、关注、微信提醒授权、纠错、工具任务记录、加量体验的开始和结束时间）', '（账号记录、同意记录、关注、微信提醒授权、纠错、AI 任务记录）', '五·1');
  edit(five, '（如账号建立时间、《工具使用说明》的同意记录）', '（如账号建立时间）', '五·1 其他');
  edit(five, '在“我的记录”里删除批量工具和 AI 的任务及结果（处理中的要等处理结束）。', '在“我的记录”里删除 AI 的任务及结果（处理中的要等处理结束）。', '五·3');
  edit(five, '纠错内容、批量工具和 AI 的任务及结果、加量体验记录（', '纠错内容、AI 的任务及结果（', '五·4');
  edit(five, '依法须留存的信息（访问日志，以及启用付费后的订单和发票记录）按规定期限保存', '依法须留存的信息（访问日志）按规定期限保存', '五·4 留存');
  edit(sec(P, '六、'), '（关注、微信提醒、纠错、批量工具、AI 功能、加量体验）', '（关注、微信提醒、纠错、AI 功能）', '六');
  edit(sec(P, '七、'), '（如关注、纠错、批量工具、AI 功能、加量体验）', '（如关注、纠错、AI 功能）', '七');
  P.keyPoints = P.keyPoints.map((k) => k.replace('（一·14、四·2）', '（一·13、四·2）'));
  if (!P.keyPoints.some((k) => k.includes('（一·13、四·2）'))) throw new Error('隐私要点里 AI 那一条的编号没改到');
  P.keyParagraphs = P.keyParagraphs.map((k) => (k === '14. AI 功能' ? '13. AI 功能' : k));
  if (!P.keyParagraphs.includes('13. AI 功能')) throw new Error('隐私重点段落里 AI 那一段的编号没改到');

  legal.version = VERSION;
  legal.final = false;
  legal.status = '草稿（D-098 过审版加 AI 协议，待创始人过目后定稿）';
  const left = JSON.stringify([legal.user, legal.privacy, legal.disclaimer, legal.status]).match(new RegExp(BANNED.source, 'g'));
  if (left) throw new Error(`还有没去掉的：${[...new Set(left)].join('、')}`);
  // 第一部分的编号要连着（1、2、3……），去掉两项后不能留空号
  const numbers = one.p.map((p) => /^(\d+)\. /.exec(p)).filter(Boolean).map((m) => Number(m[1]));
  if (numbers.some((n, i) => n !== i + 1)) throw new Error(`隐私保护指引第一部分编号不连续：${numbers.join('、')}`);
  return legal;
}

const HEADER = `// "过审版加 AI"（22 页，D-098）用的用户协议与隐私保护指引：在 content/legal.js（2026-11）的基础上，去掉包里没有的
// 批量工具、加量体验、付费相关条款，保留 AI 条款；原文链接、查名单的说法和过审版（2026-10.2，D-094）一样改；
// 适用人群写上申报服务人员（D-093 ④）；AI 结果也送微信内容安全检查（上线门槛第 4 道）。
// 由 tools/make_legal_first_release_ai.js 生成，逐处改动写在那个脚本里；定稿后这份就是固定文本，不再重新生成。
// 只用于 tools/miniapp_pack.js --profile first-release-ai 打的包（打包时替换 content/legal.js）；完整版仍用 content/legal.js。
// 版本号排在 2026-11 后面（字符串比较：2026-11 < 2026-11.1 < 2026-12）：同意这一版算同意过 AI 条款；
// 这一版没有批量工具和加量体验的条款，后台 agreements.WITHOUT_TOOLS_TERMS 里有它，同意的是这一版也用不了这两项。
// 以后带批量工具的完整版，协议版本要排在这一版后面（比如 2026-12），不然只同意过这一版的人在完整版里不会被请去重新同意。
// 后台要认这一版（当前版本 YOUJU_AGREEMENT_VERSION=2026-11.1）才同意得了；改之前要创始人点头（D-095 例外）。
// 定稿前 final 为 false：发布检查不通过，包里也不会打开登录。
`;

function main(args) {
  if (fs.existsSync(TARGET) && !args.includes('--force')) {
    const current = require(TARGET);
    if (current.final === true) throw new Error('legal-first-release-ai.js 已经定稿，不再重新生成');
  }
  const legal = build();
  fs.writeFileSync(TARGET, `${HEADER}module.exports = ${JSON.stringify(legal, null, 2)};\n`);
  console.log(`已生成 ${path.relative(process.cwd(), TARGET)}（版本 ${legal.version}，草稿）`);
}

if (require.main === module) {
  try { main(process.argv.slice(2)); } catch (err) { console.error(`没生成：${err.message}`); process.exitCode = 1; }
}

module.exports = { build, VERSION, BANNED };
