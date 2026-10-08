const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
function walk(dir, out = []) {
  for (const name of fs.readdirSync(dir)) {
    const p = path.join(dir, name);
    if (fs.statSync(p).isDirectory()) walk(p, out);
    else if (p.endsWith('.js')) out.push(p);
  }
  return out;
}

test('真机兼容：页面和工具代码不用 Promise.finally（部分旧机型没有）', () => {
  const files = ['pages', 'utils', 'components', 'custom-tab-bar'].flatMap((d) => walk(path.join(root, d)));
  for (const file of files) {
    assert.doesNotMatch(fs.readFileSync(file, 'utf8'), /\.finally\(/, `${path.relative(root, file)} 用了 .finally`);
  }
});

test('真机兼容：按二进制取回的请求都同时设 dataType other（否则云托管 SDK 按文字解析报错）', () => {
  const source = fs.readFileSync(path.join(root, 'utils/api.js'), 'utf8');
  const binary = source.match(/responseType: 'arraybuffer'/g) || [];
  const other = source.match(/dataType: 'other'/g) || [];
  assert.ok(binary.length >= 4);
  assert.equal(other.length, binary.length);
});
