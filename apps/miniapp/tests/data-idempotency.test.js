const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const keys = require('../utils/data-idempotency.js');
const OWNER = 'a'.repeat(64);
function storage() {
  const map = new Map();
  return { map, getStorageSync: k => map.get(k), setStorageSync: (k,v) => map.set(k, JSON.parse(JSON.stringify(v))) };
}
test('固定块 SHA-256 对照标准向量及 Node crypto：边界、中文、emoji、孤立代理和大文件', () => {
  assert.equal(keys.sha256('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  for (const value of ['', 'a'.repeat(55), 'a'.repeat(56), 'a'.repeat(63), 'a'.repeat(64), '中文😀\ud800\udfff', '合成客户😀'.repeat(20000), 'YQ=='.repeat(20480)]) {
    assert.equal(keys.sha256(value), crypto.createHash('sha256').update(value).digest('hex'));
  }
});
test('摘要按规范内容计算，无明文；重载模块与超过TTL未知提交仍复用键，确认后可新建', () => {
  const s = storage(); keys.syncOwner(s, OWNER);
  const body = { kind: 'track', rows: [{ name: '合成保密企业', city: '青岛' }] };
  const first = keys.claim(s, OWNER, body, 1000);
  delete require.cache[require.resolve('../utils/data-idempotency.js')];
  const reload = require('../utils/data-idempotency.js');
  const retry = reload.claim(s, OWNER, { rows: [{ city: '青岛', name: '合成保密企业' }], kind: 'track' }, 1000 + keys.TTL * 2);
  assert.equal(first.key, retry.key); assert.equal(retry.existing, true);
  const saved = JSON.stringify(s.getStorageSync(keys.STORAGE_KEY));
  assert.doesNotMatch(saved, /合成保密企业|青岛|track|rows/);
  assert.deepEqual(Object.keys(s.getStorageSync(keys.STORAGE_KEY).entries[0]).sort(), ['digest', 'expiresAt', 'key']);
  reload.confirm(s, retry); assert.equal(s.getStorageSync(keys.STORAGE_KEY).entries.length, 0);
  assert.notEqual(reload.claim(s, OWNER, body, 1000 + keys.TTL * 3).key, first.key);
});
test('切账号清理摘要；旧账号迟到确认与注销后回调不重建记录', () => {
  const s = storage(); keys.syncOwner(s, OWNER);
  const old = keys.claim(s, OWNER, { kind: 'compare', rows: ['合成甲'] });
  const nextOwner = 'b'.repeat(64); keys.syncOwner(s, nextOwner);
  const next = keys.claim(s, nextOwner, { kind: 'compare', rows: ['合成乙'] });
  assert.throws(() => keys.claim(s, OWNER, {}), /账号状态/);
  keys.confirm(s, old); assert.equal(s.getStorageSync(keys.STORAGE_KEY).entries[0].key, next.key);
  s.map.clear(); keys.confirm(s, next); assert.equal(s.map.size, 0);
  assert.throws(() => keys.claim(s, nextOwner, {}), /账号状态/);
});
test('损坏、写入失败与待确认上限均安全停止；超限正文不生成记录', () => {
  const s = storage(); keys.syncOwner(s, OWNER);
  assert.throws(() => keys.claim(s, OWNER, { text: '汉'.repeat(35000) }), /100 KB/);
  assert.equal(s.getStorageSync(keys.STORAGE_KEY).entries.length, 0);
  for (let i = 0; i < keys.MAX_PENDING; i++) keys.claim(s, OWNER, { i });
  assert.throws(() => keys.claim(s, OWNER, { i: 21 }), /未确认/);
  assert.equal(keys.claim(s, OWNER, { i: 0 }).existing, true);
  s.setStorageSync(keys.STORAGE_KEY, { owner: OWNER, entries: [null] });
  assert.throws(() => keys.claim(s, OWNER, {}), /无法读取/);
  keys.syncOwner(s, 'b'.repeat(64));
  s.setStorageSync = () => { throw new Error('storage full'); };
  assert.throws(() => keys.claim(s, 'b'.repeat(64), {}), /storage full/);
});
