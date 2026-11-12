// 首发协议 2026-12.1（D-134：创始人 11/12 对首发协议改稿 3 件回"都按推荐"）：在 2026-12（冻结在
// tools/release/legal-2026-12.js）上只改了改稿（docs/合规/首发协议改稿_2026-12.1_草稿.md）第三节那 11 处——
// D-130 截止日期由 AI 对照原文核对、不是人工核对；D-127 首发就用自己的服务器，隐私保护指引里写"微信云托管"的几处
// 照自建服务器改。守住：版本、定稿、指纹；别的段落一字不差；D-123"您保存的通知"这一版不放。
const test = require('node:test');
const assert = require('node:assert/strict');
const legal = require('../content/legal.js');
const prev = require('../../../tools/release/legal-2026-12.js');
const {
  SERVER_AI_TERMS_VERSION, SERVER_WITHOUT_TOOLS_TERMS, FINGERPRINTS, agreementFingerprint,
} = require('../../../tools/miniapp_presubmit_check.js');

test('2026-12.1：定稿、指纹已登记；排在 2026-12 后面，后台照旧认它含批量工具和 AI 条款', () => {
  assert.equal(legal.version, '2026-12.1');
  assert.equal(legal.final, true);
  assert.match(legal.status, /D-134/);
  assert.ok(legal.version > prev.version);
  assert.equal(FINGERPRINTS[legal.version], agreementFingerprint(legal));
  assert.equal(FINGERPRINTS[prev.version], agreementFingerprint(prev), '冻结的 2026-12 没被改过');
  assert.ok(!SERVER_WITHOUT_TOOLS_TERMS.includes(legal.version) && legal.version >= SERVER_AI_TERMS_VERSION);
  assert.deepEqual(legal.operator, prev.operator);
  assert.equal(legal.disclaimer, prev.disclaimer);
  for (const doc of ['user', 'privacy']) {
    assert.equal(legal[doc].title, prev[doc].title);
    assert.deepEqual(legal[doc].keyParagraphs, prev[doc].keyParagraphs);
    // 章节标题、每节段落数都没变：只改字，不加段、不删段
    assert.deepEqual(legal[doc].sections.map((s) => [s.h, s.p.length]), prev[doc].sections.map((s) => [s.h, s.p.length]));
  }
});

// 改稿第三节：[哪里, 2026-12 里改掉的说法（null 是只在末尾加一句）, 2026-12.1 里新的说法]
const CHANGED = [
  ['user.keyPoints', '我们合理核对，因我们故意', '我们合理核对，7 天内到期的截止日期由 AI 对照原文核对、不是人工核对；因我们故意'],
  ['user', '注明出处，但政府原文', '7 天内到期的截止日期由 AI 逐条对照通知原文核对，不是人工核对；核对过的才显示，标“已核对原文”'],
  ['privacy.keyPoints', '腾讯云和深度求索受我们委托', '腾讯云（云服务器，加密备份和存档的存储）和深度求索受我们委托'],
  ['privacy', '本小程序通过微信云托管调用我们的服务', '本小程序调用微信登录接口取得一个一次性的登录码'],
  ['privacy', '微信云托管转发请求时附带的来源 IP 地址', '连接我们服务器的来源 IP 地址'],
  ['privacy', null, '登录凭证（见第 2 项）也保存在您手机的本地缓存里，7 天后失效；您删除小程序或清除缓存时一并删除。'],
  ['privacy', '只对人工核对过的截止时间开放', '只对已核对原文的截止时间开放'],
  ['privacy', '微信云托管的云数据库和对象存储', '我们租用的云服务器上的数据库和硬盘'],
  ['privacy', '每天备份一次，保留 30 天', '每天备份一次，服务器上一份，加密后在腾讯云对象存储和我们公司的电脑上各存一份，都保留 30 天'],
  ['privacy', '微信云托管的通道加密传输', '数据经 HTTPS 加密传输；我们的服务只对外开放小程序要用的接口，管理后台不对公网开放'],
  ['privacy', '① 小程序运行，提供微信用户标识', '① 小程序运行和登录，提供微信用户标识'],
  ['privacy', '服务器（微信云托管）和数据库托管', '云服务器（轻量应用服务器）和对象存储'],
];

function differences() {
  const out = [];
  for (const doc of ['user', 'privacy']) {
    legal[doc].keyPoints.forEach((now, i) => {
      if (now !== prev[doc].keyPoints[i]) out.push({ where: `${doc}.keyPoints`, old: prev[doc].keyPoints[i], now });
    });
    legal[doc].sections.forEach((s, i) => s.p.forEach((now, j) => {
      const old = prev[doc].sections[i].p[j];
      if (now !== old) out.push({ where: doc, old, now });
    }));
  }
  return out;
}

test('2026-12.1：只改了改稿第三节那 11 处（12 段），别的段落和 2026-12 一字不差', () => {
  const found = differences();
  assert.equal(found.length, CHANGED.length, found.map((d) => d.now.slice(0, 30)).join(' / '));
  for (const [where, old, now] of CHANGED) {
    const hits = found.filter((d) => d.where === where && d.now.includes(now));
    assert.equal(hits.length, 1, `${where}：${now}`);
    const [hit] = hits;
    if (old === null) {
      assert.ok(hit.now.startsWith(hit.old) && hit.now.endsWith(now), `${where}：只在末尾加一句`);
    } else {
      assert.ok(hit.old.includes(old) && !hit.now.includes(old), `${where}：${old}`);
    }
  }
});

test('2026-12.1：正文里不再有"微信云托管"和"人工核对过"；不放 D-123"您保存的通知"', () => {
  const text = JSON.stringify([legal.user, legal.privacy, legal.disclaimer]);
  assert.doesNotMatch(text, /微信云托管|人工核对过|已人工核对/);
  assert.doesNotMatch(text, /您保存的通知|我的保存/);
  // 截止日期由 AI 核对：重点条款和二·3 都写了；三·2 保存期限没改（D-127 的旧库存档照现行期限）
  assert.match(legal.user.keyPoints.join(''), /7 天内到期的截止日期由 AI 对照原文核对、不是人工核对/);
  assert.match(text, /更换服务器时从原系统导出、按第 2 项的期限还要保存的存档（例如订单记录）/);
});
