#!/usr/bin/env node
// 按版本打小程序代码包（D-091：再提审只交"过审版"，按首发版 16 页打，不含批量工具、付费和 AI 页面）。
// 从 apps/miniapp 复制一份到 --out 指定的新目录：
//   1. app.json 只留这个版本的页面，底栏只留这些页面里的；
//   2. 删掉不在包里的页面目录；
//   3. 按包里实际的页面重写 utils/package-pages.js——页面上指向包外页面的入口（工具中心、我的记录、
//      购买、订单、使用帮助、AI）靠它判断后不显示（utils/pages.js）；
//   4. 过审版换上不含 AI 条款的协议（content/legal-first-release.js，D-093 ⑤），开头的说明注释不进包；
//   5. 过审版剔掉 AI 组件、模块和源码里标了 first-release-cut 的段落（tools/miniapp_first_release.js，
//      《AI 板块完整设计》第十节第 4 条：过审版和下一版的包里一点 AI 都不带）；
//   6. 用发布检查（tools/miniapp_presubmit_check.js）检查打好的包，没过就不算打好；过审版另查包里有没有 AI 字样。
// 不上传、不提交审核。上传前在开发者工具里打开输出目录，实际点一遍审核员会走的路。
// 用法：node tools/miniapp_pack.js --out <新目录> [--profile first-release] [--mode prepare|submit] [--npm-from <小程序目录>]
// 搜索页用到组件库（tdesign-miniprogram），要有构建好的 miniprogram_npm：仓库里不带（.gitignore），
// 在开发者工具里对 apps/miniapp 点"构建 npm"，或用 --npm-from 指定一个已经构建好的小程序目录（例如 miniapp-release 的）
const fs = require('node:fs');
const path = require('node:path');
const { inspect, pagesFor, PROFILES } = require('./miniapp_presubmit_check.js');
const { stripFirstRelease } = require('./miniapp_first_release.js');

const SOURCE = path.join(__dirname, '..', 'apps', 'miniapp');
// 不进代码包的（和 project.config.json 的 packOptions.ignore 一致，另加本机的开发者工具设置）
const SKIP = new Set(['tests', 'node_modules', 'README.md', 'package-lock.json', 'project.private.config.json', '.DS_Store']);

function copyTree(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    if (SKIP.has(entry.name)) continue;
    const src = path.join(from, entry.name);
    const dst = path.join(to, entry.name);
    if (entry.isDirectory()) copyTree(src, dst);
    else if (entry.isFile()) fs.copyFileSync(src, dst);
    // 符号链接等不复制：包里只放普通文件
  }
}

function pageListSource(pages) {
  return [
    '// 这个代码包里有哪些页面（tools/miniapp_pack.js 打包时按 app.json 写的，不要手改）。',
    '// 页面上指向包里没有的页面的入口，用 utils/pages.js 判断后不显示。',
    'module.exports = [',
    ...pages.map((p) => `  '${p}',`),
    '];',
    '',
  ].join('\n');
}

// 页面、组件、app.json 里引用的组件库组件（不是 / 或 . 开头的路径）
function npmComponents(dir) {
  const found = [];
  const walk = (folder) => {
    for (const entry of fs.readdirSync(folder, { withFileTypes: true })) {
      const full = path.join(folder, entry.name);
      if (entry.isDirectory()) { if (entry.name !== 'miniprogram_npm') walk(full); continue; }
      if (!entry.name.endsWith('.json')) continue;
      let json;
      try { json = JSON.parse(fs.readFileSync(full, 'utf8')); } catch (e) { continue; }
      for (const value of Object.values((json && json.usingComponents) || {})) {
        if (typeof value === 'string' && !/^[/.]/.test(value)) found.push(`${path.relative(dir, full)}：${value}`);
      }
    }
  };
  walk(dir);
  return found;
}

// 打包：返回 { out, pages, removed, result }；result 是发布检查的结果
function pack({ out, profile = 'first-release', mode = 'prepare', source = SOURCE, npmFrom }) {
  if (!PROFILES.includes(profile) || !['prepare', 'submit'].includes(mode)) throw new Error('参数不支持');
  if (!out) throw new Error('要用 --out 指定输出目录');
  const target = path.resolve(out);
  const inside = path.relative(path.resolve(source), target);
  if (!inside.startsWith('..') && !path.isAbsolute(inside)) throw new Error('输出目录不能放在 apps/miniapp 里面');
  if (fs.existsSync(target) && fs.readdirSync(target).length) throw new Error('输出目录已存在且不是空的，换一个新目录');

  const app = JSON.parse(fs.readFileSync(path.join(source, 'app.json'), 'utf8'));
  const wanted = pagesFor(profile);
  const missing = wanted.filter((p) => !app.pages.includes(p));
  if (missing.length) throw new Error(`仓库的 app.json 里没有这些页面：${missing.join('、')}`);
  if (app.subpackages || app.subPackages || app.preloadRule) throw new Error('app.json 有分包或预加载设置，打包工具还不支持，先核对');

  // 组件库：打包前先确认有构建好的（没有就不动输出目录）
  const built = path.join(npmFrom ? path.resolve(npmFrom) : source, 'miniprogram_npm');
  if (npmFrom && !fs.existsSync(built)) throw new Error(`${npmFrom} 里没有 miniprogram_npm`);
  const npm = npmComponents(source);
  if (npm.length && !fs.existsSync(built)) {
    throw new Error(`包里用到组件库（${npm[0]}），但没有构建好的 miniprogram_npm：先在开发者工具里对 apps/miniapp 点"构建 npm"，或加 --npm-from <已构建的小程序目录>`);
  }

  copyTree(source, target);
  // 保持 app.json 里原来的顺序（第一页是首页）
  const pages = app.pages.filter((p) => wanted.includes(p));
  const removed = app.pages.filter((p) => !wanted.includes(p));
  const next = { ...app, pages };
  if (app.tabBar && Array.isArray(app.tabBar.list)) {
    next.tabBar = { ...app.tabBar, list: app.tabBar.list.filter((item) => pages.includes(item.pagePath)) };
  }
  fs.writeFileSync(path.join(target, 'app.json'), `${JSON.stringify(next, null, 2)}\n`);
  for (const page of removed) {
    fs.rmSync(path.join(target, path.dirname(page)), { recursive: true, force: true });
  }
  fs.writeFileSync(path.join(target, 'utils', 'package-pages.js'), pageListSource(pages));
  // 协议：过审版用不含 AI 条款的那一份（content/legal-first-release.js，D-093 ⑤），替换掉 content/legal.js；
  // 别的版本用 content/legal.js，包里不留过审版那一份
  const firstTerms = path.join(target, 'content', 'legal-first-release.js');
  if (profile === 'first-release') {
    if (!fs.existsSync(firstTerms)) throw new Error('没有 content/legal-first-release.js（过审版协议）');
    // 开头的说明注释（写给开发看的，提到包里没有的功能）不进包；正文、版本号一字不动
    const text = fs.readFileSync(firstTerms, 'utf8');
    const body = text.replace(/^(?:[ \t]*\/\/[^\n]*\n|[ \t]*\n)*/, '');
    if (!body.startsWith('module.exports')) throw new Error('content/legal-first-release.js 开头的注释后面应当直接是 module.exports');
    fs.writeFileSync(path.join(target, 'content', 'legal.js'), body);
    fs.rmSync(firstTerms);
  } else {
    fs.rmSync(firstTerms, { force: true });
  }
  if (npmFrom) {
    fs.rmSync(path.join(target, 'miniprogram_npm'), { recursive: true, force: true });
    copyTree(built, path.join(target, 'miniprogram_npm'));
  }

  // 过审版：剔掉 AI（完整版不动）
  const stripped = profile === 'first-release' ? stripFirstRelease(target) : null;

  const load = (file) => {
    const full = path.join(target, file);
    delete require.cache[require.resolve(full)];
    return require(full);
  };
  const complete = profile.startsWith('complete-');
  const result = inspect({
    legal: load('content/legal.js'),
    config: load('utils/config.js'),
    app: JSON.parse(fs.readFileSync(path.join(target, 'app.json'), 'utf8')),
    project: JSON.parse(fs.readFileSync(path.join(target, 'project.config.json'), 'utf8')),
    toolTerms: complete ? load('content/advisor-terms.js') : undefined,
    payTerms: complete ? load('content/pay-terms.js') : undefined,
    mode,
    profile,
    root: target,
  });
  return { out: target, pages, removed, stripped, result };
}

function main(args) {
  const options = { profile: 'first-release', mode: 'prepare' };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--out' && args[i + 1]) options.out = args[++i];
    else if (args[i] === '--profile' && args[i + 1]) options.profile = args[++i];
    else if (args[i] === '--mode' && args[i + 1]) options.mode = args[++i];
    else if (args[i] === '--npm-from' && args[i + 1]) options.npmFrom = args[++i];
    else throw new Error('用法：node tools/miniapp_pack.js --out <新目录> [--profile first-release] [--mode prepare|submit] [--npm-from <小程序目录>]');
  }
  const { out, pages, removed, result } = pack(options);
  console.log(`已打包到 ${out}：${pages.length} 页（去掉 ${removed.length} 页）`);
  result.checks.forEach((c) => console.log(`${{ pass: '通过', warning: '提示', fail: '没过' }[c.state]}  ${c.name}${c.state === 'pass' ? '' : `\n      → ${c.fix}`}`));
  console.log(result.ok ? '\n发布检查通过（仍要在开发者工具里打开这个目录实际走一遍）' : '\n发布检查没过，这个包不能用');
  return result.ok ? 0 : 1;
}

if (require.main === module) {
  try { process.exitCode = main(process.argv.slice(2)); }
  catch (err) { console.error(`没能打包：${err.message}`); process.exitCode = 2; }
}

module.exports = { pack };
