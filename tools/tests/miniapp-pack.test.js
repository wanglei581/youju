// 打过审版（首发 16 页，D-091）的打包工具：输出目录里只有这一版的页面和底栏，页面清单文件按包重写，
// 发布检查按这一版检查；不碰仓库里的 apps/miniapp。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pack } = require('../miniapp_pack.js');
const { FIRST_PAGES, pagesFor } = require('../miniapp_presubmit_check.js');

const SOURCE = path.join(__dirname, '..', '..', 'apps', 'miniapp');
const fresh = () => path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'youju-pack-')), 'out');
// 构建好的组件库（miniprogram_npm）仓库里不带：测试里放一个假的
function fakeNpm() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'youju-npm-'));
  fs.mkdirSync(path.join(dir, 'miniprogram_npm', 'tdesign-miniprogram', 'popup'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'miniprogram_npm', 'tdesign-miniprogram', 'popup', 'popup.js'), 'Component({});\n');
  return dir;
}

test('首发包：16 页、底栏四格、包外页面的目录删掉、页面清单按包重写；仓库里的文件不变', () => {
  const before = fs.readFileSync(path.join(SOURCE, 'utils', 'package-pages.js'), 'utf8');
  const appBefore = fs.readFileSync(path.join(SOURCE, 'app.json'), 'utf8');
  const out = fresh();
  const { pages, removed, result } = pack({ out, profile: 'first-release', mode: 'prepare', npmFrom: fakeNpm() });
  assert.deepEqual([...pages].sort(), [...FIRST_PAGES].sort());
  assert.equal(pages[0], 'pages/home/index');
  assert.equal(removed.length, JSON.parse(appBefore).pages.length - 16);
  const app = JSON.parse(fs.readFileSync(path.join(out, 'app.json'), 'utf8'));
  assert.deepEqual(app.pages, pages);
  assert.deepEqual(app.tabBar.list.map((t) => t.pagePath), ['pages/home/index', 'pages/follow/index', 'pages/files/index', 'pages/me/index']);
  for (const page of removed) assert.equal(fs.existsSync(path.join(out, path.dirname(page))), false, page);
  for (const page of pages) assert.ok(fs.existsSync(path.join(out, `${page}.js`)), page);
  // 页面清单：包里的 16 页；utils/pages.js 照常判断
  delete require.cache[require.resolve(path.join(out, 'utils', 'package-pages.js'))];
  assert.deepEqual(require(path.join(out, 'utils', 'package-pages.js')), pages);
  const helper = require(path.join(out, 'utils', 'pages.js'));
  assert.equal(helper.has('advisor'), false); assert.equal(helper.has('my-data'), true);
  // 测试、本机依赖不进包
  for (const skipped of ['tests', 'node_modules', 'README.md', 'package-lock.json']) assert.equal(fs.existsSync(path.join(out, skipped)), false, skipped);
  assert.ok(fs.existsSync(path.join(out, 'miniprogram_npm', 'tdesign-miniprogram', 'popup', 'popup.js')));
  // 发布检查按首发版：页面范围、底栏都过；备案号这类外部条件只提示
  const byId = Object.fromEntries(result.checks.map((c) => [c.id, c.state]));
  assert.equal(byId.pages, 'pass'); assert.equal(byId.tabs, 'pass'); assert.equal(byId['ai-off'], 'pass');
  assert.equal(fs.readFileSync(path.join(SOURCE, 'utils', 'package-pages.js'), 'utf8'), before);
  assert.equal(fs.readFileSync(path.join(SOURCE, 'app.json'), 'utf8'), appBefore);
});

test('完整版也能打：页面和仓库一样多', () => {
  const { pages, removed } = pack({ out: fresh(), profile: 'complete-release', mode: 'prepare', npmFrom: fakeNpm() });
  assert.deepEqual([...pages].sort(), [...pagesFor('complete-release')].sort());
  assert.deepEqual(removed, []);
});

test('不往非空目录、apps/miniapp 里面打；参数不对不打', () => {
  const out = fresh();
  fs.mkdirSync(out, { recursive: true });
  fs.writeFileSync(path.join(out, 'keep.txt'), 'x');
  assert.throws(() => pack({ out }), /不是空的/);
  assert.equal(fs.readFileSync(path.join(out, 'keep.txt'), 'utf8'), 'x');
  assert.throws(() => pack({ out: path.join(SOURCE, 'dist') }), /不能放在 apps\/miniapp 里面/);
  assert.throws(() => pack({ out: fresh(), profile: 'nope' }), /参数不支持/);
  assert.throws(() => pack({}), /--out/);
  const empty = fresh();
  assert.throws(() => pack({ out: empty, npmFrom: os.tmpdir() }), /没有 miniprogram_npm/);
  assert.equal(fs.existsSync(empty), false, '出错时不留半个包');
  // 没有构建好的组件库就不打（搜索页用到它，缺了打开就报错）
  if (!fs.existsSync(path.join(SOURCE, 'miniprogram_npm'))) {
    const out = fresh();
    assert.throws(() => pack({ out }), /构建 npm/);
    assert.equal(fs.existsSync(out), false, '出错时不留半个包');
  }
});
