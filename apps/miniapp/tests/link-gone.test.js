// 原文链接已失效（后台 youju/link_check.py：近 30 天的每天查，隔天两次都打不开或是撤稿页才算；分不清是撤稿
// 还是改了地址，只说链接失效）：卡片标"原文链接已失效"，详情页标题下先写明、正文是之前收录的，底部写明留存内容见站内阅读页（10/23 起不放复制按钮）
// （10/15 全面测试：近 30 天 832 份里 20 份原网页已打不开；10/22 宁夏科技厅一份申报通知第二天撤稿）。
// 失效以后不能再开始 AI 解读（原因代码 link_gone），已有的结果照常能看、写明基于此前收录的正文
const test = require('node:test');
const assert = require('node:assert/strict');
const { render, textOf, findAll, hasClass } = require('./wxml-render.js');
const reasons = require('../utils/ai-reasons.js');

test('卡片：原文链接失效的标出来，没失效的不标', () => {
  const base = { doc: { kind: '申报通知', title: '关于开展申报的通知', issuer: '某厅', date: '2026-10-01' }, segs: [{ t: '关于开展申报的通知' }], kindCls: 'apply' };
  const gone = render('components/doc-row/index.wxml', { ...base, doc: { ...base.doc, link_gone: '2026-10-16' } });
  assert.equal(findAll(gone, hasClass('gone')).map((n) => textOf(n).trim()).join(), '原文链接已失效');
  const ok = render('components/doc-row/index.wxml', base);
  assert.equal(findAll(ok, hasClass('gone')).length, 0);
});

test('详情页：标题下先写明链接失效了、正文是之前收录的；底部写明哪天检查时打不开，给出下一步', () => {
  const tree = render('pages/detail/index.wxml', { doc: { title: '关于开展申报的通知', host: 'kjt.example.gov.cn', link_gone: '2026-10-16' }, loaded: true });
  const top = findAll(tree, hasClass('gone-note')).map((n) => textOf(n).trim()).join('');
  assert.match(top, /原文链接已失效（2026-10-16 检查时打不开，原网页可能已撤下或改了地址）/);
  assert.match(top, /下面是之前收录的内容，通知可能已撤回或更正/);
  const note = findAll(tree, hasClass('source-gone')).map((n) => textOf(n).trim()).join('');
  assert.match(note, /原文链接已失效（2026-10-16 检查时打不开）；留存的内容和状态见站内阅读页/);
  assert.doesNotMatch(note, /复制/);
  const fine = render('pages/detail/index.wxml', { doc: { title: '关于开展申报的通知', host: 'kjt.example.gov.cn' }, loaded: true });
  assert.equal(findAll(fine, hasClass('source-gone')).length, 0);
  assert.equal(findAll(fine, hasClass('gone-note')).length, 0);
});

test('不能解读的原因 link_gone：写明链接失效了、为什么不解读，不给下一步（复制的链接也打不开）', () => {
  assert.deepEqual(reasons.reasonView('link_gone', true), { text: '原文链接已失效，原网页可能已撤下或改了地址。为免误导，这份通知暂不提供 AI 解读和条款对照，请以发文机关现在公布的为准。', next: '', nextText: '' });
});

test('解读、对照结果页：原文链接失效了照常显示结果，顶部写明基于此前收录的正文；解读结果下不再给条款对照', () => {
  const result = { sections: [{ type: '对象', title: '申报对象', items: [{ key: 0, text: '科技型企业。', refs: [] }] }], verified: [], noticeText: '' };
  for (const page of ['advisor-interpret', 'advisor-clause']) {
    const base = { aiEnabled: true, loading: false, phase: 'done', docId: '42', title: '关于开展申报的通知', result, clauseEnabled: true };
    const gone = render(`pages/${page}/index.wxml`, { ...base, linkGone: '2026-10-24' });
    const note = findAll(gone, hasClass('advisor-gone')).map((n) => textOf(n).trim()).join('');
    assert.match(note, /原文链接 2026-10-24 检查时已失效，原网页可能已撤下或改了地址。这份结果基于此前收录的正文/, page);
    assert.ok(textOf(gone).includes('科技型企业。'), `${page} 结果照常显示`);
    const fine = render(`pages/${page}/index.wxml`, base);
    assert.equal(findAll(fine, hasClass('advisor-gone')).length, 0, page);
    if (page === 'advisor-interpret') {
      assert.ok(textOf(fine).includes('条款对照（可选）'));
      assert.ok(!textOf(gone).includes('条款对照（可选）'), '失效了不再给条款对照');
    }
  }
});

test('我的记录、AI 首页的记录：原文链接失效了写一句', () => {
  const item = { id: 1, kind: 'interpret', kindLabel: 'AI 解读', docTitle: '关于开展申报的通知', stateLabel: '已完成', time: '2026-10-24 10:00', status: 'done' };
  const tasks = render('pages/advisor-tasks/index.wxml', { loading: false, toolsEnabled: true, items: [{ ...item, linkGone: '2026-10-24' }, item] });
  assert.equal(textOf(tasks).split('原文链接已失效，结果基于此前收录的正文，仅供参考').length - 1, 1);
  const home = render('pages/ai/index.wxml', { records: [{ id: '1', kind: 'interpret', kindLabel: 'AI 解读', title: '通知', time: '2026-10-24', stateLabel: '已完成', linkGone: '2026-10-24' }] });
  assert.ok(findAll(home, hasClass('record-meta')).map((n) => textOf(n)).join('').includes('· 原文链接已失效'));
});
