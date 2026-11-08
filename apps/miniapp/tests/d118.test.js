const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { dataKinds } = require('../utils/advisor.js');

test('缺少明确开关时批量名单工具关闭，开启后仍可恢复', () => {
  const data = { enabled: true, features: ['compare', 'track'] };
  assert.deepEqual(dataKinds(data), ['track']);
  assert.deepEqual(dataKinds({ ...data, compareEnabled: false }), ['track']);
  assert.deepEqual(dataKinds({ ...data, compareEnabled: true }), ['compare', 'track']);
});

test('完整版页面及非协议内容无比对字样，单家查名单除外', () => {
  const root = path.join(__dirname, '..');
  const pages = require('../app.json').pages;
  for (const page of pages) {
    if (page.startsWith('pages/lists/')) continue;
    for (const ext of ['js', 'json', 'wxml', 'wxss']) {
      const file = path.join(root, `${page}.${ext}`);
      if (fs.existsSync(file)) assert.doesNotMatch(fs.readFileSync(file, 'utf8'), /比对/, file);
    }
  }
  for (const name of fs.readdirSync(path.join(root, 'content'))) {
    if (/^(legal.*|pay-terms)\.js$/.test(name)) continue;
    const file = path.join(root, 'content', name);
    if (fs.statSync(file).isFile()) assert.doesNotMatch(fs.readFileSync(file, 'utf8'), /比对/, file);
  }
});
