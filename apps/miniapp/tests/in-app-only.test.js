// 10/23 第 15 次审核退回："小程序内服务强制引流至其他渠道才能体验……属于纯导流小程序"，附图是查名单→命中→点"复制原文链接"→
// "已复制，请粘贴到浏览器打开"。创始人定：查询、正文、附件、结果都在小程序内完成。
// 这里守住首发包（和 tools/miniapp_presubmit_check.js 的 FIRST_PAGES 同一份）以及公共组件、工具函数：
// 不再出现把人送去浏览器或别的应用才能看完的说法，详情页和列表行不再写剪切板。
// content/legal.js（隐私保护指引第 9 条剪切板）是有版本的正式文本，改动要和平台上的隐私设置一起改，不在这里查。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
// 只看会显示出来的部分：去掉 <!-- 注释 -->、/* 注释 */ 和整行的 // 注释
const visible = (text) => text
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '');

const FORBIDDEN = ['复制原文链接', '粘贴到浏览器', '去浏览器', '用浏览器', '在浏览器', '浏览器打开', '以原网站为准', '复制后可能打不开'];

function firstPages() {
  const tool = fs.readFileSync(path.join(ROOT, '..', '..', 'tools', 'miniapp_presubmit_check.js'), 'utf8');
  const literal = /const FIRST_PAGES = \[([\s\S]*?)\]\.map/.exec(tool);
  assert.ok(literal, '读不到发布检查里的首发页面清单');
  return literal[1].match(/'([^']+)'/g).map((s) => s.slice(1, -1));
}

function filesToCheck() {
  const files = [];
  for (const page of firstPages()) {
    for (const ext of ['wxml', 'js']) {
      const file = `pages/${page}/index.${ext}`;
      if (fs.existsSync(path.join(ROOT, file))) files.push(file);
    }
  }
  for (const dir of fs.readdirSync(path.join(ROOT, 'components'))) {
    for (const ext of ['wxml', 'js']) {
      const file = `components/${dir}/index.${ext}`;
      if (fs.existsSync(path.join(ROOT, file))) files.push(file);
    }
  }
  for (const name of fs.readdirSync(path.join(ROOT, 'utils'))) {
    if (name.endsWith('.js') && !/^mock/i.test(name)) files.push(`utils/${name}`);
  }
  return files;
}

test('首发包页面、组件和工具函数：不出现复制链接去浏览器、以原网站为准这类把人送出小程序的说法', () => {
  const files = filesToCheck();
  assert.ok(files.length > 40, `要查的文件太少（${files.length}），清单可能读错了`);
  const hits = [];
  for (const file of files) {
    const text = visible(read(file));
    for (const word of FORBIDDEN) if (text.includes(word)) hits.push(`${file}：${word}`);
  }
  assert.deepEqual(hits, []);
});

test('详情页和列表行不写剪切板：原文出处只写文字，阅读走站内阅读页', () => {
  for (const file of ['pages/detail/index.js', 'components/doc-row/index.js']) {
    assert.doesNotMatch(visible(read(file)), /copyLink|setClipboardData|utils\/copy\.js/, file);
  }
  const detail = visible(read('pages/detail/index.wxml'));
  assert.doesNotMatch(detail, /bindtap="copy"|catchtap="copyVersion"|name="copy"/);
  assert.match(detail, /bindtap="readBody">站内阅读全文</);
  assert.match(detail, /正文和附件可在本小程序阅读，链接仅供记录来源/);
});
