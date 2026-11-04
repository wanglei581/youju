// 完整版协议 2026-12（D-117：提审完整版、开通支付）：content/legal.js 和《付费服务协议》content/pay-terms.js。
// 守住：版本排在过审版加 AI（2026-11.1）后面、过得了后台的工具和 AI 门槛；2026-11.1 里对完整版同样适用的改动都搬过来了，
// 其余条款和 2026-11 一字不差；付费部分并进了隐私保护指引；编号、互相引用、加粗段落对得上；
// 写到批量名单比对的地方都是能整句删掉的分句（D-104 待创始人定）；付费协议和 docs 里的正文、说明文档里附的全文逐字一致。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const legal = require('../content/legal.js');
const pay = require('../content/pay-terms.js');
const base = require('../../../tools/release/legal-2026-11.js');
const ai = require('../content/legal-first-release-ai.js');
const { render } = require('../../../tools/legal_markdown.js');
const {
  SERVER_AI_TERMS_VERSION, SERVER_WITHOUT_TOOLS_TERMS, FINGERPRINTS, agreementFingerprint,
} = require('../../../tools/miniapp_presubmit_check.js');

const ROOT = path.join(__dirname, '..', '..', '..');
const section = (doc, head) => doc.sections.find((s) => s.h.startsWith(head));
const paragraphs = (doc) => doc.sections.flatMap((s) => s.p);
const items = (s) => s.p.filter((p) => /^\d+\. /.test(p)).map((p) => Number(p.split('.')[0]));
const legalText = JSON.stringify([legal.user, legal.privacy, legal.disclaimer]);

test('版本：排在过审版加 AI 2026-11.1 后面，后台认它含批量工具条款、满足 AI 条款门槛；草稿期间不登记指纹', () => {
  assert.match(legal.version, /^\d{4}-\d{2}(\.\d+)?$/);
  assert.ok(legal.version > base.version && legal.version > ai.version, legal.version);
  assert.ok(SERVER_WITHOUT_TOOLS_TERMS.every((v) => legal.version > v), '要排在后台"不含工具条款"的版本后面');
  assert.ok(!SERVER_WITHOUT_TOOLS_TERMS.includes(legal.version));
  assert.ok(legal.version >= SERVER_AI_TERMS_VERSION);
  // 后台批量工具条款从哪一版起认（agreements.TOOLS_AGREEMENT_VERSION）
  const tools = /^TOOLS_AGREEMENT_VERSION = "([^"]+)"$/m.exec(fs.readFileSync(path.join(ROOT, 'backend/src/youju/agreements.py'), 'utf8'));
  assert.ok(tools && legal.version >= tools[1]);
  if (legal.final === true) {
    assert.equal(FINGERPRINTS[legal.version], agreementFingerprint(legal), '定稿以后指纹要登记');
  } else {
    assert.match(legal.status, /草稿/);
    assert.equal(Object.hasOwn(FINGERPRINTS, legal.version), false, '草稿不登记指纹');
  }
  assert.deepEqual(legal.operator, base.operator);
  assert.equal(legal.disclaimer, base.disclaimer);
});

test('不写的说法：征信、信用、原文链接（D-089、D-112），"开放后""付费开放后"这类完整版里已经不成立的话，混元和云开发线路（D-114）', () => {
  for (const [name, text] of [['legal.js', legalText], ['pay-terms.js', pay.text]]) {
    // "微信用户标识"里的"信用"不算
    assert.doesNotMatch(text, /征信|(?<!微)信用|原文链接/, name);
    assert.doesNotMatch(text, /开放后|付费开放后|启用付费后|开放 AI 功能后才有|腾讯云开发|混元|【/, name);
  }
});

test('2026-11.1 里对完整版同样适用的改动都搬过来了：和 2026-11.1 一字不差', () => {
  const same = (doc, head, start) => {
    const want = section(ai[doc], head).p.find((p) => p.startsWith(start));
    assert.ok(want, `2026-11.1 没有 ${start}`);
    assert.ok(section(legal[doc], head).p.includes(want), `${doc} ${head} ${start}`);
  };
  same('user', '二、', '1. 来源与出处');
  same('user', '二、', '3. 核对与责任');
  same('user', '四、', '3. 查名单');
  same('privacy', '一、', '7. 按企业名称查名单');
  same('privacy', '一、', '9. 剪切板');
  same('privacy', '二、', '· 提醒入口在提醒模板开通以后才显示');
  same('privacy', '四、', '· AI 模型调用');
  // AI 那一条（第八条）整条一样：适用人群（D-093 ④）、材料清单写成解读的一部分
  assert.deepEqual(section(legal.user, '八、'), section(ai.user, '八、'));
  // 指引里 AI 功能一项：完整版还是第 14 项（2026-11.1 去掉批量工具后是第 13 项），正文一样
  const aiItem = (doc) => section(doc, '一、').p.find((p) => /^\d+\. AI 功能/.test(p)).replace(/^\d+\. /, '');
  assert.equal(aiItem(legal.privacy), aiItem(ai.privacy));
  // 间接损失那条要点、深度求索按委托处理（D-114）
  assert.equal(legal.user.keyPoints[0], ai.user.keyPoints[0]);
  assert.ok(legal.privacy.keyPoints.some((k) => k.startsWith('腾讯云和深度求索受我们委托处理信息；')));
  assert.match(legalText, /上述合作方中，腾讯云和深度求索按我们的委托处理信息：/);
  assert.match(legalText, /和 AI 生成的解读、对照结果；/);
  assert.match(legalText, /AI 生成解读和对照结果时，随内容一起送微信内容安全接口检查/);
});

test('完整版保留批量工具、加量体验，付费并进隐私保护指引（第 17 项）；收费那一款指向《付费服务协议》', () => {
  const one = section(legal.privacy, '一、');
  assert.deepEqual(items(one), Array.from({ length: 17 }, (_, i) => i + 1));
  const titled = (n) => one.p.find((p) => p.startsWith(`${n}. `));
  assert.match(titled(13), /^13\. 批量工具（登录后使用）/);
  assert.match(titled(14), /^14\. AI 功能/);
  assert.match(titled(15), /^15\. 加量体验（登录后使用）/);
  assert.match(titled(16), /^16\. 联系客服/);
  assert.match(titled(17), /^17\. 购买批量工具/);
  for (const bullet of ['· 订单：', '· 赠送天数：', '· 退款：', '· 发票：', '· 支付平台的通知：', '· 苹果退款问询：', '· 其他方式退款：']) {
    assert.equal(one.p.filter((p) => p.startsWith(bullet)).length, 1, bullet);
  }
  assert.ok(section(legal.privacy, '四、').p.some((p) => p.startsWith('· 苹果公司（Apple）：')));
  // 苹果问询的答复是向境外提供：存储地点那条不能再说一概不向境外传输
  assert.match(section(legal.privacy, '三、').p[0], /苹果公司（见第四部分第 2 项）以外，不向境外传输/);
  const fee = section(legal.user, '六、').p.at(-1);
  assert.match(fee, /^3\. 收费服务：/);
  assert.match(fee, /《付费服务协议》/);
  assert.ok(legal.user.keyParagraphs.includes('3. 收费服务'));
  // 隐私保护指引和付费协议说的是同一套规矩
  for (const phrase of ['答复只说明这一笔是否在付款后 24 小时内、是否用过付费的量', '注销本身不退剩余天数', '注销后发邮件并附订单号']) {
    assert.ok(legalText.includes(phrase) && pay.text.includes(phrase), phrase);
  }
  assert.match(pay.text, /可以开具的发票：电子发票，个人或单位抬头都可以。/);
  assert.match(pay.text, /本服务不含 AI 次数/);
});

test('其余条款和 2026-11 一字不差；段落数只多了付费那几段和提醒入口一句', () => {
  const changed = {
    user: ['1. 服务定位', '4. 版本说明', '1. 来源与', '3. 核对与责任', '2. 未登录状态', '3. 账号注销', '3. 企业名称查询限制', '3. 查名单',
      '5. 上传名单', '3. 公开信息的检索', '3. 收费服务', '1. 功能范围和适用人群'],
    privacy: ['1. 不登录也能使用', '· 目的：识别您的账号', '6. 纠错内容', '7. 按企业名称查名单', '9. 剪切板', '10. 导出文件', '12. 保存在您手机上的内容',
      '13. 批量工具', '14. AI 功能', '15. 加量体验', '17. 购买批量工具', '· 订单：', '· 赠送天数：', '· 退款：', '· 发票：', '· 支付平台的通知：',
      '· 苹果退款问询：', '· 其他方式退款：', '· 提醒入口', '1. 存储地点', '· 订单、退款和开票记录', '· 赠送天数的记录', '· 到期后我们删除或匿名化处理',
      '· 深圳市腾讯计算机系统有限公司（微信）', '· AI 模型调用', '· 苹果公司（Apple）', '上述合作方中', '1. 查阅、复制', '2. 更正', '4. 撤回同意、注销',
      '本小程序面向具备完全民事行为能力'],
  };
  for (const doc of ['user', 'privacy']) {
    const now = paragraphs(legal[doc]);
    const before = paragraphs(base[doc]);
    const unexpected = now.filter((p) => !before.includes(p)).concat(before.filter((p) => !now.includes(p)))
      .filter((p) => !changed[doc].some((head) => p.startsWith(head)));
    assert.deepEqual(unexpected, [], `${doc} 有没列进改动清单的改动`);
    assert.deepEqual(legal[doc].sections.map((s) => s.h), base[doc].sections.map((s) => s.h));
  }
  const count = (doc) => doc.sections.map((s) => s.p.length);
  assert.deepEqual(count(legal.user), count(base.user));
  // 指引：第一部分多 8 段（第 17 项和它的 7 条），第二部分多 1 段（提醒入口），第三部分多 2 段（订单等、赠送记录的保存期限），第四部分多 1 段（苹果公司）
  assert.deepEqual(count(legal.privacy).map((n, i) => n - count(base.privacy)[i]), [0, 8, 1, 2, 1, 0, 0, 0, 0]);
});

test('编号连续、互相引用指向对的条目，加粗段落各找得到一段，要点引用的条款都在', () => {
  const heads = legal.user.sections.slice(1).map((s) => s.h.split('、')[0]);
  assert.deepEqual(heads, ['一', '二', '三', '四', '五', '六', '七', '八', '九']);
  assert.deepEqual(items(section(legal.user, '四、')), [1, 2, 3, 4, 5]);
  assert.deepEqual(items(section(legal.user, '六、')), [1, 2, 3]);
  const one = section(legal.privacy, '一、');
  const titled = (n) => one.p.find((p) => p.startsWith(`${n}. `));
  const all = JSON.stringify(legal.privacy);
  for (const m of all.matchAll(/第一部分第 (\d+) 项/g)) assert.ok(titled(m[1]), `第一部分第 ${m[1]} 项不存在`);
  for (const p of one.p) {
    for (const m of p.replace(/第一部分第 \d+ 项/g, '').matchAll(/第 (\d+)(?: 至 (\d+))? 项/g)) {
      assert.ok(titled(m[1]) && (!m[2] || titled(m[2])), `第 ${m[0]} 不存在：${p.slice(0, 20)}`);
    }
  }
  assert.match(all, /联系客服也不需要登录（见第 16 项）/);
  assert.match(all, /（见第一部分第 16 项）/);
  assert.match(all, /（见第 13 至 15 项、第 17 项）/);
  for (const doc of [legal.user, legal.privacy]) {
    const ps = paragraphs(doc);
    for (const key of doc.keyParagraphs) assert.equal(ps.filter((p) => p.startsWith(key)).length, 1, `加粗段落 ${key}`);
    for (const point of doc.keyPoints) {
      for (const m of point.matchAll(/([一二三四五六七八九十])·(\d+)/g)) {
        const found = doc.sections.find((s) => s.h.startsWith(`${m[1]}、`));
        assert.ok(found, `${point}：没有第${m[1]}章`);
        assert.ok(found.p.some((p) => p.startsWith(`${m[2]}. `)) || (['三、', '四、'].some((h) => found.h.startsWith(h)) && m[2] === '2'), `${point}：${m[1]}·${m[2]} 不存在`);
      }
    }
  }
  assert.ok(legal.privacy.keyParagraphs.includes('17. 购买批量工具'));
});

// D-104（批量名单比对等微信结论）和 D-117（所有功能都开放）说法不一，等创始人定：写到它的地方都是能整句删掉的分句，
// 说明文档 docs/合规/完整版协议草稿_2026-12.md 第四节逐处列着。定了"不带"就照这张表删
const COMPARE_CLAUSES = [
  ['legal', '、批量名单比对）和 AI 功能', '）和 AI 功能'],
  ['legal', '；批量名单比对只需企业名称或营业执照上的 18 位代码。', '。'],
  ['legal', '批量名单比对使用您提交的企业名称或营业执照上的 18 位代码，生成的结果文件里也有这些内容。', ''],
  ['pay', '；批量名单比对“未查到”不表示企业未上榜或不具备资质（第四条第 1 项）', '（第四条第 1 项）'],
  ['pay', '“客户相关通知表”和“批量名单比对”，', '“客户相关通知表”，'],
  ['pay', '已收录的名单发生漏查、', ''],
  ['pay', '；批量名单比对“未查到”不表示企业未上榜或不具备资质。申报条件', '。申报条件'],
];

test('D-104：写到批量名单比对的只有说明文档列的那几处，删掉以后不再提它、句子照样通', () => {
  let text = { legal: legalText, pay: pay.text };
  for (const [where, find, replace] of COMPARE_CLAUSES) {
    assert.equal(text[where].split(find).length - 1, 1, `${where}：${find}`);
    text = { ...text, [where]: text[where].replace(find, replace) };
  }
  assert.doesNotMatch(text.legal + text.pay, /名单比对|名单发生漏查/);
  assert.doesNotMatch(text.legal + text.pay, /，。|；。|、）|（）/);
  // 说明文档里列的就是这几处
  const doc = fs.readFileSync(path.join(ROOT, 'docs/合规/完整版协议草稿_2026-12.md'), 'utf8');
  for (const [, find] of COMPARE_CLAUSES) assert.ok(doc.includes(find), `说明文档没列：${find}`);
});

test('付费服务协议：版本 pay-2026-11、还没定稿（付款按钮不可用），正文和 docs 里的定稿稿逐段一致', () => {
  assert.equal(pay.version, 'pay-2026-11');
  assert.equal(pay.final, false);
  assert.equal(require('../utils/pay.js').termsFinal(pay), false);
  const lines = pay.text.split('\n');
  assert.equal(lines[0], `版本：${pay.version}`);
  // docs/合规/付费服务协议与退款规则_v2.md 的"正文"一节：每段一行，小标题去掉 ###，列表去掉 "- "，去掉 **
  const md = fs.readFileSync(path.join(ROOT, 'docs/合规/付费服务协议与退款规则_v2.md'), 'utf8');
  const body = md.slice(md.indexOf(`## 正文（${pay.version}）`), md.indexOf('## 11/4 入包时改的'));
  const plain = body.split('\n').slice(1).map((l) => l.trim()).filter(Boolean)
    .map((l) => l.replace(/^###\s+/, '').replace(/^-\s+/, '').replace(/\*\*/g, ''));
  assert.deepEqual(plain, lines);
});

test('给创始人看的说明文档里附的全文和 legal.js、pay-terms.js 逐字一致（2026-12 草稿期间）', () => {
  if (legal.version !== '2026-12') return;
  const doc = fs.readFileSync(path.join(ROOT, 'docs/合规/完整版协议草稿_2026-12.md'), 'utf8');
  // 全文按 tools/legal_markdown.js 转出来，标题降一级放进说明文档
  const full = render(legal).split('\n').map((l) => (l.startsWith('#') ? `#${l}` : l)).join('\n');
  assert.ok(doc.includes(full), '协议全文和 legal.js 对不上：用 tools/legal_markdown.js 重新转一份贴进去');
  const payFull = pay.text.split('\n').join('\n\n');
  assert.ok(doc.includes(payFull), '付费服务协议全文和 pay-terms.js 对不上');
});
