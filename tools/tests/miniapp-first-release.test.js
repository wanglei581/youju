// 过审版和下一版的包里一点 AI 都不带（《AI 板块完整设计》第十节第 4 条；过审版总装窗口 10/25 定）：
// tools/miniapp_first_release.js 剪掉标了 first-release-cut 的段落、删掉 AI 组件和没人用的模块，发布检查再扫一遍字样。
// 完整版（带 AI 的那份）不剪；仓库里的 apps/miniapp 不动。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pack } = require('../miniapp_pack.js');
const { cutMarked, removeTag, aiHits } = require('../miniapp_first_release.js');
const { inspect } = require('../miniapp_presubmit_check.js');

const SOURCE = path.join(__dirname, '..', '..', 'apps', 'miniapp');
const tmp = (name) => fs.mkdtempSync(path.join(os.tmpdir(), `youju-${name}-`));
// 构建好的组件库（miniprogram_npm）仓库里不带：测试里放一个假的
function fakeNpm() {
  const dir = tmp('npm');
  fs.mkdirSync(path.join(dir, 'miniprogram_npm', 'tdesign-miniprogram', 'popup'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'miniprogram_npm', 'tdesign-miniprogram', 'popup', 'popup.js'), 'Component({});\n');
  return dir;
}
function files(root, dir = root, out = []) {
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    if (fs.statSync(full).isDirectory()) { if (name !== 'miniprogram_npm') files(root, full, out); } else out.push(path.relative(root, full));
  }
  return out;
}
const fresh = (require_) => { delete require.cache[require.resolve(require_)]; return require(require_); };

test('打好的过审版包：没有 AI 组件、AI 模块、剪切标记和 AI 字样，发布检查这一项过；仓库里的源码不动', () => {
  const tabBefore = fs.readFileSync(path.join(SOURCE, 'custom-tab-bar', 'index.js'), 'utf8');
  const out = path.join(tmp('first'), 'out');
  const { result, stripped } = pack({ out, profile: 'first-release', mode: 'prepare', npmFrom: fakeNpm() });
  assert.deepEqual([...stripped.aiComponents].sort(),
    ['components/ai-button/index', 'components/ai-entry/index', 'components/ai-hint/index', 'components/ai-only/index']);
  for (const gone of ['components/ai-button', 'components/ai-entry', 'utils/ai-access.js', 'utils/ai-reasons.js', 'utils/ai-interpret.js',
    'utils/advisor.js', 'utils/advisor.wxss', 'utils/tool-context.js', 'content/help.js']) {
    assert.equal(fs.existsSync(path.join(out, gone)), false, gone);
  }
  assert.deepEqual(aiHits(out), []);
  const byId = Object.fromEntries(result.checks.map((c) => [c.id, c.state]));
  assert.equal(byId['no-ai'], 'pass');
  for (const file of files(out)) {
    if (/\.(js|wxml|wxss|json)$/.test(file)) assert.doesNotMatch(fs.readFileSync(path.join(out, file), 'utf8'), /first-release-cut/, file);
  }
  // 底栏没有 AI 那一格；页面 json 不再引用 AI 组件，wxml 里也没有它们的标签
  assert.doesNotMatch(fs.readFileSync(path.join(out, 'custom-tab-bar', 'index.js'), 'utf8'), /pages\/ai\//);
  for (const page of ['home', 'search', 'detail', 'report']) {
    const json = JSON.parse(fs.readFileSync(path.join(out, 'pages', page, 'index.json'), 'utf8'));
    assert.ok(!Object.keys(json.usingComponents || {}).some((k) => k.startsWith('ai-')), page);
    assert.doesNotMatch(fs.readFileSync(path.join(out, 'pages', page, 'index.wxml'), 'utf8'), /<ai-/, page);
  }
  assert.doesNotMatch(fs.readFileSync(path.join(out, 'components', 'doc-row', 'index.wxml'), 'utf8'), /<ai-/);
  // 我的数据还在、批量工具的类型名照常有（从 utils/task-kinds.js 来，不经过带 AI 的 utils/advisor.js）
  assert.ok(fs.existsSync(path.join(out, 'utils', 'task-kinds.js')));
  assert.match(fs.readFileSync(path.join(out, 'pages', 'my-data', 'index.js'), 'utf8'), /utils\/task-kinds\.js/);
  // 协议：正文、版本号和仓库里的过审版协议一字不差，只是开头的说明注释不进包
  const packed = fs.readFileSync(path.join(out, 'content', 'legal.js'), 'utf8');
  assert.ok(packed.startsWith('module.exports'));
  assert.deepEqual(fresh(path.join(out, 'content', 'legal.js')), fresh(path.join(SOURCE, 'content', 'legal-first-release.js')));
  // 仓库里的源码不动：完整版照常带 AI
  assert.equal(fs.readFileSync(path.join(SOURCE, 'custom-tab-bar', 'index.js'), 'utf8'), tabBefore);
  assert.match(tabBefore, /pages\/ai\/index/);
  assert.ok(fs.existsSync(path.join(SOURCE, 'components', 'ai-button', 'index.js')));

  // 发布检查：包里又出现 AI 字样就不过；只列文件、行号和类型，不输出那一行的内容
  fs.writeFileSync(path.join(out, 'pages', 'home', 'extra.js'), "module.exports = '再写一句 AI 的话';\n");
  const load = (file) => fresh(path.join(out, file));
  const again = inspect({ legal: load('content/legal.js'), config: load('utils/config.js'),
    app: JSON.parse(fs.readFileSync(path.join(out, 'app.json'), 'utf8')),
    project: JSON.parse(fs.readFileSync(path.join(out, 'project.config.json'), 'utf8')), mode: 'prepare', profile: 'first-release', root: out });
  const noAi = again.checks.find((c) => c.id === 'no-ai');
  assert.equal(noAi.state, 'fail');
  assert.match(noAi.fix, /pages\/home\/extra\.js:1（AI）/);
  assert.doesNotMatch(noAi.fix, /再写一句/);
  assert.equal(again.ok, false);
  // 不给包目录时（只传了配置）不查这一项
  const noRoot = inspect({ legal: load('content/legal.js'), config: load('utils/config.js'),
    app: JSON.parse(fs.readFileSync(path.join(out, 'app.json'), 'utf8')),
    project: JSON.parse(fs.readFileSync(path.join(out, 'project.config.json'), 'utf8')), mode: 'prepare', profile: 'first-release' });
  assert.equal(noRoot.checks.some((c) => c.id === 'no-ai'), false);
});

test('完整版不剪：AI 组件、模块都在，标记原样留着（只是注释）', () => {
  const out = path.join(tmp('full'), 'out');
  const { stripped } = pack({ out, profile: 'complete-release', mode: 'prepare', npmFrom: fakeNpm() });
  assert.equal(stripped, null);
  for (const kept of ['components/ai-button/index.js', 'utils/ai-access.js', 'utils/advisor.js', 'content/help.js']) {
    assert.ok(fs.existsSync(path.join(out, kept)), kept);
  }
  assert.match(fs.readFileSync(path.join(out, 'custom-tab-bar', 'index.js'), 'utf8'), /first-release-cut:start/);
});

test('剪标记：成对的整段去掉；不成对、嵌套、和代码同一行都报错', () => {
  const js = ['a();', '  // first-release-cut:start', '  b();', '  // first-release-cut:end', 'c();'].join('\n');
  assert.equal(cutMarked(js, '.js', 'x.js'), 'a();\nc();');
  assert.equal(cutMarked(['<view/>', '  <!-- first-release-cut:start -->', '<x/>', '  <!-- first-release-cut:end -->'].join('\n'), '.wxml'), '<view/>');
  assert.equal(cutMarked(['.a {}', '/* first-release-cut:start */', '.b {}', '/* first-release-cut:end */', ''].join('\n'), '.wxss'), '.a {}\n');
  assert.equal(cutMarked('没有标记的照原样', '.js'), '没有标记的照原样');
  assert.throws(() => cutMarked('// first-release-cut:start\nx();', '.js', 'x.js'), /x\.js:1 .*没有对应的 end/);
  assert.throws(() => cutMarked('x();\n// first-release-cut:end', '.js', 'x.js'), /x\.js:2 .*前面没有 start/);
  assert.throws(() => cutMarked('// first-release-cut:start\n// first-release-cut:start\n// first-release-cut:end', '.js'), /不能嵌套/);
  assert.throws(() => cutMarked('a(); // first-release-cut:start', '.js'), /单独占一行/);
  // 各用各的注释写法：js 的写法放进 wxml 不认，直接报错
  assert.throws(() => cutMarked('// first-release-cut:start', '.wxml'), /单独占一行/);
});

test('去组件标签：自闭合的去掉这一个；引号里的 > 不算结尾；成对的连里面一起去掉；名字只是开头相同的不动', () => {
  const src = '<view><text>a</text><ai-button doc-id="{{d.id}}" show="{{n > 0}}" /></view>\n'
    + '<ai-entry x="1"><ai-entry/><text>b</text></ai-entry><text>c</text>\n<ai-button-x/>';
  assert.equal(removeTag(removeTag(src, 'ai-button'), 'ai-entry'), '<view><text>a</text></view>\n<text>c</text>\n<ai-button-x/>');
  assert.throws(() => removeTag('<ai-entry><text/>', 'ai-entry', 'p.wxml'), /p\.wxml 里的 <ai-entry> 没有结束标签/);
});

test('字样检查：大写 AI、中文说法、ai- 名字、ai 开头或带 Ai 的变量名、单独的 ai 都算；detail、email 这些词和内嵌字体数据不算', () => {
  const dir = tmp('scan');
  const write = (file, text) => {
    fs.mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
    fs.writeFileSync(path.join(dir, file), text);
  };
  write('pages/a/index.js', "const detail = 1; const email = 'x'; const MAX_WAIT = 3; const UNAVAILABLE = 4;\n");
  write('components/icon/index.wxss', "@font-face { src: url('data:font/woff;base64,AAAI0AI+AI/'); }\n");
  write('content/legal.js', "module.exports = { name: '某某软件智能科技有限公司' };\n");
  assert.deepEqual(aiHits(dir), []);
  for (const [text, what] of [["'AI 解读'", 'AI'], ["'大模型'", '中文说法'], ["'<ai-button/>'", 'ai- 开头的名字'], ['{ aiOnly: false }', 'ai 开头的变量名'],
    ['this.removeAi', '带 Ai 的变量名'], ["has('ai')", '单独的 ai'], ["'/api/v1/advisor/mode'", '解读、对照页面']]) {
    write('pages/b/index.js', `x(${text});\n`);
    const hits = aiHits(dir);
    assert.equal(hits.length, 1, text);
    assert.deepEqual(hits[0], { file: 'pages/b/index.js', line: 1, what }, text);
  }
  write('pages/b/index.js', 'x();\n');
  write('utils/ai-x.js', 'x();\n');
  assert.deepEqual(aiHits(dir), [{ file: 'utils/ai-x.js', line: 0, what: '文件名' }]);
});
