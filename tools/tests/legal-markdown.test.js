// 协议转 Markdown（tools/legal_markdown.js）：--sanitize 发给外部模型审阅前用，运营主体信息一点都不能留
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { render, sanitize } = require('../legal_markdown.js');

test('脱敏：名称是地址的一部分、邮箱大小写不同、多出来的字段，都换成占位', () => {
  const operator = { name: '某某科技有限公司', address: '烟台市芝罘区某某科技有限公司大厦 1 号', email: 'Contact@Example.com', icp: '鲁ICP备1号', extra: '张三' };
  const text = '由某某科技有限公司运营，地址烟台市芝罘区某某科技有限公司大厦 1 号，邮箱 contact@example.com，备案 鲁ICP备1号，联系人张三。';
  assert.equal(sanitize(text, operator), '由【运营主体】运营，地址【地址】，邮箱 【联系邮箱】，备案 【备案号】，联系人【运营信息】。');
  // 正则里的特殊字符按原样匹配
  assert.equal(sanitize('写信到 a+b@x.cn 或 a+b@x.cn', { email: 'a+b@x.cn' }), '写信到 【联系邮箱】 或 【联系邮箱】');
  assert.equal(sanitize('没有运营信息', {}), '没有运营信息');
});

test('真实协议转出来的脱敏版里没有运营主体信息；不脱敏时三份全文都在', () => {
  for (const file of ['legal.js', 'legal-first-release.js', 'legal-first-release-ai.js']) {
    const legal = require(path.join(__dirname, '..', '..', 'apps', 'miniapp', 'content', file));
    const clean = render(legal, { clean: true });
    for (const value of Object.values(legal.operator || {})) {
      if (typeof value === 'string' && value.trim()) assert.equal(clean.toLowerCase().includes(value.trim().toLowerCase()), false, `${file} 里还有运营主体信息`);
    }
    const full = render(legal);
    for (const head of ['## 用户协议', '## 隐私保护指引', '## 免责声明']) assert.ok(full.includes(head), `${file}：${head}`);
    assert.ok(full.includes(`版本 ${legal.version}`));
  }
});
