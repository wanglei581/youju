const test = require('node:test');
const assert = require('node:assert/strict');
const { navMetrics, DEFAULTS } = require('../utils/nav.js');

// 自定义导航栏的尺寸（10/11 Codex 排查电脑版时指出：状态栏为 0 时原来按 20 算，导航内容区会被算成十来像素）
test('手机：胶囊在状态栏下面，内容区高度让胶囊上下居中', () => {
  // iPhone 14：状态栏 47，胶囊 top 51、高 32、left 281，窗口宽 390
  assert.deepEqual(navMetrics({ statusBarHeight: 47, windowWidth: 390 }, { top: 51, height: 32, left: 281 }), { statusBarHeight: 47, navHeight: 40, menuWidth: 109 });
  // 安卓常见：状态栏 24，胶囊 top 32、高 32
  assert.deepEqual(navMetrics({ statusBarHeight: 24, windowWidth: 360 }, { top: 32, height: 32, left: 263 }), { statusBarHeight: 24, navHeight: 48, menuWidth: 97 });
});

test('电脑版：状态栏是 0 就按 0 算，不再硬当成 20；内容区不会被算成一条窄缝', () => {
  const pc = navMetrics({ statusBarHeight: 0, windowWidth: 1024 }, { top: 10, height: 32, left: 920 });
  assert.deepEqual(pc, { statusBarHeight: 0, navHeight: 52, menuWidth: 104 });
  // 胶囊比状态栏还靠上（数不合理）：内容区至少 40
  assert.equal(navMetrics({ statusBarHeight: 20, windowWidth: 390 }, { top: 8, height: 32, left: 281 }).navHeight, 40);
  // 高得离谱：封顶 88
  assert.equal(navMetrics({ statusBarHeight: 0, windowWidth: 390 }, { top: 200, height: 32, left: 281 }).navHeight, 88);
});

test('取不到或取到 0：用默认值，页面照常显示', () => {
  for (const [win, menu] of [[null, null], [{ statusBarHeight: 0, windowWidth: 0 }, { top: 0, height: 0, left: 0 }], [{ windowWidth: 390 }, {}], [{ statusBarHeight: 47, windowWidth: 390 }, { top: 51, height: 0, left: 281 }]]) {
    assert.deepEqual(navMetrics(win, menu), DEFAULTS);
  }
  // 状态栏取不到但胶囊正常：状态栏用默认 20
  assert.equal(navMetrics({ windowWidth: 390 }, { top: 26, height: 32, left: 281 }).statusBarHeight, 20);
  // 胶囊 left 不合理（比窗口还宽）：右侧留白用默认值
  assert.equal(navMetrics({ statusBarHeight: 0, windowWidth: 390 }, { top: 10, height: 32, left: 500 }).menuWidth, 96);
});

test('app.js 用这个函数算，不再自己把 0 当成 20', () => {
  const src = require('node:fs').readFileSync(require('node:path').join(__dirname, '../app.js'), 'utf8');
  assert.match(src, /navMetrics\(wx\.getWindowInfo\(\), wx\.getMenuButtonBoundingClientRect\(\)\)/);
  assert.doesNotMatch(src, /statusBarHeight \|\| 20/);
});
