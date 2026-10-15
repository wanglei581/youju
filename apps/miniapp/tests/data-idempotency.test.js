const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const keys = require('../utils/data-idempotency.js');
const KEY = keys.STORAGE_KEY;
const [A, B, C] = ['a', 'b', 'c'].map(c => c.repeat(64));
// 与 wx 一致：缺键返回空字符串，读写都是副本。
function storage() {
  const map = new Map(); const copy = v => JSON.parse(JSON.stringify(v));
  return { map, getStorageSync: k => map.has(k) ? copy(map.get(k)) : '', setStorageSync: (k,v) => map.set(k, copy(v)), removeStorageSync: k => map.delete(k), clearStorageSync: () => map.clear() };
}
const raw = s => s.map.get(KEY);
// 真实 consent.js 的注销流程，服务端和导出文件用替身。
function withdraw(s, { confirm = true, deleteFails = false, clearFails = false } = {}) {
  const module = { exports: {} };
  const deps = { '../content/legal.js': { final: true, version: '2026-10' }, './config.js': { templates: {} }, './exports.js': { clear() {} }, './exporter.js': { removeFiles: () => 0 },
    './api.js': { AGREED_KEY: 'youjuAgreedVersion', forgetAgreed: () => s.removeStorageSync('youjuAgreedVersion'), deleteMe: () => deleteFails ? Promise.reject(new Error('网络故障')) : Promise.resolve() } };
  const wx = { ...s, clearStorageSync: clearFails ? () => { throw new Error('clearStorageSync:fail'); } : s.clearStorageSync, showModal: o => o.success({ confirm }), showToast() {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../utils/consent.js'), 'utf8'), { module, wx, Promise, require: k => { assert.ok(Object.hasOwn(deps, k), k); return deps[k]; } });
  return module.exports.withdraw();
}
test('固定块 SHA-256 对照标准向量及 Node crypto：边界、中文、emoji、孤立代理和大文件', () => {
  assert.equal(keys.sha256('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  for (const value of ['', 'a'.repeat(55), 'a'.repeat(56), 'a'.repeat(63), 'a'.repeat(64), '中文😀' + String.fromCharCode(0xd800, 0xdfff, 0xdc00, 0xd800), '合成客户😀'.repeat(20000), 'YQ=='.repeat(20480)]) {
    assert.equal(keys.sha256(value), crypto.createHash('sha256').update(value).digest('hex'));
  }
});
test('摘要按规范内容计算，无明文；重载模块与超过TTL未知提交仍复用键，确认后可新建', () => {
  const s = storage(); keys.syncOwner(s, A);
  const body = { kind: 'track', rows: [{ name: '合成保密企业', city: '青岛' }] };
  const first = keys.claim(s, A, body, 1000);
  delete require.cache[require.resolve('../utils/data-idempotency.js')];
  const reload = require('../utils/data-idempotency.js');
  const retry = reload.claim(s, A, { rows: [{ city: '青岛', name: '合成保密企业' }], kind: 'track' }, 1000 + keys.TTL * 2);
  assert.equal(first.key, retry.key); assert.equal(retry.existing, true);
  assert.doesNotMatch(JSON.stringify(raw(s)), /合成保密企业|青岛|track|rows/);
  assert.deepEqual(Object.keys(raw(s).entries[0]).sort(), ['digest', 'expiresAt', 'key']);
  reload.confirm(s, retry); assert.equal(raw(s).entries.length, 0);
  assert.notEqual(reload.claim(s, A, body, 1000 + keys.TTL * 3).key, first.key);
});
test('A未知提交→切到B→回A：A的键按账号保留，超过TTL重填同一内容仍用原键；B拿不到A的键', () => {
  const s = storage(); keys.syncOwner(s, A);
  const body = { kind: 'compare', rows: ['合成甲'] };
  const first = keys.claim(s, A, body, 1000); // 504：服务端可能已受理并扣额
  keys.syncOwner(s, B);
  assert.equal(raw(s).owner, B); assert.deepEqual(raw(s).entries, []); assert.deepEqual(raw(s).others[A].map(e => e.key), [first.key]);
  assert.throws(() => keys.claim(s, A, body), /账号状态/); // 仍拿着A的页面不能借B的会话发出A的键
  const other = keys.claim(s, B, body, 2000);
  assert.equal(other.existing, false); assert.notEqual(other.key, first.key); assert.equal(other.owner, B);
  keys.syncOwner(s, A);
  const retry = keys.claim(s, A, { rows: ['合成甲'], kind: 'compare' }, 1000 + keys.TTL * 10);
  assert.equal(retry.existing, true); assert.equal(retry.key, first.key);
  assert.deepEqual(raw(s).others[B].map(e => e.key), [other.key]);
  keys.syncOwner(s, B); assert.equal(keys.claim(s, B, body).key, other.key);
  assert.doesNotMatch(JSON.stringify(raw(s)), /合成甲|compare|rows/);
});
test('旧版{owner,entries}原样可读；切账号迁入分区，切回仍复用旧键；单账号写回仍是旧形状', () => {
  const s = storage(); const body = { kind: 'track', rows: [{ name: '合成旧版企业' }] };
  const legacy = { digest: keys.sha256(A + '\n' + keys.canonical(body)), key: 'data-legacy-a224', expiresAt: 1000 };
  s.map.set(KEY, { owner: A, entries: [legacy] });
  keys.syncOwner(s, A); assert.deepEqual(raw(s), { owner: A, entries: [legacy] });
  keys.syncOwner(s, B); assert.deepEqual(raw(s), { owner: B, entries: [], others: { [A]: [legacy] } });
  keys.syncOwner(s, A); assert.deepEqual(raw(s), { owner: A, entries: [legacy] });
  const retry = keys.claim(s, A, body, 1000 + keys.TTL * 30);
  assert.equal(retry.key, 'data-legacy-a224'); assert.equal(retry.existing, true);
  assert.deepEqual(Object.keys(raw(s)).sort(), ['entries', 'owner']);
});
test('迟到回调只改发出请求的账号分区：不碰B、不换当前账号，重复或无关回调不写，清空后不重建', () => {
  const s = storage(); keys.syncOwner(s, A);
  const a1 = keys.claim(s, A, { n: 1 }); const a2 = keys.claim(s, A, { n: 2 });
  keys.syncOwner(s, B); const b1 = keys.claim(s, B, { n: 1 });
  const mine = JSON.stringify(raw(s).entries);
  keys.confirm(s, a1); // A的请求切到B后才返回成功（或首次明确4xx）
  assert.equal(raw(s).owner, B); assert.equal(JSON.stringify(raw(s).entries), mine); assert.deepEqual(raw(s).others[A].map(e => e.key), [a2.key]);
  let writes = 0; const set = s.setStorageSync; s.setStorageSync = (k, v) => { writes += 1; set(k, v); };
  keys.confirm(s, a1); keys.confirm(s, { owner: C, key: a2.key }); keys.confirm(s, { owner: 'x', key: a2.key }); keys.confirm(s, null);
  assert.equal(writes, 0);
  keys.confirm(s, a2); assert.deepEqual(raw(s), { owner: B, entries: JSON.parse(mine) });
  s.map.clear(); keys.confirm(s, a2); keys.confirm(s, b1); assert.equal(s.map.size, 0);
  assert.throws(() => keys.claim(s, B, { n: 1 }), /账号状态/);
});
test('真实注销成功删除全部账号分区，清空存储失败也删掉；迟到回调不重建、旧页面不能发送；取消或失败不动', async () => {
  const setup = () => {
    const s = storage(); s.map.set('personalCache', { value: 'x' });
    keys.syncOwner(s, A); const a = keys.claim(s, A, { n: 1 }); keys.syncOwner(s, B); const b = keys.claim(s, B, { n: 1 });
    return { s, a, b };
  };
  for (const clearFails of [false, true]) {
    const { s, a, b } = setup();
    assert.equal(await withdraw(s, { clearFails }), true);
    assert.equal(s.map.has(KEY), false); assert.equal(s.map.has('personalCache'), clearFails);
    keys.confirm(s, a); keys.confirm(s, b); assert.equal(s.map.has(KEY), false);
    assert.throws(() => keys.claim(s, B, { n: 1 }), /账号状态/);
  }
  for (const options of [{ confirm: false }, { deleteFails: true }]) {
    const { s } = setup(); const before = JSON.stringify(raw(s));
    assert.equal(await withdraw(s, options), false); assert.equal(JSON.stringify(raw(s)), before);
  }
});
test('容量：每账号20条、全机合计40条；到上限只停止新建，原内容仍用原键，未确认键一个不丢', () => {
  const s = storage(); keys.syncOwner(s, A);
  assert.throws(() => keys.claim(s, A, { text: '汉'.repeat(35000) }), /100 KB/); assert.equal(raw(s).entries.length, 0);
  const fill = owner => { keys.syncOwner(s, owner); return Array.from({ length: keys.MAX_PENDING }, (_, i) => keys.claim(s, owner, { i })); };
  const fromA = fill(A);
  assert.throws(() => keys.claim(s, A, { i: 'new' }), /未确认的提交较多/); assert.equal(keys.claim(s, A, { i: 0 }).key, fromA[0].key);
  const fromB = fill(B); // 另一账号占满自己的上限也挡不住当前账号
  keys.syncOwner(s, C); assert.throws(() => keys.claim(s, C, { i: 0 }), /其他账号/);
  assert.deepEqual(raw(s).others[A].map(e => e.key), fromA.map(r => r.key)); assert.deepEqual(raw(s).others[B].map(e => e.key), fromB.map(r => r.key));
  keys.confirm(s, fromA[0]);
  assert.equal(keys.claim(s, C, { i: 0 }).existing, false); assert.throws(() => keys.claim(s, C, { i: 1 }), /其他账号/);
  keys.syncOwner(s, A); assert.equal(keys.claim(s, A, { i: 1 }).key, fromA[1].key);
});
test('坏数据：任何一处不合格都停止同步、领取和确认，不改写、不丢弃原记录', () => {
  const entry = (n = 0) => ({ digest: keys.sha256(String(n)), key: `data-k${n}`, expiresAt: 1 });
  const many = (n, from = 0) => Array.from({ length: n }, (_, i) => entry(from + i));
  const owners = Object.fromEntries(Array.from({ length: keys.MAX_TOTAL + 1 }, (_, i) => [i.toString(16).padStart(64, 'd'), []]));
  const bad = ['garbage', 0, false, [], {}, { owner: A }, { owner: 'x', entries: [] }, { owner: [A], entries: [] }, { owner: A, entries: {} },
    { owner: A, entries: [null] }, { owner: A, entries: ['x'] }, { owner: A, entries: [{ ...entry(), digest: 'z' }] }, { owner: A, entries: [{ ...entry(), key: '' }] },
    { owner: A, entries: [{ ...entry(), key: 'k'.repeat(129) }] }, { owner: A, entries: [{ ...entry(), expiresAt: 'soon' }] }, { owner: A, entries: many(keys.MAX_PENDING + 1) },
    { owner: A, entries: [], others: null }, { owner: A, entries: [], others: [] }, { owner: A, entries: [], others: 'x' }, { owner: A, entries: [], others: { [A]: [entry()] } },
    { owner: A, entries: [], others: { bogus: [entry()] } }, { owner: A, entries: [], others: { [B]: [null] } }, { owner: A, entries: [], others: owners },
    { owner: A, entries: many(keys.MAX_PENDING), others: { [B]: many(keys.MAX_PENDING, 20), [C]: [entry(99)] } }];
  for (const value of bad) {
    const s = storage(); s.map.set(KEY, value); const before = JSON.stringify(raw(s));
    for (const run of [() => keys.syncOwner(s, A), () => keys.syncOwner(s, B), () => keys.claim(s, A, { n: 1 }), () => keys.confirm(s, { owner: A, key: 'data-k0' })]) {
      assert.throws(run, /无法读取/, JSON.stringify(value).slice(0, 60));
    }
    assert.equal(JSON.stringify(raw(s)), before);
  }
  const s = storage(); s.getStorageSync = () => { throw new Error('getStorageSync:fail'); };
  assert.throws(() => keys.syncOwner(s, A), /getStorageSync:fail/); assert.throws(() => keys.claim(s, A, {}), /getStorageSync:fail/);
});
test('写入失败一律停止：新键未落盘不发送，续期失败不发送，换账号失败保留原分区', () => {
  const s = storage(); keys.syncOwner(s, A); const kept = keys.claim(s, A, { n: 1 });
  const before = JSON.stringify(raw(s));
  s.setStorageSync = () => { throw new Error('setStorageSync:fail exceed storage'); };
  assert.throws(() => keys.claim(s, A, { n: 2 }), /setStorageSync:fail/);
  assert.throws(() => keys.claim(s, A, { n: 1 }), /setStorageSync:fail/);
  assert.throws(() => keys.syncOwner(s, B), /setStorageSync:fail/);
  assert.equal(JSON.stringify(raw(s)), before); assert.equal(raw(s).owner, A); assert.equal(raw(s).entries[0].key, kept.key);
});
