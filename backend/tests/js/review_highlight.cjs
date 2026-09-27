// 核对台证据日期高亮的用例：从 review.html 里取出正则和 evidenceNode 直接运行（pytest 通过 test_review_highlight.py 调用）
// 结果写法："实:日期"=标黄，"虚:日期"=虚线框（原文这里没写年份），"不高亮"
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '../../src/youju/admin/review.html'), 'utf8');
const js = html.split('<script>')[1].split('</script>')[0];
new Function(js); // 整段脚本语法检查
const DATE_RE = eval(js.match(/const DATE_RE = (\/.*\/g);/)[1]);
const YEAR_RE = eval(js.match(/const YEAR_RE = (\/.*\/g);/)[1]);
const src = js.slice(js.indexOf('  function evidenceNode'), js.indexOf('  function setCount'));
const el = (tag, cls, text) => ({ tag, className: cls || '', textContent: text, append(...parts) { this.parts = parts; } });
const evidenceNode = new Function('DATE_RE', 'YEAR_RE', 'el', `${src}; return evidenceNode;`)(DATE_RE, YEAR_RE, el);

function show(text, date) {
  const mark = (evidenceNode(text, date).parts || []).find((p) => p && p.tag === 'mark');
  if (!mark) return '不高亮';
  return `${mark.className === 'unsure' ? '虚' : '实'}:${mark.textContent}`;
}

const cases = [
  ['2026-10-09', '截至2026年10月09日17:00', '实:2026年10月09日'],
  ['2026-10-09', '10月9日前报送', '实:10月9日'],
  ['2026-10-09', '于2026-10-09提交', '实:2026-10-09'],
  ['2026-10-09', '10-9截止', '实:10-9'],
  ['2026-10-09', '2025年10月9日前', '不高亮'],
  ['2026-10-09', '2025年 10月9日前', '不高亮'],
  ['2026-10-09', '2025-10-9', '不高亮'],
  ['2026-10-09', '2025年10-9', '不高亮'],
  ['2026-10-09', '2026年10-9', '实:2026年10-9'],
  ['2026-10-09', '10月19日', '不高亮'],
  ['2026-10-09', '截止日期-2026-10-9', '实:2026-10-9'],
  ['2026-10-09', '截止日期：2026.10.9 17:00', '实:2026.10.9'],
  ['2026-10-09', '2025年10月9日至2026年10月9日', '实:2026年10月9日'],
  ['2026-10-09', '10月9日完成初审，2026年10月9日前报送', '实:2026年10月9日'],
  ['2026-10-09', '第110月9日', '不高亮'],
  ['2026-10-09', '2026 年 10 月 9 日', '实:2026 年 10 月 9 日'],
  ['2026-10-09', '2026/10/9', '实:2026/10/9'],
  ['2026-10-09', '2025-10-9至2026-10-9', '实:2026-10-9'],
  ['2026-10-09', '10月8日至10月9日', '实:10月9日'],
  ['2026-10-09', '联系电话0531-86-9，截止10月9日', '实:10月9日'],
  // 没写年份、句中别处写了年份的：只用虚线框标位置，年份由审核人判断
  ['2026-10-09', '2025年10月8日至10月9日', '虚:10月9日'],
  ['2026-10-09', '2026年10月8日至10月9日', '虚:10月9日'],
  ['2025-01-09', '2025年12月31日至次年1月9日', '虚:1月9日'],
  ['2026-01-09', '2025年12月31日至次年1月9日', '虚:1月9日'],
  ['2026-10-09', '依据2025年9月30日通知，2026年度申报10月9日截止', '虚:10月9日'],
  ['2026-02-09', '2025年12月31日至1月9日，2月9日前报送', '虚:2月9日'],
  ['2026-10-09', '2025年12月31日到期项目，申报截止10月9日', '虚:10月9日'],
  ['2026-10-09', '2026年度项目申报截止10月9日', '虚:10月9日'],
];

let bad = 0;
for (const [cand, text, want] of cases) {
  const got = show(text, cand);
  if (got !== want) bad += 1;
  console.log(got === want ? '✓' : '✗', cand, text, '→', got);
}
console.log(bad ? `${bad} 个不符` : `全部 ${cases.length} 个符合`);
process.exit(bad ? 1 : 0);
