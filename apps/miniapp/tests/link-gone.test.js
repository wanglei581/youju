// 原网页已撤下或改了地址（后台 youju/link_check.py 连续两次打不开才算）：卡片标"原网页已失效"，
// 详情页写明哪天检查时打不开、复制的链接可能打不开（10/15 全面测试：近 30 天 832 份里 20 份原网页已打不开）
const test = require('node:test');
const assert = require('node:assert/strict');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');

test('卡片：原网页失效的标出来，没失效的不标', () => {
  const base = { doc: { kind: '申报通知', title: '关于开展申报的通知', issuer: '某厅', date: '2026-10-01' }, segs: [{ t: '关于开展申报的通知' }], kindCls: 'apply' };
  const gone = render('components/doc-row/index.wxml', { ...base, doc: { ...base.doc, link_gone: '2026-10-16' } });
  assert.equal(findAll(gone, hasClass('gone')).map((n) => textOf(n).trim()).join(), '原网页已失效');
  const ok = render('components/doc-row/index.wxml', base);
  assert.equal(findAll(ok, hasClass('gone')).length, 0);
});

test('详情页：写明哪天检查时打不开，给出下一步', () => {
  const tree = render('pages/detail/index.wxml', { doc: { title: '关于开展申报的通知', host: 'kjt.example.gov.cn', link_gone: '2026-10-16' }, loaded: true });
  const note = findAll(tree, hasClass('source-gone')).map((n) => textOf(n).trim()).join('');
  assert.match(note, /原网页已撤下或改了地址（2026-10-16 检查时打不开）/);
  assert.match(note, /可以到发文机关网站按标题查找/);
  const fine = render('pages/detail/index.wxml', { doc: { title: '关于开展申报的通知', host: 'kjt.example.gov.cn' }, loaded: true });
  assert.equal(findAll(fine, hasClass('source-gone')).length, 0);
});
