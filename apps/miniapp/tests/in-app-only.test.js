// 10/23 第 15 次审核退回："小程序内服务强制引流至其他渠道才能体验……属于纯导流小程序"，附图是查名单→命中→点"复制原文链接"→
// "已复制，请粘贴到浏览器打开"。创始人定：查询、正文、附件、结果都在小程序内完成。
// 这里守住首发包（和 tools/miniapp_presubmit_check.js 的 FIRST_PAGES 同一份）以及公共组件、工具函数：
// 不再出现把人送去浏览器或别的应用才能看完的说法，不再写"复制原文链接"。
// content/legal.js（用户协议和隐私保护指引）也查：10/24 一·9 剪切板已改成只写联系邮箱和"我的数据"复制
// （创始人"隐私指引第 9 条按你的建议稿改"；同一版 2026-11、不升版本，平台后台的剪切板用途照填写单一起改）。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

// 只去掉不会显示的部分：成对的 <!-- -->、从行首开始的 /* */ 和整行的 // 注释。
// 行尾注释和字符串里的 /* 不去，宁可多报（注释里写了禁用说法也算），不能把字符串里的文字吞掉。
const visible = (text) => text
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/^[ \t]*\/\*[\s\S]*?\*\//gm, '')
  .replace(/^[ \t]*\/\/.*$/gm, '');

// 10/25：站内不再给原文链接，"每条附原文链接"（D-014 角标）改成"每条注明出处"
const FORBIDDEN = ['复制原文链接', '浏览器', '以原网站为准', '复制后可能打不开', '其他应用打开', '用其他应用', '换个应用', '用 WPS', '用WPS', '附原文链接'];

function hitsIn(text) {
  const shown = visible(text);
  return FORBIDDEN.filter((word) => shown.includes(word));
}

function firstPages() {
  const tool = fs.readFileSync(path.join(ROOT, '..', '..', 'tools', 'miniapp_presubmit_check.js'), 'utf8');
  const literal = /const FIRST_PAGES = \[([\s\S]*?)\]/.exec(tool);
  assert.ok(literal, '读不到发布检查里的首发页面清单');
  return literal[1].match(/['"]([^'"]+)['"]/g).map((s) => s.slice(1, -1));
}

function listFiles(dir, pick) {
  return fs.readdirSync(path.join(ROOT, dir)).flatMap((name) => pick(name).filter((file) => fs.existsSync(path.join(ROOT, file))));
}

test('扫描规则本身：字符串、属性里的禁用说法要报出来，只在注释里的不报', () => {
  assert.deepEqual(hitsIn("wx.showToast({ title: '已复制，请粘贴到浏览器打开' });"), ['浏览器']);
  assert.deepEqual(hitsIn('<view aria-label="复制原文链接">出处</view>'), ['复制原文链接']);
  assert.deepEqual(hitsIn("const re = /a\\/*b/; const s = '以原网站为准'; // 末尾 */"), ['以原网站为准']);
  assert.deepEqual(hitsIn('<!-- 以前这里是"复制原文链接" -->\n// 以前提示粘贴到浏览器\n/* 以原网站为准 */'), []);
});

test('首发包页面、组件和工具函数：不出现复制链接去浏览器、以原网站为准这类把人送出小程序的说法', () => {
  const pages = firstPages();
  assert.equal(pages.length, 16, `首发页面清单读成了 ${pages.length} 页`);
  for (const page of ['detail', 'files', 'about', 'reader', 'lists', 'results']) assert.ok(pages.includes(page), `首发清单里没有 ${page}`);
  const files = [
    ...pages.flatMap((page) => ['wxml', 'js'].map((ext) => `pages/${page}/index.${ext}`)).filter((file) => fs.existsSync(path.join(ROOT, file))),
    ...listFiles('components', (dir) => ['wxml', 'js'].map((ext) => `components/${dir}/index.${ext}`)),
    ...listFiles('utils', (name) => (name.endsWith('.js') && !/^mock/i.test(name) ? [`utils/${name}`] : [])),
    'content/legal.js',
  ];
  assert.ok(files.includes('pages/detail/index.wxml') && files.includes('pages/reader/index.wxml'), '详情页、阅读页没有读到');
  const hits = files.flatMap((file) => hitsIn(read(file)).map((word) => `${file}：${word}`));
  assert.deepEqual(hits, []);
});

test('不再有复制原文链接的代码：utils/copy.js 已删，所有页面、组件、工具函数都不引用；详情页不绑复制', () => {
  assert.equal(fs.existsSync(path.join(ROOT, 'utils/copy.js')), false);
  const files = [
    ...listFiles('pages', (dir) => [`pages/${dir}/index.js`, `pages/${dir}/index.wxml`]),
    ...listFiles('components', (dir) => [`components/${dir}/index.js`, `components/${dir}/index.wxml`]),
    ...listFiles('utils', (name) => (name.endsWith('.js') ? [`utils/${name}`] : [])),
  ];
  const hits = files.filter((file) => /copyLink|utils\/copy(\.js)?['"]|\.\/copy(\.js)?['"]/.test(visible(read(file))));
  assert.deepEqual(hits, []);
  for (const file of ['pages/detail/index.wxml', 'components/doc-row/index.wxml']) {
    assert.doesNotMatch(visible(read(file)), /(?:bind|catch):?tap=["']copy|name=["']copy["']/, file);
  }
  for (const file of ['pages/detail/index.js', 'components/doc-row/index.js']) {
    assert.doesNotMatch(visible(read(file)), /setClipboardData|copyVersion/, file);
  }
  const detail = visible(read('pages/detail/index.wxml'));
  assert.match(detail, /bindtap="readBody">站内阅读全文</);
  assert.match(detail, /正文和附件可在本小程序阅读，链接仅供记录来源/);
});

// D-090（创始人 10/24 "查名单这组用词按你推荐的改"）：首页小字"政府公开名单"，名单页和转发标题"在政府公开名单里查企业"，
// "名单状态"改叫"发布阶段"，不写"上过哪些名单""当前效力"这类像资格或信用结论的说法（10/23 审核以"企业征信"退回过）
// "上过哪些……名单"中间夹了别的字也算（10/25 关于页漏过一句"查它上过哪些政府公开的认定、立项名单"）
const LIST_WORDING = ['上过哪些', '上了哪些', '名单状态', '当前效力'];

test('查名单用词（D-090）：首发包页面、组件和工具函数不再写"上过哪些名单""名单状态""当前效力"', () => {
  const pages = firstPages();
  const files = [
    ...pages.flatMap((page) => ['wxml', 'js'].map((ext) => `pages/${page}/index.${ext}`)).filter((file) => fs.existsSync(path.join(ROOT, file))),
    ...listFiles('components', (dir) => ['wxml', 'js'].map((ext) => `components/${dir}/index.${ext}`)),
    ...listFiles('utils', (name) => (name.endsWith('.js') && !/^mock/i.test(name) ? [`utils/${name}`] : [])),
  ];
  const hits = files.flatMap((file) => {
    const shown = visible(read(file));
    return LIST_WORDING.filter((word) => shown.includes(word)).map((word) => `${file}：${word}`);
  });
  assert.deepEqual(hits, []);
  assert.match(visible(read('pages/home/index.wxml')), /<text class="entry-name">查名单<\/text><text class="entry-sub">政府公开名单<\/text>/);
  assert.match(visible(read('pages/lists/index.wxml')), /<view class="h1">在政府公开名单里查企业<\/view>/);
  assert.match(visible(read('pages/lists/index.js')), /title: '在政府公开名单里查企业'/);
  assert.match(visible(read('pages/detail/index.wxml')), /class="tag">发布阶段：{{doc.status}}</);
  // D-091 ③（改 D-090 第⑤点）：输入框只留企业全称，不再写统一社会信用代码
  assert.match(visible(read('pages/lists/index.wxml')), /placeholder="输入企业全称"/);
  assert.ok(!visible(read('pages/lists/index.wxml')).includes('输入企业全称或统一社会信用代码'));
});

// 隐私保护指引一·9（剪切板）和代码一致：哪个页面写剪切板，一·9 就要写到它；一·9 不再提复制原文链接、浏览器
const CLIPBOARD_PAGES = { about: '关于页的联系邮箱', complaint: '投诉举报页的联系邮箱', 'my-data': '“我的数据”里的全部数据、单条数据和完整微信用户标识' };

test('隐私保护指引一·9 剪切板：写剪切板的页面都写进去了，不再提复制原文链接和浏览器（10/24）', () => {
  const legal = require('../content/legal.js');
  const item = JSON.stringify(legal).match(/9\. 剪切板：[^"]*/);
  assert.ok(item, '读不到隐私保护指引一·9');
  const text = item[0];
  assert.equal(text, '9. 剪切板：我们不读取剪切板，只在您主动点联系邮箱或“复制”时写入对应文字：关于页的联系邮箱；投诉举报页的联系邮箱（开放 AI 功能后才有）；“我的数据”里的全部数据、单条数据和完整微信用户标识（登录后才有）。');
  // 页面、组件、工具函数都查（按原文查，不去注释），组件和工具函数里不许写剪切板：写剪切板只能在页面里，好对上一·9
  const code = [
    ...listFiles('pages', (dir) => [`pages/${dir}/index.js`]),
    ...listFiles('components', (dir) => [`components/${dir}/index.js`]),
    ...listFiles('utils', (name) => (name.endsWith('.js') ? [`utils/${name}`] : [])),
  ];
  const writers = code.filter((file) => /setClipboardData/.test(read(file)));
  assert.deepEqual(writers.filter((file) => !file.startsWith('pages/')), [], '组件或工具函数写剪切板：改到页面里，并写进一·9');
  const pages = writers.map((file) => file.split('/')[1]).sort();
  assert.deepEqual(pages, Object.keys(CLIPBOARD_PAGES).sort(), '有新的页面写剪切板：先改隐私保护指引一·9 和后台填写单');
  for (const page of pages) assert.ok(text.includes(CLIPBOARD_PAGES[page]), `一·9 没写到 ${page}`);
  assert.deepEqual(code.filter((file) => /getClipboardData/.test(read(file))), [], '代码里不能读取剪切板');
});
