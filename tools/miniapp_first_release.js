// 过审版和下一版（首发包，D-091）里一点 AI 都不带（《AI 板块完整设计》第十节第 4 条；过审版总装窗口 10/25 定）。
// tools/miniapp_pack.js 打 first-release 时调用 stripFirstRelease(输出目录)，只改输出目录，不碰仓库里的 apps/miniapp：
//   1. 去掉源码里标了 first-release-cut 的段落（完整版里只是注释，不影响完整版）；
//   2. 删掉 components/ai-* 组件，各页面、组件 json 里对它们的引用和 wxml 里的标签一起去掉；
//   3. 删掉没人引用的脚本（require 不到的）和 utils 下没人 @import 的样式；
//   4. 核对剩下的：require 的文件、用到的组件都在，脚本语法能过。
// 发布检查（tools/miniapp_presubmit_check.js）再用 aiHits(包目录) 把字样扫一遍，扫到就不过。
//
// 标记写法（单独占一行，可以缩进）：
//   js、wxs：  // first-release-cut:start      ……      // first-release-cut:end
//   wxml：     <!-- first-release-cut:start -->  ……  <!-- first-release-cut:end -->
//   wxss：     /* first-release-cut:start */    ……    /* first-release-cut:end */
// 不能嵌套；同一行里还有别的代码的不算，直接报错（免得悄悄漏剪）。
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const MARK = 'first-release-cut:';
const MARKERS = {
  js: [/^\s*\/\/ first-release-cut:start\s*$/, /^\s*\/\/ first-release-cut:end\s*$/],
  wxml: [/^\s*<!-- first-release-cut:start -->\s*$/, /^\s*<!-- first-release-cut:end -->\s*$/],
  wxss: [/^\s*\/\* first-release-cut:start \*\/\s*$/, /^\s*\/\* first-release-cut:end \*\/\s*$/],
};
MARKERS.wxs = MARKERS.js;
const TEXT_EXT = ['.js', '.wxs', '.wxml', '.wxss', '.json'];
const SKIP_DIRS = new Set(['miniprogram_npm', 'node_modules']);

function walk(root, dir = root, out = []) {
  for (const name of fs.readdirSync(dir).sort()) {
    const full = path.join(dir, name);
    if (fs.statSync(full).isDirectory()) {
      if (!SKIP_DIRS.has(name)) walk(root, full, out);
    } else out.push(path.relative(root, full).split(path.sep).join('/'));
  }
  return out;
}

// 去掉标记段落；标记写错（不成对、嵌套、和代码同一行）就报错
function cutMarked(text, ext, file = '') {
  const pair = MARKERS[ext.replace(/^\./, '')];
  if (!pair || !text.includes(MARK)) return text;
  const kept = [];
  let open = 0;
  text.split('\n').forEach((line, i) => {
    const where = `${file}:${i + 1}`;
    if (pair[0].test(line)) {
      if (open) throw new Error(`${where} first-release-cut 标记不能嵌套`);
      open = i + 1;
      return;
    }
    if (pair[1].test(line)) {
      if (!open) throw new Error(`${where} first-release-cut:end 前面没有 start`);
      open = 0;
      return;
    }
    if (line.includes(MARK)) throw new Error(`${where} first-release-cut 标记要单独占一行`);
    if (!open) kept.push(line);
  });
  if (open) throw new Error(`${file}:${open} first-release-cut:start 没有对应的 end`);
  return kept.join('\n');
}

// 从 from 起找标签的结尾 ">"（跳过引号里的 ">"，如 wx:if="{{n > 0}}"），返回 ">" 后面的位置
function tagEnd(src, from, file) {
  let quote = null;
  for (let k = from; k < src.length; k++) {
    const c = src[k];
    if (quote) { if (c === quote) quote = null; continue; }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === '>') return k + 1;
  }
  throw new Error(`${file} 里有标签没有结束`);
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// 去掉 wxml 里名为 name 的组件标签：自闭合的去掉这一个标签，成对的连同里面的内容一起去掉
function removeTag(src, name, file = '') {
  const open = new RegExp(`<${escapeRe(name)}(?=[\\s/>])`, 'g');
  const any = new RegExp(`<${escapeRe(name)}(?=[\\s/>])|</${escapeRe(name)}\\s*>`, 'g');
  let out = '';
  let i = 0;
  for (;;) {
    open.lastIndex = i;
    const m = open.exec(src);
    if (!m) return out + src.slice(i);
    out += src.slice(i, m.index);
    const end = tagEnd(src, m.index + m[0].length, file);
    if (src[end - 2] === '/') { i = end; continue; }
    let depth = 1;
    let j = end;
    while (depth > 0) {
      any.lastIndex = j;
      const t = any.exec(src);
      if (!t) throw new Error(`${file} 里的 <${name}> 没有结束标签`);
      if (t[0].startsWith('</')) { depth -= 1; j = t.index + t[0].length; continue; }
      const e = tagEnd(src, t.index + t[0].length, file);
      if (src[e - 2] !== '/') depth += 1;
      j = e;
    }
    i = j;
  }
}

// 组件路径（json 里 usingComponents 的值）→ 包里的相对路径（不带扩展名）；组件库（tdesign 等）返回 null
function componentPath(jsonFile, value) {
  if (typeof value !== 'string' || !value) return null;
  if (value.startsWith('/')) return path.posix.normalize(value.slice(1));
  if (value.startsWith('.')) return path.posix.normalize(path.posix.join(path.posix.dirname(jsonFile), value));
  return null;
}

const REQUIRE = /\brequire\(\s*(['"])([^'"]+)\1\s*\)/g;
const ANY_REQUIRE = /\brequire\(/g;
function requiresOf(file, src) {
  const out = [];
  for (const m of src.matchAll(REQUIRE)) {
    if (!m[2].startsWith('.') && !m[2].startsWith('/')) continue;
    let target = m[2].startsWith('/') ? m[2].slice(1) : path.posix.join(path.posix.dirname(file), m[2]);
    target = path.posix.normalize(target);
    if (!target.endsWith('.js')) target += '.js';
    out.push(target);
  }
  return out;
}

function readJson(root, file) {
  return JSON.parse(fs.readFileSync(path.join(root, file), 'utf8'));
}

// 用到的组件：全局的、各页面和自定义底栏的，再一层层往下
function usedComponents(root, app) {
  const used = new Set();
  const stack = [];
  const visit = (jsonFile) => {
    if (!fs.existsSync(path.join(root, jsonFile))) return;
    const map = readJson(root, jsonFile).usingComponents || {};
    for (const value of Object.values(map)) {
      const comp = componentPath(jsonFile, value);
      if (comp && !used.has(comp)) { used.add(comp); stack.push(`${comp}.json`); }
    }
  };
  visit('app.json');
  for (const page of app.pages) visit(`${page}.json`);
  if (app.tabBar && app.tabBar.custom) visit('custom-tab-bar/index.json');
  while (stack.length) visit(stack.pop());
  return used;
}

function stripFirstRelease(root) {
  // 1. 标记段落
  for (const file of walk(root)) {
    const ext = path.extname(file);
    if (!['.js', '.wxs', '.wxml', '.wxss'].includes(ext)) continue;
    const full = path.join(root, file);
    const text = fs.readFileSync(full, 'utf8');
    const next = cutMarked(text, ext, file);
    if (next !== text) fs.writeFileSync(full, next);
  }
  // 2. AI 组件：目录删掉，json 里的引用和 wxml 里的标签去掉
  const compDir = path.join(root, 'components');
  const aiComponents = fs.existsSync(compDir)
    ? fs.readdirSync(compDir).filter((name) => name.startsWith('ai-')).map((name) => `components/${name}/index`) : [];
  for (const comp of aiComponents) fs.rmSync(path.join(root, path.posix.dirname(comp)), { recursive: true, force: true });
  const removedTags = new Map();
  let globalTags = [];
  for (const file of walk(root).filter((f) => f.endsWith('.json'))) {
    const data = readJson(root, file);
    const map = data.usingComponents;
    if (!map || typeof map !== 'object') continue;
    const tags = Object.keys(map).filter((tag) => aiComponents.includes(componentPath(file, map[tag])));
    if (!tags.length) continue;
    for (const tag of tags) delete map[tag];
    fs.writeFileSync(path.join(root, file), `${JSON.stringify(data, null, 2)}\n`);
    if (file === 'app.json') globalTags = tags;
    else removedTags.set(file.replace(/\.json$/, '.wxml'), tags);
  }
  for (const file of walk(root).filter((f) => f.endsWith('.wxml'))) {
    const tags = [...new Set(globalTags.concat(removedTags.get(file) || []))];
    if (!tags.length) continue;
    const full = path.join(root, file);
    const text = fs.readFileSync(full, 'utf8');
    const next = tags.reduce((src, tag) => removeTag(src, tag, file), text);
    if (next !== text) fs.writeFileSync(full, next);
  }
  // 3. 没人引用的脚本、组件、样式
  const app = readJson(root, 'app.json');
  const components = usedComponents(root, app);
  const entries = ['app.js', ...app.pages.map((p) => `${p}.js`), ...[...components].map((c) => `${c}.js`)];
  if (app.tabBar && app.tabBar.custom) entries.push('custom-tab-bar/index.js');
  const reachable = new Set();
  const stack = entries.slice();
  while (stack.length) {
    const file = stack.pop();
    if (reachable.has(file) || !fs.existsSync(path.join(root, file))) continue;
    reachable.add(file);
    const src = fs.readFileSync(path.join(root, file), 'utf8');
    const found = requiresOf(file, src);
    // 不是写死路径的 require（拼出来的路径）判断不了用到哪些文件：直接报错，不猜
    const literal = [...src.matchAll(REQUIRE)].length;
    if ([...src.matchAll(ANY_REQUIRE)].length !== literal) throw new Error(`${file} 里有不是写死路径的 require，打包工具判断不了它用到哪些文件`);
    stack.push(...found);
  }
  const pruned = [];
  for (const file of walk(root)) {
    const top = file.split('/')[0];
    if (file.endsWith('.js') && ['utils', 'content', 'mock'].includes(top) && !reachable.has(file)) {
      fs.rmSync(path.join(root, file));
      pruned.push(file);
    }
  }
  for (const dir of fs.existsSync(compDir) ? fs.readdirSync(compDir).sort() : []) {
    if (!components.has(`components/${dir}/index`)) {
      fs.rmSync(path.join(compDir, dir), { recursive: true, force: true });
      pruned.push(`components/${dir}`);
    }
  }
  const imported = new Set();
  const IMPORT = /@import\s+(?:url\(\s*)?(['"])([^'"]+)\1/g;
  for (const file of walk(root).filter((f) => f.endsWith('.wxss'))) {
    const src = fs.readFileSync(path.join(root, file), 'utf8');
    for (const m of src.matchAll(IMPORT)) {
      imported.add(path.posix.normalize(m[2].startsWith('/') ? m[2].slice(1) : path.posix.join(path.posix.dirname(file), m[2])));
    }
  }
  for (const file of walk(root)) {
    if (file.startsWith('utils/') && file.endsWith('.wxss') && !imported.has(file)) {
      fs.rmSync(path.join(root, file));
      pruned.push(file);
    }
  }
  // 4. 核对
  const left = walk(root);
  for (const file of left.filter((f) => f.endsWith('.js'))) {
    const src = fs.readFileSync(path.join(root, file), 'utf8');
    try {
      new vm.Script(`(function (require, module, exports) {\n${src}\n})`, { filename: file });
    } catch (err) {
      throw new Error(`${file} 剪完以后语法不对：${err.message}`);
    }
    for (const target of requiresOf(file, src)) {
      if (!fs.existsSync(path.join(root, target))) throw new Error(`${file} 引用的 ${target} 不在包里`);
    }
  }
  for (const file of left.filter((f) => f.endsWith('.json'))) {
    const map = readJson(root, file).usingComponents || {};
    for (const value of Object.values(map)) {
      const comp = componentPath(file, value);
      if (comp && !fs.existsSync(path.join(root, `${comp}.js`))) throw new Error(`${file} 用到的组件 ${comp} 不在包里`);
    }
  }
  return { aiComponents, pruned };
}

// 过审版和下一版的包里不能有的字样（第十节第 4 条）。只扫文本文件，不扫组件库（miniprogram_npm）；
// 字体等 data: 内嵌数据先去掉（base64 里会碰巧出现 AI 两个字母）。公司全称里有"智能"二字，所以不单查"智能"。
const AI_PATTERNS = [
  ['AI', /(?<![A-Za-z])AI(?![A-Za-z])/],
  ['中文说法', /人工智能|大模型|生成式|深度合成|智能体|混元|DeepSeek|deepseek/],
  ['ai- 开头的名字', /(?<![A-Za-z0-9_-])ai-[a-z]/],
  ['ai 开头的变量名', /(?<![A-Za-z0-9_])ai(?:[A-Z]|_[a-z])/],
  ['带 Ai 的变量名', /(?<=[a-z])Ai(?![a-z])/],
  ['单独的 ai', /(?<![A-Za-z0-9_-])ai(?![A-Za-z0-9_-])/],
  ['解读、对照页面', /advisor-interpret|advisor-clause|\/advisor\/mode/],
];

function aiHits(root, limit = 50) {
  const hits = [];
  for (const file of walk(root)) {
    if (/(^|\/)ai(?:[-_.]|$)/.test(file)) hits.push({ file, line: 0, what: '文件名' });
    if (!TEXT_EXT.includes(path.extname(file))) continue;
    const lines = fs.readFileSync(path.join(root, file), 'utf8').replace(/data:[^)'"\s]+/g, 'data:').split('\n');
    lines.forEach((line, i) => {
      const found = AI_PATTERNS.find(([, re]) => re.test(line));
      if (found) hits.push({ file, line: i + 1, what: found[0] });
    });
    if (hits.length >= limit) break;
  }
  return hits.slice(0, limit);
}

module.exports = { stripFirstRelease, cutMarked, removeTag, aiHits, AI_PATTERNS };
