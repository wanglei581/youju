const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 协议升版的过渡（10/15，Codex 审查）：版本号按时间可排序，服务端记的版本不早于本机的就算已同意；
// 小程序太旧、后台已不认它带的版本（422）时弹窗请用户更新
const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');

function load(file, deps, globals) {
  const module = { exports: {} };
  vm.runInNewContext(read(file), {
    module, setTimeout, clearTimeout, Promise,
    require(name) { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    ...globals,
  }, { filename: file });
  return module.exports;
}

function consent() {
  const shown = { modals: [], toasts: [], stored: {} };
  const wx = {
    getStorageSync: (key) => shown.stored[key] || '',
    setStorageSync: (key, value) => { shown.stored[key] = value; },
    showToast: (o) => shown.toasts.push(o.title),
    showModal: (o) => shown.modals.push(o),
  };
  const mod = load('utils/consent.js', {
    '../content/legal.js': { final: true, version: '2026-10' },
    './api.js': { isLive: () => true, AGREED_KEY: 'agreed', forgetAgreed() { delete shown.stored.agreed; } },
    './exports.js': {}, './exporter.js': {},
  }, { wx });
  return { mod, shown };
}

test('协议版本（10/15）：服务端记的版本不早于本机的就算已同意，更早的要重新同意', () => {
  const { mod } = consent();
  assert.equal(mod.covers('2026-10'), true);
  // 在另一台手机的新版小程序里同意过更新的版本：不再请用户同意，免得把服务端记录改回旧版本
  assert.equal(mod.covers('2026-11'), true);
  assert.equal(mod.covers('2026-09'), false);
  assert.equal(mod.covers(''), false);
  assert.equal(mod.covers(null), false);
  // 写法不对的（不是"年-月"开头）不算：字母排在数字后面，直接比字符串会被当成更新的版本
  assert.equal(mod.covers('old'), false);
  assert.equal(mod.covers('9999'), false);

  let s = consent();
  assert.equal(s.mod.applyServer({ version: '2026-11', at: '2026-11-02T10:00:00+08:00' }, s.mod.consentEpoch()), true);
  assert.equal(s.mod.agreedHere(), true);
  s = consent();
  assert.equal(s.mod.applyServer({ version: '2026-09', at: '2026-09-28T10:00:00+08:00' }, s.mod.consentEpoch()), false);
  assert.equal(s.mod.agreedHere(), false);
  assert.equal(s.mod.applyServer(null, s.mod.consentEpoch()), false);
  // 服务端说这份同意已不算数（仍认的最早版本调高了）：版本号一样也要重新同意
  s = consent();
  assert.equal(s.mod.applyServer({ version: '2026-10', at: '2026-10-08T20:24:00+08:00', valid: false }, s.mod.consentEpoch()), false);
  assert.equal(s.mod.agreedHere(), false);
  assert.equal(s.mod.applyServer({ version: '2026-10', at: '2026-10-08T20:24:00+08:00', valid: true }, s.mod.consentEpoch()), true);
});

test('同意没成功（10/15）：后台已不认这个协议版本（422）时弹窗请用户更新小程序，其余照旧提示', () => {
  const { mod, shown } = consent();
  mod.agreeFailed({ status: 422, message: '协议已更新，请关闭小程序后重新打开，更新到新版本后再同意' });
  assert.equal(shown.modals.length, 1);
  assert.equal(shown.modals[0].title, '需要更新小程序');
  assert.equal(shown.modals[0].showCancel, false);
  assert.match(shown.modals[0].content, /重新打开/);
  mod.agreeFailed({ status: 0, message: '网络不太好，请稍后再试' });
  mod.agreeFailed(undefined);
  assert.deepEqual(shown.toasts, ['网络不太好，请稍后再试', '网络不太好，请稍后再试']);
  assert.equal(shown.modals.length, 1);
});

test('同意的三个入口（10/15）：弹窗同意、协议全文页、"我的"登录失败时都用 agreeFailed', () => {
  assert.match(read('utils/consent.js'), /\.catch\(\(err\) => \{ agreeFailed\(err\); resolve\(false\); \}\)/);
  assert.match(read('pages/agreement/index.js'), /agreeFailed\(err\);/);
  assert.match(read('pages/me/index.js'), /\.catch\(agreeFailed\);/);
  // 版本号要按时间可排序（直接比字符串）
  assert.match(read('content/legal.js'), /"version": "\d{4}-\d{2}"/);
});
