// 过审版协议（D-093 ⑤）：content/legal-first-release.js 是 content/legal.js（2026-11）去掉首发包里没有的功能
// （AI、批量工具、加量体验、付费）那一份，只用于 tools/miniapp_pack.js 打的首发包。守住：只写包里有的功能，
// 版本排在后台 AI 条款版本之前，编号和互相引用对得上，加粗的段落都找得到；草稿期间和生成脚本的结果一致
// （定稿以后是固定文本，不再比）。
const test = require('node:test');
const assert = require('node:assert/strict');
const full = require('../content/legal.js');
const first = require('../content/legal-first-release.js');
const { SERVER_AI_TERMS_VERSION, FIRST_RELEASE_TERMS_BANNED } = require('../../../tools/miniapp_presubmit_check.js');
const { build } = require('../../../tools/make_legal_first_release.js');

const CN = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
const items = (section) => section.p.filter((p) => /^\d+\. /.test(p)).map((p) => Number(p.split('.')[0]));

test('版本：比 10/15 首发的 2026-10 新，比后台第一次认 AI 条款的版本早（字符串比较）', () => {
  assert.match(first.version, /^\d{4}-\d{2}/);
  assert.ok(first.version > '2026-10' && first.version < SERVER_AI_TERMS_VERSION, first.version);
  // 完整版 2026-11 含 AI 条款；10/28 起后台从 2026-11.1 起认（完整版没写"AI 结果送微信内容安全检查"），
  // 完整版要升到 2026-12 以后才能对所有人开放 AI（白名单试用不看版本）
  assert.ok(full.version >= '2026-11' && full.version > first.version);
});

test('只写首发包里有的：不写 AI、批量工具、加量体验、付费、投诉举报页、原文链接、使用帮助和"我的记录"', () => {
  assert.doesNotMatch(JSON.stringify([first.user, first.privacy, first.disclaimer]), FIRST_RELEASE_TERMS_BANNED);
  for (const word of ['AI', '批量工具', '加量体验', '订单', '投诉举报', '原文链接', '统一社会信用代码']) {
    assert.ok(FIRST_RELEASE_TERMS_BANNED.test(word), `发布检查没拦：${word}`);
  }
  assert.doesNotMatch(first.status, /\bAI\b/);
  // 运营者、免责声明和完整版一样
  assert.deepEqual(first.operator, full.operator);
  assert.equal(first.disclaimer, full.disclaimer);
});

test('章节编号连续，互相引用指向对的条目', () => {
  // 用户协议：去掉了第八条（AI），后面的"联系我们"改成第八条；第四条去掉了第 5 款（上传名单）
  const heads = first.user.sections.slice(1).map((s) => s.h.split('、')[0]);
  assert.deepEqual(heads, CN.slice(0, heads.length));
  assert.equal(first.user.sections.length, full.user.sections.length - 1);
  assert.match(first.user.sections.at(-1).h, /^八、联系我们/);
  const four = first.user.sections.find((s) => s.h.startsWith('四、'));
  assert.deepEqual(items(four), [1, 2, 3, 4]);
  // 隐私保护指引第一部分：1 到 13 项连续（去掉了第 13 项批量工具、14 项 AI、15 项加量体验，联系客服改成第 13 项）
  const one = first.privacy.sections.find((s) => s.h.startsWith('一、'));
  assert.deepEqual(items(one), Array.from({ length: 13 }, (_, i) => i + 1));
  const titled = (n) => one.p.find((p) => p.startsWith(`${n}. `)) || '';
  assert.match(titled(13), /^13\. 联系客服/);
  const all = JSON.stringify(first.privacy);
  for (const m of all.matchAll(/第一部分第 (\d+) 项/g)) assert.ok(titled(m[1]), `第一部分第 ${m[1]} 项不存在`);
  for (const p of one.p) {
    for (const m of p.replace(/第一部分第 \d+ 项/g, '').matchAll(/第 (\d+) 项/g)) assert.ok(titled(m[1]), `第 ${m[1]} 项不存在：${p.slice(0, 20)}`);
  }
  assert.match(all, /联系客服也不需要登录（见第 13 项）/);
  assert.match(all, /（见第一部分第 13 项）/);
});

test('加粗的段落（keyParagraphs）都找得到，要点（keyPoints）引用的条款都在', () => {
  for (const doc of [first.user, first.privacy]) {
    const paragraphs = doc.sections.flatMap((s) => s.p);
    for (const key of doc.keyParagraphs) assert.ok(paragraphs.some((p) => p.startsWith(key)), `找不到加粗段落：${key}`);
  }
  assert.ok(first.user.keyPoints.length >= 4 && first.privacy.keyPoints.length >= 4);
  // 要点里写的"二·3""三·2"这类出处：那一章有那一条（条目按"N."或"· "开头的段落数）
  for (const doc of [first.user, first.privacy]) {
    for (const point of doc.keyPoints) {
      for (const m of point.matchAll(/([一二三四五六七八九十])·(\d+)/g)) {
        const section = doc.sections.find((s) => s.h.startsWith(`${m[1]}、`));
        assert.ok(section, `${point}：没有第${m[1]}章`);
        assert.ok(section.p.some((p) => p.startsWith(`${m[2]}. `)) || (section.h.startsWith('三、') && m[2] === '2'), `${point}：${m[1]}·${m[2]} 不存在`);
      }
    }
  }
});

test('查名单不像征信、只收企业全称；客服写首发包里实际有的；其余条款和 2026-11 一样', () => {
  const privacy = JSON.stringify(first.privacy);
  const user = JSON.stringify(first.user);
  assert.match(user, /只用于在政府公开发布的认定、立项、公示、入库、备案类名单中查找某个企业或机构的名称，不提供征信、信用评价或资质核验服务/);
  assert.match(privacy, /按企业名称查名单时输入的企业全称：只用于在政府公开名单中查找/);
  assert.match(privacy, /会改为提示您发邮件。/);
  assert.match(user, /每条注明发文机关、发布日期和来源网站/);
  // 段落数：用户协议第四条少 1 款（上传名单）、整个第八条（AI）去掉；
  // 指引第一部分少 3 项（批量工具、AI、加量体验），第三部分少 3 条保存期限，第四部分少 1 条（AI 模型调用）
  const count = (doc) => doc.sections.map((s) => s.p.length);
  assert.deepEqual(count(first.privacy).map((n, i) => count(full.privacy)[i] - n), [0, 3, 0, 3, 1, 0, 0, 0, 0]);
  assert.deepEqual(count(first.user), count(full.user).filter((_, i) => full.user.sections[i].h.indexOf('八、AI') !== 0).map((n, i) => (first.user.sections[i].h.startsWith('四、') ? n - 1 : n)));
});

test('草稿期间和生成脚本 tools/make_legal_first_release.js 的结果一致（不在别处手改）', () => {
  if (first.final === true) return;
  const built = build();
  assert.deepEqual(JSON.parse(JSON.stringify(first)), JSON.parse(JSON.stringify(built)));
});
