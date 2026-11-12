// D-130（创始人 11/12）：7 天内到期的截止日期改由 AI 逐条对照原文核对，不再是人工核对。
// 页面上只写"已核对原文"、不写谁核的；谁核的照实写在使用帮助（AI 关着时不出现 AI 字样，D-075）和用户协议里。
// 这里守住：给用户看的字里不再出现说"人工核对过"的旧说法。
// 不查的：订单的"人工核对中"（订单要人工核对付款，另一个意思）；旧过审版协议 legal-first-release*.js（冻结的旧版本，
// 只在 --profile 打旧包时用）；注释。
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const DIRS = ['pages', 'components', 'content', 'utils', 'custom-tab-bar'];
const SKIP = [/^content\/legal-first-release/, /^pages\/orders\//, /^utils\/pay\.js$/];
const OLD = /已人工核对|人工核对过|由人工对照原文核对|人工核对值|未经人工核对|没有经过人工核对|只对人工核对/;

function files(dir) {
  const full = path.join(ROOT, dir);
  if (!fs.existsSync(full)) return [];
  return fs.readdirSync(full, { withFileTypes: true }).flatMap((e) => {
    const rel = path.join(dir, e.name);
    if (e.isDirectory()) return files(rel);
    return /\.(js|wxml|json)$/.test(e.name) ? [rel] : [];
  });
}

// 去掉注释：wxml 的 <!-- -->，js 的 /* */ 和行尾 //（不在字符串里的才算；这里只用来找旧说法，够用）
function visible(rel, text) {
  if (rel.endsWith('.wxml')) return text.replace(/<!--[\s\S]*?-->/g, '');
  if (rel.endsWith('.json')) return text;
  return text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .map((line) => line.replace(/^\s*\/\/.*$/, '').replace(/([;,{}()\]])\s*\/\/.*$/, '$1'))
    .join('\n');
}

// 协议正文改字要升版本号、创始人定稿（D-095）：2026-12 是 D-130 之前定稿的那一版，里面"只对人工核对过的截止时间开放"
// 等首发协议 2026-12.1 定稿时一起改（docs/合规/首发协议改稿_2026-12.1_草稿.md 第三节第 7 条）。版本升上去以后这里也查 legal.js
const LEGAL_BEFORE_D130 = '2026-12';

test('D-130：给用户看的字里没有"已人工核对""人工核对过"一类旧说法', () => {
  const legalVersion = require('../content/legal.js').version;
  const hits = [];
  for (const rel of DIRS.flatMap(files)) {
    const unix = rel.split(path.sep).join('/');
    if (SKIP.some((re) => re.test(unix))) continue;
    if (unix === 'content/legal.js' && legalVersion === LEGAL_BEFORE_D130) continue;
    const text = visible(unix, fs.readFileSync(path.join(ROOT, rel), 'utf8'));
    text.split('\n').forEach((line, i) => { if (OLD.test(line)) hits.push(`${unix}:${i + 1}`); });
  }
  assert.deepEqual(hits, []);
});

test('D-130：截止标签叫"已核对原文"；使用帮助写明谁核的，那一条只在 AI 开着时显示', () => {
  const format = require('../utils/format.js');
  const due = format.dueView({ source: 'verified', date: '2099-01-01', quote: '请于 1 月 1 日前报送。' }, new Date('2098-12-29T00:00:00+08:00'));
  assert.equal(due.label, '已核对原文');
  const help = require('../content/help.js');
  const deadline = help.items.find((item) => item.key === 'deadline');
  assert.ok(!deadline.ai, '截止日期怎么看：AI 关着也显示');
  assert.doesNotMatch(deadline.a.join(''), /\bAI\b/);
  assert.match(deadline.a.join(''), /^7 天内到期的截止日期，对照通知原文核对过才显示，标“已核对原文”.*原文里写截止的那句话；这项核对不是人工做的。/);
  // 8–30 天的原文摘录照旧说明（"看不准不显示"只说 7 天内的，Codex 第 1 轮）
  assert.match(deadline.a.join(''), /原文摘录.*没有经过核对.*只有 8 到 30 天后到期的才这样显示/);
  const who = help.items.find((item) => item.key === 'deadline-check');
  assert.equal(who.ai, true, '写到 AI 的那一条只在 AI 开着时显示（D-075）');
  assert.match(who.q, /谁核对/);
  assert.match(who.a.join(''), /^7 天内到期的截止日期，由 AI 每天逐条对照通知原文核对.*不是人工核对。7 天内到期的，原文里找不到、看不准的不显示日期，写“截止时间见原文”或“可能 7 天内截止”/);
  // 紧跟在"截止日期怎么看"后面
  const keys = help.items.map((item) => item.key);
  assert.equal(keys.indexOf('deadline-check'), keys.indexOf('deadline') + 1);
});

test('D-130：AI 使用须知、工具使用说明改了字，版本号和服务端一起升', () => {
  const ai = require('../utils/ai-interpret.js');
  assert.equal(ai.NOTICE_VERSION, '2026-11-12');
  assert.match(ai.TERMS.join(''), /已对照原文核对过的值/);
  const server = fs.readFileSync(path.join(ROOT, '..', '..', 'backend', 'src', 'youju', 'ai', 'interpret.py'), 'utf8');
  assert.match(server, /^AI_NOTICE_VERSION = "2026-11-12"$/m);
  const terms = require('../content/advisor-terms.js');
  const tasks = fs.readFileSync(path.join(ROOT, '..', '..', 'backend', 'src', 'youju', 'advisor_tasks.py'), 'utf8');
  assert.equal(terms.version, 'advisor-2026-12-v2');
  assert.match(tasks, /^ADVISOR_AGREEMENT_VERSION = "advisor-2026-12-v2"$/m);
  assert.match(terms.paragraphs.join(''), /已核对原文、原文摘录或未核对；原文摘录未经核对/);
  assert.doesNotMatch(terms.paragraphs.join(''), /\bAI\b/);
});
