// 仅存账号摘要、内容摘要、幂等键和有效期，不存企业输入。未知结果不因过期自动换键。
const STORAGE_KEY = 'youjuDataTaskKeysV1';
const TTL = 24 * 60 * 60 * 1000;
const MAX_PENDING = 20;
const MAX_TOTAL = 40; // 本机所有账号合计；空分区不落盘，分区数也不超过它。
const MAX_BODY_BYTES = 100 * 1024; // 与后端 advisor_tasks.MAX_BODY_BYTES 一致。
const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
const rotate = (x, n) => (x >>> n) | (x << (32 - n));
function sha256(text) {
  const hash = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  const block = new Uint8Array(64); const w = new Uint32Array(64);
  let used = 0; let bytes = 0;
  const push = (byte) => {
    block[used++] = byte; bytes += 1;
    if (used !== 64) return;
    for (let i = 0; i < 16; i += 1) w[i] = (block[i*4] << 24) | (block[i*4+1] << 16) | (block[i*4+2] << 8) | block[i*4+3];
    for (let i = 16; i < 64; i += 1) {
      const a = w[i-15]; const b = w[i-2];
      w[i] = (w[i-16] + (rotate(a,7) ^ rotate(a,18) ^ (a >>> 3)) + w[i-7] + (rotate(b,17) ^ rotate(b,19) ^ (b >>> 10))) >>> 0;
    }
    let [a,b,c,d,e,f,g,h] = hash;
    for (let i = 0; i < 64; i += 1) {
      const t1 = (h + (rotate(e,6) ^ rotate(e,11) ^ rotate(e,25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) >>> 0;
      const t2 = ((rotate(a,2) ^ rotate(a,13) ^ rotate(a,22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      h=g; g=f; f=e; e=(d+t1)>>>0; d=c; c=b; b=a; a=(t1+t2)>>>0;
    }
    [a,b,c,d,e,f,g,h].forEach((v,i) => { hash[i] = (hash[i]+v)>>>0; });
    used = 0;
  };
  // 固定 64 字节缓冲；不把 base64 输入展开成全量普通 JS 数字数组。
  for (const char of text) {
    let c = char.codePointAt(0);
    if (c >= 0xd800 && c <= 0xdfff) c = 0xfffd;
    if (c < 0x80) push(c);
    else if (c < 0x800) { push(0xc0 | (c >>> 6)); push(0x80 | (c & 63)); }
    else if (c < 0x10000) { push(0xe0 | (c >>> 12)); push(0x80 | ((c >>> 6) & 63)); push(0x80 | (c & 63)); }
    else { push(0xf0 | (c >>> 18)); push(0x80 | ((c >>> 12) & 63)); push(0x80 | ((c >>> 6) & 63)); push(0x80 | (c & 63)); }
  }
  const bits = bytes * 8; push(0x80);
  while (used !== 56) push(0);
  for (const value of [Math.floor(bits / 0x100000000), bits >>> 0]) for (let n = 24; n >= 0; n -= 8) push((value >>> n) & 255);
  return hash.map(v => v.toString(16).padStart(8,'0')).join('');
}
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
const isHex = v => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
const isMap = v => !!v && typeof v === 'object' && !Array.isArray(v);
const validList = list => Array.isArray(list) && list.length <= MAX_PENDING && list.every(e => isMap(e) && isHex(e.digest) && typeof e.key === 'string' && e.key.length > 0 && e.key.length <= 128 && Number.isFinite(e.expiresAt));
// 当前账号在 entries（与旧版 {owner, entries} 同形，旧记录原样可读），其他账号的未确认键按账号留在 others：
// 切换账号只换分区，切回后重填同一内容仍用原键。任何一处不合格都停下，不改写也不丢弃。
function load(storage) {
  const saved = storage.getStorageSync(STORAGE_KEY);
  if (saved === '' || saved === undefined || saved === null) return { owner: '', entries: [], others: {} };
  const others = isMap(saved) && saved.others !== undefined ? saved.others : {};
  const names = isMap(others) ? Object.keys(others) : [];
  if (!isMap(saved) || !isHex(saved.owner) || !validList(saved.entries) || !isMap(others) || names.length > MAX_TOTAL
    || names.some(name => !isHex(name) || name === saved.owner || !validList(others[name]))
    || names.reduce((sum, name) => sum + others[name].length, saved.entries.length) > MAX_TOTAL) throw new Error('安全重试记录无法读取，请先到任务中心核对');
  return { owner: saved.owner, entries: saved.entries, others: { ...others } };
}
function save(storage, state) {
  const others = {};
  Object.keys(state.others).forEach((name) => { if (state.others[name].length) others[name] = state.others[name]; });
  storage.setStorageSync(STORAGE_KEY, Object.keys(others).length ? { owner: state.owner, entries: state.entries, others } : { owner: state.owner, entries: state.entries });
}
function syncOwner(storage, owner) {
  if (!isHex(owner)) throw new Error('未能确认当前账号，请重新进入工具');
  const state = load(storage);
  if (state.owner === owner) return;
  if (state.owner) state.others[state.owner] = state.entries;
  state.entries = state.others[owner] || [];
  delete state.others[owner];
  state.owner = owner;
  save(storage, state);
}
function claim(storage, owner, body, stamp = Date.now()) {
  try {
    if (!isHex(owner)) throw new Error('未能确认当前账号，请重新进入工具');
    const state = load(storage);
    // 已切到其他账号或记录随注销清空：旧页面不能借当前会话发出请求。
    if (state.owner !== owner) throw new Error('账号状态已变化，请重新进入工具后重试');
    const content = canonical(body);
    let size = 0;
    for (const char of content) {
      const cp = char.codePointAt(0); size += cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
      if (size > MAX_BODY_BYTES) throw new Error('提交内容不能超过 100 KB，请减少输入');
    }
    const digest = sha256(owner + '\n' + content);
    const existing = state.entries.find(e => e.digest === digest);
    if (existing) {
      // TTL 到期后仍复用未知请求的键，续期摘要；不能把未知结果误当成可重新扣额的新提交。
      existing.expiresAt = stamp + TTL; save(storage, state);
      return { ...existing, existing: true, owner };
    }
    if (state.entries.length >= MAX_PENDING) throw new Error('未确认的提交较多，请先到任务中心核对并重试原内容');
    if (Object.keys(state.others).reduce((sum, name) => sum + state.others[name].length, state.entries.length) >= MAX_TOTAL) throw new Error('本机其他账号的未确认提交较多，请先回到原账号，在任务中心核对并重试原内容');
    const entry = { digest, key: `data-${stamp.toString(36)}-${Math.random().toString(36).slice(2)}-${digest.slice(0,16)}`, expiresAt: stamp + TTL };
    state.entries.push(entry); save(storage, state);
    return { ...entry, existing: false, owner };
  } catch (err) { throw new Error(err.message || '无法保存安全重试记录，请检查手机存储后重试'); }
}
function confirm(storage, request) {
  if (!request || !isHex(request.owner)) return;
  const state = load(storage);
  // 只改发出请求的账号分区：旧账号的迟到回调碰不到当前账号；记录已清空（注销）时不重建。
  const current = state.owner === request.owner;
  const list = current ? state.entries : state.others[request.owner];
  if (!list) return;
  const rest = list.filter(e => e.key !== request.key);
  if (rest.length === list.length) return;
  if (current) state.entries = rest; else state.others[request.owner] = rest;
  save(storage, state);
}
module.exports = { STORAGE_KEY, TTL, MAX_PENDING, MAX_TOTAL, sha256, canonical, syncOwner, claim, confirm };
