// 与后台 compare.py 共用的上传边界：202 行、64 列、12928 单元格、2 MiB 解压总量。
const MAX_ROWS = 202; const MAX_COLUMNS = 64; const MAX_CELLS = MAX_ROWS * MAX_COLUMNS;
const MAX_BYTES = 2 * 1024 * 1024; const MAX_ELEMENTS = 100000; const MAX_ENTRIES = 512;
function fail(message) { const err = new Error(message); err.status = 422; throw err; }
const CRC_TABLE = Array.from({ length: 256 }, (_, value) => {
  for (let n = 0; n < 8; n += 1) value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0);
  return value >>> 0;
});
function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) crc = (crc >>> 8) ^ CRC_TABLE[(crc ^ byte) & 255];
  return (crc ^ 0xffffffff) >>> 0;
}
function utf8(bytes) { return decodeURIComponent(Array.from(bytes, (n) => `%${n.toString(16).padStart(2, '0')}`).join('')); }
function csvText(bytes) {
  try { return utf8(bytes); } catch (e) { /* UTF-8 无效时再尝试 GBK */ }
  const table = require('./gbk-map.js'); let text = '';
  for (let i = 0; i < bytes.length; i += 1) {
    const lead = bytes[i];
    if (lead < 128) { text += String.fromCharCode(lead); continue; }
    const trail = bytes[++i];
    if (lead < 129 || lead > 254 || trail < 64 || trail > 254 || trail === 127 || trail === undefined) fail('CSV 不是 UTF-8 或 GBK 编码');
    const ch = table[(lead - 129) * 190 + trail - 64 - (trail > 127 ? 1 : 0)];
    if (ch === '\ufffd') fail('CSV 不是 UTF-8 或 GBK 编码');
    text += ch;
  }
  return text;
}
// 在内存上限内解 DEFLATE，不能先调用无大小限制的系统 unzip 再检查大小。
function inflate(data, size) {
  let offset = 0; let bits = 0; let available = 0; let count = 0;
  const out = new Uint8Array(size);
  function read(n) {
    while (available < n) { if (offset >= data.length) fail('无法读取 XLSX 文件'); bits |= data[offset++] << available; available += 8; }
    const value = bits & ((1 << n) - 1); bits >>>= n; available -= n; return value;
  }
  function tree(lengths) {
    const counts = new Array(16).fill(0); const codes = new Array(16).fill(0); const maps = [];
    lengths.forEach((n) => { if (n) counts[n] += 1; });
    let code = 0;
    for (let n = 1; n <= 15; n += 1) { code = (code + counts[n - 1]) << 1; codes[n] = code; maps[n] = {}; if (code + counts[n] > 1 << n) fail('无法读取 XLSX 文件'); }
    lengths.forEach((n, symbol) => {
      if (!n) return;
      let value = codes[n]++; let reverse = 0;
      for (let i = 0; i < n; i += 1) { reverse = (reverse << 1) | (value & 1); value >>>= 1; }
      maps[n][reverse] = symbol;
    });
    return maps;
  }
  function symbol(maps) {
    let value = 0;
    for (let n = 1; n <= 15; n += 1) { value |= read(1) << (n - 1); if (maps[n][value] !== undefined) return maps[n][value]; }
    fail('无法读取 XLSX 文件');
  }
  function write(value) { if (count >= size) fail('表格解压后过大或大小声明无效'); out[count++] = value; }
  const lengthBase = [3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,35,43,51,59,67,83,99,115,131,163,195,227,258];
  const lengthExtra = [0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5,0];
  const distanceBase = [1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,257,385,513,769,1025,1537,2049,3073,4097,6145,8193,12289,16385,24577];
  const distanceExtra = [0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10,11,11,12,12,13,13];
  let final;
  do {
    final = read(1); const type = read(2);
    if (type === 0) {
      read(available % 8); const len = read(16); if ((len ^ read(16)) !== 65535) fail('无法读取 XLSX 文件');
      for (let i = 0; i < len; i += 1) write(read(8));
      continue;
    }
    let literal; let distance;
    if (type === 1) {
      literal = tree(Array.from({ length: 288 }, (_, n) => n < 144 ? 8 : n < 256 ? 9 : n < 280 ? 7 : 8));
      distance = tree(new Array(32).fill(5));
    } else if (type === 2) {
      const nl = read(5) + 257; const nd = read(5) + 1; const nc = read(4) + 4;
      const order = [16,17,18,0,8,7,9,6,10,5,11,4,12,3,13,2,14,1,15]; const lengths = new Array(19).fill(0);
      for (let i = 0; i < nc; i += 1) lengths[order[i]] = read(3);
      const codeTree = tree(lengths); const all = [];
      while (all.length < nl + nd) {
        const code = symbol(codeTree);
        if (code < 16) all.push(code);
        else {
          if (code === 16 && !all.length) fail('无法读取 XLSX 文件');
          const repeat = code === 16 ? read(2) + 3 : code === 17 ? read(3) + 3 : read(7) + 11;
          const value = code === 16 ? all[all.length - 1] : 0;
          if (all.length + repeat > nl + nd) fail('无法读取 XLSX 文件');
          for (let i = 0; i < repeat; i += 1) all.push(value);
        }
      }
      if (!all[256]) fail('无法读取 XLSX 文件');
      literal = tree(all.slice(0, nl)); distance = tree(all.slice(nl));
    } else fail('无法读取 XLSX 文件');
    while (true) {
      const code = symbol(literal);
      if (code < 256) { write(code); continue; }
      if (code === 256) break;
      if (code > 285) fail('无法读取 XLSX 文件');
      const index = code - 257; const length = lengthBase[index] + read(lengthExtra[index]); const d = symbol(distance);
      if (d > 29) fail('无法读取 XLSX 文件');
      const back = distanceBase[d] + read(distanceExtra[d]); if (back > count) fail('无法读取 XLSX 文件');
      for (let i = 0; i < length; i += 1) write(out[count - back]);
    }
  } while (!final);
  if (count !== size) fail('无法读取 XLSX 文件');
  return out;
}
function zipFiles(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength); let end = -1;
  for (let n = bytes.length - 22; n >= Math.max(0, bytes.length - 65557); n -= 1) {
    if (view.getUint32(n, true) === 0x06054b50 && n + 22 + view.getUint16(n + 20, true) === bytes.length) { end = n; break; }
  }
  if (end < 0) return null;
  const entries = view.getUint16(end + 10, true); let offset = view.getUint32(end + 16, true); let total = 0; const records = []; const files = Object.create(null);
  if (entries > MAX_ENTRIES) fail('表格包含过多文件，请减少内容后重试');
  for (let n = 0; n < entries; n += 1) {
    if (offset + 46 > end || view.getUint32(offset, true) !== 0x02014b50) return null;
    const packed = view.getUint32(offset + 20, true); const size = view.getUint32(offset + 24, true);
    const nameSize = view.getUint16(offset + 28, true); const extra = view.getUint16(offset + 30, true); const comment = view.getUint16(offset + 32, true);
    total += size; if (total > MAX_BYTES) fail('表格解压后过大，请减少内容后重试');
    const name = utf8(bytes.subarray(offset + 46, offset + 46 + nameSize));
    if (name.startsWith('/') || name.split('/').includes('..') || name.includes('\\') || records.some((r) => r.name === name)) fail('无法读取 XLSX 文件');
    records.push({ name, size, packed, crc: view.getUint32(offset + 16, true), method: view.getUint16(offset + 10, true), flags: view.getUint16(offset + 8, true), local: view.getUint32(offset + 42, true) });
    offset += 46 + nameSize + extra + comment;
  }
  if (offset !== end || !records.some((r) => r.name === '[Content_Types].xml') || !records.some((r) => r.name === 'xl/workbook.xml') || !records.some((r) => r.name === 'xl/_rels/workbook.xml.rels')) return null;
  for (const record of records) {
    const start = record.local;
    if (start + 30 > bytes.length || view.getUint32(start, true) !== 0x04034b50 || record.flags & 1) fail('无法读取 XLSX 文件');
    const begin = start + 30 + view.getUint16(start + 26, true) + view.getUint16(start + 28, true);
    if (begin + record.packed > view.getUint32(end + 16, true)) fail('无法读取 XLSX 文件');
    const data = bytes.subarray(begin, begin + record.packed);
    const unpacked = record.method === 8 ? inflate(data, record.size) : record.method === 0 && data.length === record.size ? data : null;
    if (!unpacked || crc32(unpacked) !== record.crc) fail('无法读取 XLSX 文件');
    // 二进制成员也计入总大小，但只为需要解析的 XML 解码。
    files[record.name] = unpacked;
  }
  return files;
}
module.exports = { MAX_ROWS, MAX_COLUMNS, MAX_CELLS, MAX_ELEMENTS, utf8, csvText, zipFiles, fail };
