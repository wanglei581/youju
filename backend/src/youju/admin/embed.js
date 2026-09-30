// 只向同源父页面取口令；单独访问和无法信任父页面时沿用原来的口令框。
function youjuEmbedded() {
  try {
    return new URLSearchParams(location.search).get('embed') === '1' &&
      window.parent !== window && window.parent.location.origin === location.origin &&
      typeof window.parent.getYoujuAdminToken === 'function';
  } catch (_) { return false; }
}
function youjuAdminToken() {
  if (youjuEmbedded()) return window.parent.getYoujuAdminToken();
  return document.getElementById('token').value.trim();
}
document.addEventListener('DOMContentLoaded', () => {
  if (!youjuEmbedded()) return;
  const header = document.querySelector('header');
  // 先藏口令框、先把工具栏放进页面，再挪控件：控件挪进还没放进页面的工具栏后，就找不到口令框了
  const token = document.getElementById('token');
  if (token) token.style.display = 'none';
  const toolbar = document.createElement('div');
  toolbar.style.cssText = 'display:flex;gap:10px;flex-wrap:wrap;align-items:center;padding:12px 16px;background:#fff;border-bottom:1px solid #e4e7e9';
  if (header) {
    header.after(toolbar);
    header.style.display = 'none';
    const controls = header.querySelector('.auth');
    if (controls) toolbar.append(controls);
  } else {
    document.body.prepend(toolbar);
  }
  const count = document.getElementById('count');
  if (count) toolbar.append(count);
  const due = document.getElementById('due');
  const wanted = new URLSearchParams(location.search).get('due');
  if (due && ['all', '30', 'future'].includes(wanted)) due.value = wanted;
  const load = document.getElementById('load');
  if (load) load.click();
  else if (typeof window.load === 'function') window.load();
});
