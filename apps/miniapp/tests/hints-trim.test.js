// 10/17 创始人："把小程序上面没有用的提示去掉，只保留有用的提示信息，比如第三方整理、非政府平台这样的"。
// 规则：出处说明（第三方整理 · 非政府平台）由顶部 notice-bar 统一写一次，页面里不再重复；
// 和具体数据挂钩的准确性说明、隐私说明、AI 投诉渠道照旧保留，删提示时不能把它们一起删掉。
// 10/29 创始人第二轮："这里面有些多余的提示，去掉，没有必要……不要写太多提示，这样用户看到可能会引起信任问题"。
// Claude、Codex、agy 各自挑过再汇总，规则改成：
// - 同一页"以原文为准"一类的话只写一处：详情页在页底原文出处，截止页在页底说明；每个截止日期旁的状态标签照旧（D-061）；
// - 标签后面不再跟套话："截止未核对，请以原文为准"写成"截止未核对"；
// - 内部怎么计数、去重、解析的不写（转发版本不重复计、"认不出列名"等）；看界面就懂的不写；
// - 出错、空结果、没收录时的下一步，"不判断能否申报"、"不能据此判断不在名单里"这类防误解、防征信的话照旧。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
// 只看会显示出来的部分：去掉 <!-- 注释 -->
const wxml = (page) => read(`pages/${page}/index.wxml`).replace(/<!--[\s\S]*?-->/g, '');

const WITH_NOTICE_BAR = ['about', 'advisor', 'advisor-clause', 'advisor-form', 'advisor-interpret', 'advisor-tasks', 'advisor-trial', 'ai', 'help',
  'advisor-terms', 'agreement', 'clients', 'detail', 'feedback', 'files', 'follow', 'follow-new', 'lists', 'me',
  'my-data', 'orders', 'pass', 'report', 'search'];

test('出处说明只在顶部 notice-bar 写一次：各页都有 notice-bar，页面里不再另写"第三方整理"', () => {
  assert.match(read('components/notice-bar/index.wxml'), /第三方整理 · 非政府平台 · 每条注明出处/);
  for (const page of WITH_NOTICE_BAR) {
    const template = wxml(page);
    assert.equal((template.match(/<notice-bar\b/g) || []).length, 1, page);
    // 例外：关于页的品牌副标题与后台简介同一口径；购买页的付费须知是付费服务的法律说明
    if (!['about', 'pass'].includes(page)) assert.ok(!template.includes('第三方整理'), `${page} 又写了一遍第三方整理`);
  }
  // 首页没有 notice-bar，顶部角标就是唯一一处；底部不再重复
  const home = wxml('home');
  assert.equal((home.match(/第三方整理/g) || []).length, 1);
  assert.match(home, /<text class="badge">第三方整理 · 非政府平台<\/text>/);
  // 我的页底部那行已去掉
  assert.ok(!wxml('me').includes('class="foot"'));
});

test('AI 页面底部只留投诉举报链接', () => {
  for (const page of ['advisor-clause', 'advisor-interpret']) {
    assert.match(wxml(page), /<view class="advisor-footer"><text class="link" bindtap="report">投诉举报<\/text><\/view>/, page);
  }
  assert.match(wxml('advisor'), /<view wx:if="{{aiEnabled}}" class="advisor-footer"><text class="link" bindtap="report">投诉举报<\/text><\/view>/);
  for (const page of ['advisor-form', 'advisor-tasks', 'advisor-terms', 'advisor-trial']) assert.ok(!wxml(page).includes('advisor-footer"'), page);
});

test('和具体数据挂钩的准确性说明、隐私说明照旧保留', () => {
  const RULE = '城市选“全部”查全省；区县选“全部”只查这个市的市级来源，直辖市查整个直辖市。';
  const keep = {
    // 详情页：状态标签、页底出处里的"以原文为准"、"不判断能否申报"、不提供填报入口
    detail: ['<view wx:else class="due-unverified">截止未核对</view>', '以上要点由有据按原文逐条整理，{{doc.checked}} 核对。', '有据不判断能否申报', '（自动提取）',
      '类型、类别和发布阶段按标题归类，非官方分类', '申报请在主管部门的申报系统办理，有据不提供填报入口。'],
    report: ['截止日期以原文为准。', '截止未核对', '7 天内到期的只列人工核对过的日期', '不代表当地没有通知', RULE],
    search: [RULE, '{{openNote}}'],
    // D-091：没查到的说明按份数写在 missNote 里（不能据此判断不在名单里）；同名提示改成"不一定是同一家"
    lists: ['{{missNote}}', '名称相近的企业分开列出，不一定是同一家', '请核对企业全称再查；'],
    clients: ['已确认截止的不列', '这里的客户卡片不会自动发送', '要另外确认上传', '选省包含该省已收录的城市、区县', RULE, '不判断客户能不能申报', '客户名称只存在本机，导出的表里不含客户名称。',
      '这里的客户卡片只保存在本机，不上传'],
    files: ['导出记录只保存在这台手机上'],
    feedback: ['请不要填写姓名、电话等个人信息'],
    // agy 审阅（10/17）：首页没有 notice-bar，"不代办申报"要留一处
    home: ['有据不代办申报。'],
  };
  for (const [page, texts] of Object.entries(keep)) {
    const template = wxml(page);
    for (const text of texts) assert.ok(template.includes(text), `${page} 缺少：${text}`);
  }
  // 10/29：详情页、截止页"以原文为准"整页只出现一次
  for (const page of ['detail', 'report']) assert.equal((wxml(page).match(/以原文为准/g) || []).length, 1, page);
  // 详情页那一处不挂在"有核对日期"的条件上：没核对日期的通知也要有（Codex 10/29 复审）
  const { render, textOf, findAll, hasClass } = require('./wxml-render.js');
  for (const checked of ['10月28日', '']) {
    const root = render('pages/detail/index.wxml', { doc: { title: '关于组织申报的通知', issuer: '科技厅', date: '2026-10-20', checked }, loaded: true });
    const note = textOf(findAll(root, hasClass('source-note'))[0]);
    assert.equal((textOf(root).match(/以原文为准/g) || []).length, 1, checked || '没有核对日期');
    assert.match(note, /内容以原文为准；申报请在主管部门的申报系统办理，有据不提供填报入口。/);
    assert.equal(note.includes('以上要点由有据按原文逐条整理'), Boolean(checked));
  }
});

test('删掉的重复或没有用的提示不再出现', () => {
  const gone = {
    detail: ['按各版本原文分别核对', '仅供阅读参考', '推荐截止', '有据是第三方整理的信息工具', '非官方分类，以原文为准',
      // 10/29 第二轮
      '<text class="weak">以原文为准</text>', '请以原文为准', '条件可能未列全', '各版本的截止日期、报送要求可能不同', '未经人工逐字核对', '截止日期可先看', '一切以原文为准',
      '检查时已打不开。', '仅供检索参考', 'attNote'],
    me: ['有据是第三方整理的信息工具', '不绕过', '每条都有出处', '每条都注明出处', '地市目前只收录部分城市', '区县目前只收录少数地方'],
    home: ['有据是第三方整理的信息工具', '未重复计算', '每条都有出处', '点开看是哪些'],
    ai: ['每条都有出处', '点标题看通知全文'],
    help: ['每条都有出处'],
    files: ['数据说明（来源网站', '导出不是必须的', '为节省空间'],
    follow: ['就能把当前的关键词和筛选条件存下来'],
    report: ['最终以原文为准', '未收录的城市、区县会明确提示', '直辖市不选区县时也是', '机器摘录仅供参考', '请以原文为准', '只统计已收录网站的文件', '城市一栏选', '人工核对中'],
    search: ['选择器无法使用时请升级微信', '同一通知的各地转发版本合为一条', '直辖市不选区县时也是', '不重复计数', '城市一栏选', '直辖市例外'],
    clients: ['需要批量处理或生成 Excel', '选择器无法使用时请升级微信', '选择具体区县会替换所属城市和全省的条件', '一条一行列出来', '直辖市不选区县时也是',
      '点一下移除', '省市区选项来自微信', '城市一栏选'],
    results: ['来源核对：', '变更核对：'],
    'advisor-interpret': ['原网页可能已撤下', '可能已撤回或更正', '正文没变'],
    'advisor-clause': ['原网页可能已撤下', '可能已撤回或更正', '正文没变', '不表示当地的数据已收录'],
  };
  for (const [page, texts] of Object.entries(gone)) {
    const template = wxml(page);
    for (const text of texts) assert.ok(!template.includes(text), `${page} 还有：${text}`);
  }
});

test('10/29 第二轮：程序里拼出来的提示也一起压短（截止状态、筛选口径、名单没查到、AI 页顶部、客服）', () => {
  const format = require('../utils/format.js');
  const conditions = require('../utils/conditions.js');
  const advisor = require('../utils/advisor.js');
  const reasons = require('../utils/ai-reasons.js');
  const attachments = require('../utils/attachments.js');
  assert.equal(format.NOTE_UNVERIFIED, '截止未核对');
  assert.equal(conditions.SUPPORT_NOTE, '只含申报通知和名单，不代表一定提供这种支持');
  assert.equal(conditions.INDUSTRY_NOTE, undefined);
  assert.equal(attachments.NOTE_READ, undefined);
  // AI 标识照旧，后面的说明不再写"可能不全"；"不判断能否申报"保留（agy：防资格判断、防征信）
  assert.equal(advisor.internalNotice(null), '摘录通知原文原句，不判断能否申报，以原文和主管部门解释为准。');
  assert.doesNotMatch(reasons.reasonView('link_gone', true).text, /可能已撤下|为免误导/);
  // 名单没查到：先说不能据此判断不在名单里，再按份数说，不写"扫描件、旧格式、表格没认出来"这类内部原因
  const lists = read('pages/lists/index.js');
  assert.match(lists, /return `不能据此判断这家企业不在名单里：\$\{parts\.join\('，'\)\}。`;/);
  assert.doesNotMatch(lists, /旧格式|没认出来/);
  assert.match(lists, /只收录了科技、工信等部门的公开名单，地市发布的还没收录/);
  const reader = read('pages/reader/index.js');
  assert.doesNotMatch(reader, /认不出列名|以上面的原文为准/);
  const search = read('pages/search/index.js');
  assert.doesNotMatch(search, /不代表全市数据完整|不代表区县数据完整|省市区选项由微信提供|市区目前仅收录部分来源/);
  const contact = read('components/contact-button/index.wxml');
  assert.doesNotMatch(contact, /一般当天或第二天回复|客服只解答/);
  // agy 复审："不代办"和"不判断能否申报"是两条边界，都要写
  assert.match(contact, /客服解答小程序的使用问题，不代办申报、不判断能否申报/);
  // 合规要求的照旧：AI 标识、第十条公开、模型和备案号、投诉举报
  assert.match(read('pages/ai/index.wxml'), /AI 生成 · 以原文为准/);
  assert.equal(advisor.SCOPE.length, 4);
  for (const page of ['advisor-interpret', 'advisor-clause']) assert.match(wxml(page), /bindtap="report">投诉举报/);
});
