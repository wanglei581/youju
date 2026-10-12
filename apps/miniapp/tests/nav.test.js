const test = require('node:test');
const assert = require('node:assert/strict');
const { navMetrics, DEFAULTS } = require('../utils/nav.js');

// 自定义导航栏的尺寸。10/11 Codex 排查电脑版时指出：状态栏为 0 时原来按 20 算，导航内容区会被算成十来像素。
// 10/12 Codex 审查：手机上的结果要和原来的算法完全一样，只对取不到或不合理的数兜底；状态栏和胶囊分开判断。
const legacy = (win, menu) => {
  const statusBar = win.statusBarHeight || 20;
  return { statusBarHeight: statusBar, navHeight: (menu.top - statusBar) * 2 + menu.height, menuWidth: win.windowWidth - menu.left };
};

test('手机：和原来的算法结果完全一样（不加上下限）', () => {
  const phones = [
    [{ statusBarHeight: 47, windowWidth: 390 }, { top: 51, height: 32, left: 281 }], // iPhone 14
    [{ statusBarHeight: 59, windowWidth: 430 }, { top: 63, height: 32, left: 321 }], // 灵动岛机型
    [{ statusBarHeight: 20, windowWidth: 375 }, { top: 24, height: 32, left: 278 }], // 老款 iPhone
    [{ statusBarHeight: 24, windowWidth: 360 }, { top: 32, height: 32, left: 263 }], // 安卓常见
    [{ statusBarHeight: 24, windowWidth: 360 }, { top: 26, height: 32, left: 263 }], // 间距只有 2：原来是 36，不能被抬到 40
    [{ statusBarHeight: 30, windowWidth: 412 }, { top: 59, height: 32, left: 315 }], // 间距 29：原来是 90，不能被压到 88
    [{ statusBarHeight: 38, windowWidth: 820 }, { top: 44, height: 32, left: 723 }], // 平板
  ];
  for (const [win, menu] of phones) assert.deepEqual(navMetrics(win, menu), legacy(win, menu));
});

test('电脑版：状态栏是 0 就按 0 算，不再硬当成 20', () => {
  assert.deepEqual(navMetrics({ statusBarHeight: 0, windowWidth: 1024 }, { top: 10, height: 32, left: 920 }), { statusBarHeight: 0, navHeight: 52, menuWidth: 104 });
  // 胶囊位置取不到：状态栏仍按取到的 0，导航内容区和右侧留白用默认值
  for (const menu of [null, {}, { top: 0, height: 0, left: 0 }, { top: 10, height: NaN, left: 900 }]) {
    assert.deepEqual(navMetrics({ statusBarHeight: 0, windowWidth: 1024 }, menu), { statusBarHeight: 0, navHeight: DEFAULTS.navHeight, menuWidth: DEFAULTS.menuWidth });
  }
});

test('数不合理时只有那一项用默认值，不出现负数、NaN', () => {
  // 胶囊比状态栏还靠上：导航内容区用默认高度，其余照算
  assert.deepEqual(navMetrics({ statusBarHeight: 20, windowWidth: 390 }, { top: 8, height: 32, left: 281 }), { statusBarHeight: 20, navHeight: 44, menuWidth: 109 });
  // 胶囊离状态栏远得离谱
  assert.equal(navMetrics({ statusBarHeight: 0, windowWidth: 390 }, { top: 200, height: 32, left: 281 }).navHeight, 44);
  // 胶囊 left 比窗口还宽、或取不到窗口宽度：右侧留白用默认值
  assert.equal(navMetrics({ statusBarHeight: 0, windowWidth: 390 }, { top: 10, height: 32, left: 500 }).menuWidth, 96);
  assert.equal(navMetrics({ statusBarHeight: 47 }, { top: 51, height: 32, left: 281 }).menuWidth, 96);
  // 状态栏取不到：用默认 20，胶囊照算
  assert.deepEqual(navMetrics({ windowWidth: 390 }, { top: 26, height: 32, left: 281 }), { statusBarHeight: 20, navHeight: 44, menuWidth: 109 });
  // 什么都取不到
  assert.deepEqual(navMetrics(null, null), DEFAULTS);
  assert.deepEqual(navMetrics(undefined, { top: 51, height: 32, left: 281 }), { statusBarHeight: 20, navHeight: 94, menuWidth: 96 });
  for (const [win, menu] of [[null, null], [{ statusBarHeight: -1, windowWidth: -5 }, { top: -3, height: 32, left: -9 }], [{ statusBarHeight: NaN, windowWidth: NaN }, { top: NaN, height: 32, left: NaN }]]) {
    const out = navMetrics(win, menu);
    for (const key of ['statusBarHeight', 'navHeight', 'menuWidth']) assert.ok(Number.isFinite(out[key]) && out[key] >= 0, `${key}=${out[key]}`);
  }
});

test('app.js 用这个函数算，不再自己把 0 当成 20', () => {
  const src = require('node:fs').readFileSync(require('node:path').join(__dirname, '../app.js'), 'utf8');
  assert.match(src, /navMetrics\(wx\.getWindowInfo\(\), wx\.getMenuButtonBoundingClientRect\(\)\)/);
  assert.doesNotMatch(src, /statusBarHeight \|\| 20/);
});
