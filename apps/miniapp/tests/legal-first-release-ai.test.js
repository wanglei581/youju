// 过审版加 AI 协议（D-098）：content/legal-first-release-ai.js 是完整版协议 2026-11 去掉这次包里没有的
// 批量工具、加量体验、付费那一份，保留 AI 条款，只用于 tools/miniapp_pack.js --profile first-release-ai 打的包。
// 守住：只写包里有的功能，版本排在后台 AI 条款版本之后、后台认它不含工具条款，编号和互相引用对得上，
// 加粗的段落都找得到；草稿期间和生成脚本的结果一致（定稿以后是固定文本，不再比）。
// 11/4 起 content/legal.js 升到 2026-12：和"2026-11 一样"比的是冻结的底稿 tools/release/legal-2026-11.js
const test = require('node:test');
const assert = require('node:assert/strict');
const full = require('../../../tools/release/legal-2026-11.js');
const current = require('../content/legal.js');
const ai = require('../content/legal-first-release-ai.js');
const { SERVER_AI_TERMS_VERSION, SERVER_WITHOUT_TOOLS_TERMS } = require('../../../tools/miniapp_presubmit_check.js');
const { build, VERSION, BANNED } = require('../../../tools/make_legal_first_release_ai.js');

const CN = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
const items = (section) => section.p.filter((p) => /^\d+\. /.test(p)).map((p) => Number(p.split('.')[0]));
const section = (doc, head) => doc.sections.find((s) => s.h.startsWith(head));

test('版本：排在后台第一次认 AI 条款的版本之后（同意它算同意过 AI 条款），后台认它不含批量工具和加量体验条款', () => {
  assert.equal(ai.version, VERSION);
  assert.match(ai.version, /^\d{4}-\d{2}/);
  assert.ok(ai.version >= SERVER_AI_TERMS_VERSION && ai.version > full.version, ai.version);
  assert.ok(SERVER_WITHOUT_TOOLS_TERMS.includes(ai.version), '后台 agreements.WITHOUT_TOOLS_TERMS 里没有这一版');
  // 带批量工具的完整版要排在这一版后面（2026-12）：只同意过这一版的人在完整版里会被请去重新同意
  assert.equal(full.version, '2026-11');
  assert.ok(current.version > ai.version, current.version);
});

test('只写包里有的：保留 AI、投诉举报、我的记录、使用帮助，不写批量工具、加量体验、付费、原文链接', () => {
  const text = JSON.stringify([ai.user, ai.privacy, ai.disclaimer]);
  assert.doesNotMatch(text, BANNED);
  for (const word of ['批量工具', '加量体验', '订单', '原文链接', '统一社会信用代码', '工具使用说明', '上传名单', '支付']) {
    assert.ok(BANNED.test(word), `发布检查没拦：${word}`);
  }
  assert.ok(section(ai.user, '八、AI 功能与生成内容标识'), '用户协议要有 AI 那一条');
  for (const kept of ['AI 功能（登录后使用）', '投诉举报', '我的记录', '使用帮助', 'AI 模型调用', '求索对话DeepSeek Chat']) {
    assert.ok(text.includes(kept), `少了：${kept}`);
  }
  // 运营者、免责声明和完整版一样
  assert.deepEqual(ai.operator, full.operator);
  assert.equal(ai.disclaimer, full.disclaimer);
});

test('章节编号连续，互相引用指向对的条目', () => {
  // 用户协议：十条都在（AI 第八条保留）；第四条去掉了第 5 款（上传名单）
  const heads = ai.user.sections.slice(1).map((s) => s.h.split('、')[0]);
  assert.deepEqual(heads, CN.slice(0, heads.length));
  assert.equal(ai.user.sections.length, full.user.sections.length);
  assert.deepEqual(items(section(ai.user, '四、')), [1, 2, 3, 4]);
  // 隐私保护指引第一部分：1 到 14 项连续（去掉第 13 项批量工具、第 15 项加量体验；AI 改成第 13 项，联系客服改成第 14 项）
  const one = section(ai.privacy, '一、');
  assert.deepEqual(items(one), Array.from({ length: 14 }, (_, i) => i + 1));
  const titled = (n) => one.p.find((p) => p.startsWith(`${n}. `)) || '';
  assert.match(titled(13), /^13\. AI 功能/);
  assert.match(titled(14), /^14\. 联系客服/);
  const all = JSON.stringify(ai.privacy);
  for (const m of all.matchAll(/第一部分第 (\d+) 项/g)) assert.ok(titled(m[1]), `第一部分第 ${m[1]} 项不存在`);
  for (const p of one.p) {
    for (const m of p.replace(/第一部分第 \d+ 项/g, '').matchAll(/第 (\d+) 项/g)) assert.ok(titled(m[1]), `第 ${m[1]} 项不存在：${p.slice(0, 20)}`);
  }
  assert.match(all, /联系客服也不需要登录（见第 14 项）/);
  assert.match(all, /（见第一部分第 14 项）/);
});

test('加粗的段落（keyParagraphs）都找得到，要点（keyPoints）引用的条款都在', () => {
  for (const doc of [ai.user, ai.privacy]) {
    const paragraphs = doc.sections.flatMap((s) => s.p);
    for (const key of doc.keyParagraphs) assert.ok(paragraphs.some((p) => p.startsWith(key)), `找不到加粗段落：${key}`);
  }
  assert.ok(ai.user.keyPoints.length >= 4 && ai.privacy.keyPoints.length >= 4);
  for (const doc of [ai.user, ai.privacy]) {
    for (const point of doc.keyPoints) {
      for (const m of point.matchAll(/([一二三四五六七八九十])·(\d+)/g)) {
        const found = doc.sections.find((s) => s.h.startsWith(`${m[1]}、`));
        assert.ok(found, `${point}：没有第${m[1]}章`);
        assert.ok(found.p.some((p) => p.startsWith(`${m[2]}. `)) || (found.h.startsWith('三、') && m[2] === '2') || (found.h.startsWith('四、') && m[2] === '2'), `${point}：${m[1]}·${m[2]} 不存在`);
      }
    }
  }
  assert.ok(ai.privacy.keyParagraphs.includes('13. AI 功能'));
});

test('改的几处：适用人群、查名单、注明出处、AI 结果送内容安全检查；其余条款和 2026-11 一样', () => {
  const user = JSON.stringify(ai.user);
  const privacy = JSON.stringify(ai.privacy);
  assert.match(user, /适用于企业里负责项目申报的经办人员和为企业提供申报服务的人员/);
  assert.match(user, /只用于在政府公开发布的认定、立项、公示、入库、备案类名单中查找某个企业或机构的名称，不对企业作评价、评级或核验/);
  // D-112（11/2）：不写"征信""信用评价"，以后收费的那一款（六·3）和对应的要点都删掉
  assert.doesNotMatch(user, /征信|信用评价|收费功能|购买前|付费协议/);
  assert.ok(!ai.user.keyParagraphs.includes('3. 公开信息的检索'));
  assert.match(user, /每条注明发文机关、发布日期和来源网站/);
  assert.match(privacy, /按企业名称查名单时输入的企业全称：只用于在政府公开名单中查找/);
  assert.match(privacy, /和 AI 生成的解读、对照结果/);
  assert.match(privacy, /提醒入口在提醒模板开通以后才显示/);
  assert.match(user, /政策解读（含从通知原文整理出的申报材料清单）和条款对照/);
  // AI 那几段（用户协议第八条、指引里 AI 功能、AI 任务的保存期限、AI 模型调用）除编号外和 2026-11 一字不差
  const same = (doc, head, pick) => assert.deepEqual(pick(section(ai[doc], head)), pick(section(full[doc], head)), `${doc} ${head}`);
  same('user', '八、', (s) => s.p.map((p) => p.replace('经办人员和为企业提供申报服务的人员', '经办人员')
    .replace('政策解读（含从通知原文整理出的申报材料清单）和条款对照', '政策解读、材料清单和条款对照')));
  const aiItem = (s) => s.p.find((p) => /^\d+\. AI 功能/.test(p)).replace(/^\d+\. /, '').replace('政策解读（含申报材料清单）', '政策解读和材料清单');
  same('privacy', '一、', aiItem);
  same('privacy', '三、', (s) => s.p.find((p) => p.startsWith('· AI 任务：')));
  // AI 模型调用：写明调用深度求索的求索对话DeepSeek Chat（创始人 11/2），后面"发给模型的只有……"和 2026-11 一字不差
  const modelCall = (s) => s.p.find((p) => p.startsWith('· AI 模型调用：')).split('按我们的委托处理。')[1];
  same('privacy', '四、', modelCall);
  // 类目页上登记的是 DeepSeek（创始人 11/2）：写明调用北京深度求索人工智能基础技术研究有限公司的模型，不再写混元
  assert.match(privacy, /调用求索对话DeepSeek Chat 大模型/);
  // 接口和按委托处理的是杭州深度求索（DeepSeek 开放平台的运营方），备案主体是北京深度求索（独立审阅 11/3）
  assert.match(privacy, /杭州深度求索人工智能基础技术研究有限公司（以下简称“深度求索”）运营的 DeepSeek 开放平台接口/);
  assert.match(privacy, /已由北京深度求索人工智能基础技术研究有限公司在国家网信办完成生成式人工智能服务备案/);
  assert.match(privacy, /北京深度求索人工智能基础技术研究有限公司/);
  assert.doesNotMatch(privacy, /混元/);
  // 深度求索也按委托处理：下一段和隐私要点里"按委托处理"的合作方都写上它
  assert.match(privacy, /上述合作方中，腾讯云和深度求索按我们的委托处理信息：/);
  assert.ok(ai.privacy.keyPoints.some((k) => k.startsWith('腾讯云和深度求索受我们委托处理信息；')));
  assert.doesNotMatch(privacy, /求索对话 DeepSeek/);
  // 段落数：用户协议第四条少 1 款（上传名单），第六条少 1 款（以后收费，D-112）；指引第一部分少 2 项（批量工具、加量体验），第三部分少 2 条保存期限
  const count = (doc) => doc.sections.map((s) => s.p.length);
  // 第二部分多 1 条：提醒入口开通后才显示
  assert.deepEqual(count(ai.privacy).map((n, i) => count(full.privacy)[i] - n), [0, 2, -1, 2, 0, 0, 0, 0, 0]);
  assert.deepEqual(count(ai.user).map((n, i) => count(full.user)[i] - n), [0, 0, 0, 0, 1, 0, 1, 0, 0, 0]);
});

test('草稿期间和生成脚本 tools/make_legal_first_release_ai.js 的结果一致（不在别处手改）', () => {
  if (ai.final === true) return;
  assert.equal(ai.final, false);
  assert.match(ai.status, /草稿/);
  assert.deepEqual(JSON.parse(JSON.stringify(ai)), JSON.parse(JSON.stringify(build())));
});

test('D-112：完整版里六·3、它的要点、重点段落对不上或重复时，生成脚本报错，不会不声不响地删掉', () => {
  const clone = () => JSON.parse(JSON.stringify(full));
  // 六·3 后半句变了
  const a = clone();
  const six = section(a.user, '六、');
  six.p[six.p.length - 1] = six.p[six.p.length - 1].replace('已购买的服务按购买时的约定履行', '已购买的服务另行约定');
  assert.throws(() => build(a), /六·3/);
  // 六·3 不再是第六条最后一款
  const b = clone();
  section(b.user, '六、').p.push('4. 新加的一款。');
  assert.throws(() => build(b), /六·3/);
  // 重点段落里六·3 那一段重复
  const c = clone();
  c.user.keyParagraphs.push('3. 公开信息的检索');
  assert.throws(() => build(c), /重点段落/);
  // 要点改了一个字
  const d = clone();
  d.user.keyPoints = d.user.keyPoints.map((k) => k.replace('收费功能另行购买', '收费功能另外购买'));
  assert.throws(() => build(d), /要点 六·3/);
});
