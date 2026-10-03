// 原生弹窗没有页面数据绑定，统一在 app 启动时追踪它们的完成事件。
const listeners = new Set();
let dialogs = 0;
let loading = false;
let installed = false;
function emit() { listeners.forEach((listener) => listener(dialogs > 0 || loading)); }
function subscribe(listener) {
  listeners.add(listener); listener(dialogs > 0 || loading);
  return () => listeners.delete(listener);
}
function install(platform) {
  if (installed) return;
  installed = true;
  ['showModal', 'showActionSheet', 'requestSubscribeMessage'].forEach((name) => {
    const original = platform[name];
    if (!original) return;
    platform[name] = function (options = {}) {
      dialogs += 1; emit();
      let finished = false;
      const finish = () => { if (!finished) { finished = true; dialogs -= 1; emit(); } };
      try {
        return original.call(platform, Object.assign({}, options, {
          complete(result) { finish(); if (options.complete) options.complete(result); },
        }));
      } catch (err) { finish(); throw err; }
    };
  });
  const show = platform.showLoading; const hide = platform.hideLoading;
  if (show && hide) {
    platform.showLoading = function (options = {}) {
      loading = true; emit();
      try {
        return show.call(platform, Object.assign({}, options, {
          fail(err) { loading = false; emit(); if (options.fail) options.fail(err); },
        }));
      } catch (err) { loading = false; emit(); throw err; }
    };
    platform.hideLoading = function (options) {
      loading = false; emit(); return hide.call(platform, options);
    };
  }
}
module.exports = { install, subscribe };
