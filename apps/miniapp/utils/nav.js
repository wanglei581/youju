// 自定义导航栏（首页、查名单等蓝色顶部）的尺寸：状态栏高度、导航内容区高度、右上角胶囊占的宽度。
// 手机上状态栏 20–59px，胶囊在状态栏下面；电脑版微信没有状态栏（高度是 0），胶囊位置也可能取不到。
// 取到的数照用（手机上的结果和原来的算法一样）；只有取不到、或者数不合理时，那一项才用默认值。
const DEFAULTS = { statusBarHeight: 20, navHeight: 44, menuWidth: 96 };
// 胶囊离状态栏底边的距离超过这个数就不可信（常见是 2–10px）
const MAX_GAP = 60;

const num = (n) => typeof n === 'number' && Number.isFinite(n);

function navMetrics(win, menu) {
  const out = { ...DEFAULTS };
  // 状态栏：取到就用，0 也照用（电脑版）；取不到才用默认值。和胶囊分开判断
  if (win && num(win.statusBarHeight) && win.statusBarHeight >= 0) out.statusBarHeight = win.statusBarHeight;
  if (!menu || !num(menu.top) || !num(menu.height) || menu.height <= 0) return out;
  // 导航内容区高度：让胶囊在内容区里上下居中。胶囊在状态栏上面、或者离得太远，说明数不对，用默认高度
  const gap = menu.top - out.statusBarHeight;
  if (gap >= 0 && gap <= MAX_GAP) out.navHeight = gap * 2 + menu.height;
  // 右侧留白：胶囊左边到窗口右边
  if (win && num(win.windowWidth) && num(menu.left) && menu.left > 0 && win.windowWidth > menu.left) out.menuWidth = win.windowWidth - menu.left;
  return out;
}

module.exports = { navMetrics, DEFAULTS, MAX_GAP };
