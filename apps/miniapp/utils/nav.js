// 自定义导航栏（首页、查名单等蓝色顶部）的尺寸：状态栏高度、导航内容区高度、右上角胶囊占的宽度。
// 手机上状态栏 20–59px，胶囊在状态栏下面；电脑版微信没有状态栏（高度是 0），胶囊位置也可能取不到。
// 取到的数不合理时用默认值，免得顶部被算成一条窄缝、标题和胶囊叠在一起。
const DEFAULTS = { statusBarHeight: 20, navHeight: 44, menuWidth: 96 };
const MIN_NAV = 40;
const MAX_NAV = 88;

function navMetrics(win, menu) {
  const ok = (n) => typeof n === 'number' && Number.isFinite(n);
  if (!win || !menu || !ok(menu.top) || !ok(menu.height) || !ok(menu.left) || menu.height <= 0 || !ok(win.windowWidth) || win.windowWidth <= 0) {
    return { ...DEFAULTS };
  }
  // 状态栏高度是 0 就照用 0（电脑版）；取不到才用默认值
  const statusBar = ok(win.statusBarHeight) && win.statusBarHeight >= 0 ? win.statusBarHeight : DEFAULTS.statusBarHeight;
  // 导航内容区高度：让胶囊在内容区里上下居中
  const gap = Math.max(menu.top - statusBar, 0);
  const navHeight = Math.min(MAX_NAV, Math.max(MIN_NAV, gap * 2 + menu.height));
  const menuWidth = win.windowWidth - menu.left;
  return { statusBarHeight: statusBar, navHeight, menuWidth: menuWidth > 0 && menuWidth < win.windowWidth ? menuWidth : DEFAULTS.menuWidth };
}

module.exports = { navMetrics, DEFAULTS };
