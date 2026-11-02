// 10/17 创始人："把小程序上面没有用的提示去掉，只保留有用的提示信息，比如第三方整理、非政府平台这样的"。
// 规则：出处说明（第三方整理 · 非政府平台）由顶部 notice-bar 统一写一次，页面里不再重复；
// 和具体数据挂钩的准确性说明、隐私说明、AI 投诉渠道照旧保留，删提示时不能把它们一起删掉。
// 10/29 创始人第二轮："这里面有些多余的提示，去掉，没有必要……不要写太多提示，这样用户看到可能会引起信任问题"。
// Claude、Codex、agy 各自挑过再汇总，规则改成：
// - 同一页"以原文为准"一类的话只写一处：详情页在页底原文出处，截止页在页底说明；每个截止日期旁的状态标签照旧（D-061）；
// - 标签后面不再跟套话："截止未核对，请以原文为准"写成"截止未核对"；
// - 内部怎么计数、去重、解析的不写（转发版本不重复计、"认不出列名"等）；看界面就懂的不写；
// - 出错、空结果、没收录时的下一步，"不判断能否申报"、"不能据此判断不在名单里"这类防误解、防征信的话照旧。
// 10/29 晚创始人又说了两条："自动摘取，未经人工核对不要写在页面上，可以写在帮助里面，增加一个帮助的功能"；
// 截图里正文卡片和底栏各有一个"阅读全文"——"类似这种问题……全面修复"。于是：
// - 截止标签"机器摘录·未经人工核对"改叫"原文摘录"（照旧附原句），没核对的写"截止时间见原文"；
//   正文"自动提取"、分类口径、截止规则、只看未截止、城市区县范围、查名单没查到说明什么，都写在使用帮助（content/help.js）；
//   页面上只在详情页、截止页各留一个去帮助的"说明 ›"（包里有帮助页才显示）；
// - 同一屏同一件事只放一个入口、只写一遍（阅读全文、发文机关、日期、地区、截止、份数、页码……）。
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
    detail: ["<view wx:else class=\"due-unverified\">{{item.soon ? '可能 7 天内截止' : '截止时间见原文'}}</view>", '截止日期已于 {{checkedText}} 对照原文核对。', '有据不判断能否申报',
      '内容以原文为准；申报请在主管部门的申报系统办理，有据不提供填报入口。', '数据说明 ›'],
    // 截止页：页底是"截止日期怎么看 ›"；包里没有使用帮助时照旧写那一句
    report: ['截止日期怎么看 ›', '截止日期以原文为准。', '截止时间见原文', '7 天内到期的只列人工核对过的日期', '不代表当地没有通知', RULE],
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
  for (const checked of ['2026年10月28日', '']) {
    const root = render('pages/detail/index.wxml', { doc: { title: '关于组织申报的通知', issuer: '科技厅', date: '2026-10-20' }, checkedText: checked, loaded: true });
    const note = textOf(findAll(root, hasClass('source-note'))[0]);
    assert.equal((textOf(root).match(/以原文为准/g) || []).length, 1, checked || '没有核对日期');
    assert.match(note, /内容以原文为准；申报请在主管部门的申报系统办理，有据不提供填报入口。/);
    // 核对日期说的是截止；"要点"常常整张不显示，不再说"以上要点"（10/29 独立审阅）
    // 日期写成中文、说"已于……对照原文核对"，免得读成截止日是这一天（10/29 独立审阅）
    assert.equal(note.includes('截止日期已于 2026年10月28日 对照原文核对。'), Boolean(checked));
    assert.ok(!note.includes('以上要点'));
  }
});

test('删掉的重复或没有用的提示不再出现', () => {
  const gone = {
    detail: ['按各版本原文分别核对', '仅供阅读参考', '推荐截止', '有据是第三方整理的信息工具', '非官方分类，以原文为准',
      // 10/29 第二轮
      '<text class="weak">以原文为准</text>', '请以原文为准', '条件可能未列全', '各版本的截止日期、报送要求可能不同', '未经人工逐字核对', '截止日期可先看', '一切以原文为准',
      '检查时已打不开。', '仅供检索参考', 'attNote',
      // 10/29 晚
      '（自动提取', '非官方分类', '机器摘录', '截止未核对', 'body-more', '发文机关：{{doc.issuer}}', 'doc.points',
      // 10/29 独立审阅第 3 轮：核对过的截止原句不再单列一张"原文摘录"卡；"原文摘录"只标机器摘出的截止；正文卡顶上那一行去掉
      '原文摘录', 'doc.excerpt.section', '以上要点', 'bodySummary'],
    me: ['有据是第三方整理的信息工具', '不绕过', '每条都有出处', '每条都注明出处', '地市目前只收录部分城市', '区县目前只收录少数地方'],
    home: ['有据是第三方整理的信息工具', '未重复计算', '每条都有出处', '点开看是哪些', '日期还没核对', 'soon-stale', '覆盖国家部委和', '份可能 7 天内截止 ›', '份通知可能 7 天内截止 ›'],
    about: ['每条都注明出处'],
    ai: ['每条都有出处', '点标题看通知全文', '我的 AI 记录'],
    help: ['每条都有出处'],
    files: ['数据说明（来源网站', '导出不是必须的', '为节省空间'],
    follow: ['就能把当前的关键词和筛选条件存下来'],
    report: ['最终以原文为准', '未收录的城市、区县会明确提示', '直辖市不选区县时也是', '机器摘录仅供参考', '请以原文为准', '只统计已收录网站的文件', '城市一栏选', '人工核对中',
      '日期还没核对', '截止未核对', '核对过的截止　空心点：机器摘录', '<text>截止时间见原文</text>'],
    search: ['选择器无法使用时请升级微信', '同一通知的各地转发版本合为一条', '直辖市不选区县时也是', '不重复计数', '城市一栏选', '直辖市例外', 'tagNote', "' ' + total"],
    clients: ['需要批量处理或生成 Excel', '选择器无法使用时请升级微信', '选择具体区县会替换所属城市和全省的条件', '一条一行列出来', '直辖市不选区县时也是',
      '点一下移除', '省市区选项来自微信', '城市一栏选', '非官方分类'],
    results: ['来源核对：', '变更核对：', '{{item.urls}}', '未核对：', 'item.date_role'],
    reader: ['反馈内容问题', 'page-label'],
    'advisor-tasks': ['查看解读', '查看对照', '结果基于此前收录的正文，仅供参考'],
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
  assert.equal(format.NOTE_UNVERIFIED, '截止时间见原文');
  assert.equal(format.EXCERPT_LABEL, '原文摘录');
  assert.equal(conditions.SUPPORT_NOTE, '只含申报通知和名单');
  assert.equal(conditions.INDUSTRY_NOTE, undefined);
  assert.equal(attachments.NOTE_READ, undefined);
  // AI 标识照旧，后面的说明不再写"可能不全"；"不判断能否申报"保留（agy：防资格判断、防征信）
  assert.equal(advisor.internalNotice(null), '摘录通知原文原句，不判断能否申报，以原文和主管部门解释为准。');
  assert.doesNotMatch(reasons.reasonView('link_gone', true).text, /可能已撤下|为免误导/);
  // 名单没查到：先说不能据此判断不在名单里，再按份数说，不写"扫描件、旧格式、表格没认出来"这类内部原因
  const lists = read('pages/lists/index.js');
  assert.match(lists, /return `不能据此判断这家企业不在名单里：\$\{parts\.join\('；'\)\}。`;/);
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

test('10/29 晚：数据说明都写在使用帮助里，页面上只留一个"说明 ›"；包里没有帮助页时不显示入口、截止页照旧写那一句', async () => {
  const help = require('../content/help.js');
  const keys = help.items.map((item) => item.key);
  for (const key of ['deadline', 'text', 'tags', 'versions', 'open', 'region', 'lists', 'update']) assert.ok(keys.includes(key), key);
  assert.equal(new Set(keys).size, keys.length, 'key 不重复');
  const text = (key) => help.items.find((item) => item.key === key).a.join('');
  // 页面上撤下来的几句，帮助里都写着
  assert.match(text('deadline'), /已人工核对.*原文摘录.*没有经过人工核对.*截止时间见原文.*可能 7 天内截止/);
  assert.match(text('text'), /自动提取的，没有经过人工逐字核对/);
  assert.match(text('tags'), /按标题归类，仅供检索参考，非官方分类/);
  assert.match(text('lists'), /不能这样判断/);
  for (const item of help.items.filter((i) => !i.ai)) assert.doesNotMatch(item.q + item.a.join(''), /\bAI\b/, item.key);
  // 入口：详情页原文出处下、截止页页底，都按包里有没有帮助页显示
  assert.match(wxml('detail'), /<view wx:if="{{has.help}}" class="link source-help" bindtap="help" aria-role="link">数据说明 ›<\/view>/);
  assert.match(wxml('report'), /<view wx:if="{{has.help}}" class="note"><text class="link" bindtap="help" aria-role="link">截止日期怎么看 ›<\/text><\/view>/);
  assert.match(wxml('report'), /<view wx:else class="note">7 天内到期的只列人工核对过的日期/);
  // 详情页、阅读页跳到"正文和附件里的文字是怎么来的"，截止页、结果页跳到"截止日期怎么看"（agy、Codex 第 3 轮）
  for (const [page, topic] of [['detail', 'text'], ['reader', 'text'], ['report', 'deadline'], ['results', 'deadline']]) {
    assert.match(read(`pages/${page}/index.js`), new RegExp(`if \\(this\\.data\\.has\\.help\\) wx\\.navigateTo\\(\\{ url: '/pages/help/index\\?topic=${topic}' \\}\\);`), page);
    assert.match(read(`pages/${page}/index.js`), /require\('\.\.\/\.\.\/utils\/pages\.js'\)/, page);
  }
  assert.match(wxml('reader'), /<view wx:if="{{has.help}}" class="help-link"><text class="link" bindtap="help" aria-role="link">数据说明 ›<\/text><\/view>/);
  assert.match(wxml('results'), /<view wx:if="{{has.help && !advisor}}" class="help-link"><text class="link" bindtap="help" aria-role="link">数据说明 ›<\/text><\/view>/);
  // 帮助页：从"说明 ›"进来滚到那一条并加框
  const vm = require('node:vm');
  let def; const scrolled = [];
  vm.runInNewContext(read('pages/help/index.js'), {
    require: (name) => ({ '../../content/help.js': help, '../../utils/ai-access.js': { current: () => null, subscribe: () => () => {}, ensure() {} }, '../../utils/consent.js': { loginOpen: () => true } })[name],
    Page: (d) => { def = d; }, wx: { setNavigationBarTitle() {}, pageScrollTo: (o) => scrolled.push(o.selector) },
  });
  const page = { ...def, data: { ...def.data }, setData(v) { Object.assign(this.data, v); } };
  page.onLoad({ topic: 'deadline' }); page.onReady();
  assert.deepEqual(scrolled, ['#help-deadline']);
  assert.match(wxml('help'), /id="help-{{item.key}}" class="card help-card {{topic === item.key \? 'help-card--on' : ''}}"/);
  page.onLoad({ topic: 'nope' }); page.onReady(); assert.deepEqual(scrolled, ['#help-deadline'], '没有这一条就不滚');
  page.onLoad({ topic: '<script>' }); assert.equal(page.data.topic, '');
});

// ---- 10/29 晚第三轮的几处逻辑（Codex、独立审阅提的） ----
const vmRun = require('node:vm');
function pageOf(file, deps, globals = {}) {
  let def;
  vmRun.runInNewContext(read(file), {
    module: { exports: {} }, setTimeout, clearTimeout, Promise, Date, Math,
    require(name) {
      if (Object.hasOwn(deps, name)) return deps[name];
      if (name === '../../utils/regions.js') return require('../utils/regions.js');
      if (name === '../../utils/format.js') return require('../utils/format.js');
      if (name === '../../utils/repost.js') return require('../utils/repost.js');
      if (/(^|\/)pages\.js$/.test(name)) return require('./helpers/package-pages-default.js').pages;
      if (/ai-access\.js$/.test(name)) return require('./helpers/ai-off.js');
      throw new Error(`${file} 缺少依赖 ${name}`);
    },
    Page(d) { def = d; }, wx: { navigateTo() {}, setNavigationBarTitle() {}, ...(globals.wx || {}) }, getApp: () => ({ globalData: {} }), getCurrentPages: () => [{}, {}],
  }, { filename: file });
  return { ...def, data: JSON.parse(JSON.stringify(def.data)), setData(values) { Object.assign(this.data, JSON.parse(JSON.stringify(values))); } };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

test('详情页"要点"：别处确实写着的才不重复——发文机关在标题下；适用地区包含在标题旁的地区里；申报状态要截止卡片里有带日期的那条', async () => {
  const today = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
  const plus = (n) => new Date(Date.parse(`${today}T00:00:00Z`) + n * 86400000).toISOString().slice(0, 10);
  const base = { id: '9', kind: '申报通知', tags: ['申报通知'], title: '关于组织申报的通知', issuer: '宁夏回族自治区科学技术厅', date: '2026-10-28', region: '宁夏 · 省级', attachments: [],
    points: [['发文机关', '宁夏回族自治区科学技术厅'], ['文号', '宁科发〔2026〕1号'], ['适用地区', '宁夏'], ['申报状态', '征集中 · 按企业申报截止 11月6日']] };
  let doc = { ...base, deadlines: [{ kind: '企业申报截止', source: 'verified', date: plus(9), time: null, remainingDays: 9, quote: null }] };
  const deps = {
    '../../utils/api.js': { detail: () => Promise.resolve(JSON.parse(JSON.stringify(doc))) },
    '../../utils/subscribe.js': { remindOpen: () => false, requestOnce: () => Promise.resolve(false) }, '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) },
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/attachments.js': { decorate: () => [] },
  };
  const page = pageOf('pages/detail/index.js', deps);
  page.onLoad({ id: '9' }); await settle();
  assert.deepEqual(page.data.points, [['文号', '宁科发〔2026〕1号']]);
  // 截止卡片里没有带日期的：申报状态照旧列；地区标签里没写到这个地名：适用地区照旧列
  doc = { ...base, region: '国家部委', points: base.points.map((p) => (p[0] === '适用地区' ? ['适用地区', '全国'] : p)),
    deadlines: [{ kind: '企业申报截止', source: 'unverified', date: null, time: null, remainingDays: null, quote: null }] };
  page.refreshing = false; page.load(); await settle();
  assert.deepEqual(page.data.points.map((p) => p[0]), ['文号', '适用地区', '申报状态']);
  // 过了零点重算：截止卡片里带日期的那条没了（机器摘录进了 7 天内），申报状态跟着回来（Codex 第 4 轮）
  doc = { ...base, deadlines: [{ kind: '企业申报截止', source: 'excerpt', date: plus(9), time: null, remainingDays: 9, quote: '请于某日前报送。' }] };
  page.refreshing = false; page.load(); await settle();
  assert.deepEqual(page.data.points.map((p) => p[0]), ['文号']);
  page.data.doc = { ...page.data.doc, deadlines: [{ kind: '企业申报截止', source: 'excerpt', date: plus(5), time: null, remainingDays: 5, quote: '请于某日前报送。' }] };
  page.recompute();
  assert.deepEqual(page.data.points.map((p) => p[0]), ['文号', '申报状态']);
  // 一项都不剩时整张卡不显示
  doc = { ...base, points: [['发文机关', '宁夏回族自治区科学技术厅'], ['适用地区', '宁夏']], deadlines: [] };
  page.refreshing = false; page.load(); await settle();
  assert.deepEqual(page.data.points, []);
  assert.match(wxml('detail'), /<view wx:if="{{points.length}}" class="card">\s*<view class="section-title">要点<\/view>/);
});

test('截止页：选了整个还没收录的省写明"还没收录"；换到别的省马上清掉，收录范围没取到不说没收录', async () => {
  let coverage = { notCovered: [{ name: '广西', reason: '网站不允许程序访问' }] };
  let release;
  const deps = {
    '../../utils/api.js': { deadlineReport: () => Promise.resolve({ items: [], total: 0, summary: {}, by_province: {}, verified_total: 0 }), calendar: () => Promise.resolve({ items: [], past: [] }) },
    '../../utils/exporter.js': {}, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/coverage.js': { loadCoverage: () => new Promise((resolve) => { release = () => resolve(coverage); }) },
  };
  const page = pageOf('pages/report/index.js', deps);
  page.onLoad({}); await settle();
  const pick = (name) => page.pickProvince({ detail: { value: page.data.provinces.indexOf(name) } });
  pick('广西'); release(); await settle();
  assert.equal(page.data.uncovered, '广西');
  pick('山东');
  assert.equal(page.data.uncovered, '', '换省时马上清掉，不等收录范围回来');
  release(); await settle(); assert.equal(page.data.uncovered, '');
  coverage = null; pick('广西'); release(); await settle();
  assert.equal(page.data.uncovered, '', '收录范围没取到：不说没收录');
  assert.match(wxml('report'), /<view wx:if="{{uncovered}}" class="empty-sub">{{uncovered}}还没收录，暂时查不到这个地区的通知。<\/view>/);
});

test('结果页（站内看所选文件）：截止只写日期或"截止时间见原文"，原文摘录在末尾标一次；不显示导出说明、未核对一行和网址', async () => {
  const items = [
    { number: 1, doc_id: '1', title: '甲', deadline: '2026-11-06（机器摘录）', date_role: '企业申报截止（机器摘录）', deadline_quote: '请于2026年11月6日前报送。', sites: '某厅网站', urls: 'https://a.example.gov.cn/1', unchecked: '截止日期（以原文为准）、支持方式' },
    { number: 2, doc_id: '2', title: '乙', deadline: '截止未核对，以原文为准', date_role: '截止未核对', sites: '某厅网站', urls: 'https://a.example.gov.cn/2', unchecked: '截止日期（以原文为准）' },
    { number: 3, doc_id: '3', title: '丙', deadline: '见原文', date_role: '截止时间见原文', sites: '某厅网站', urls: 'https://a.example.gov.cn/3', unchecked: '无' },
    // 核对过的照后台的写法：类型在前、日期在后，几条用"；"连（api/exports.py _deadline_text）
    { number: 4, doc_id: '4', title: '丁', deadline: '企业申报截止 2026-11-06；推荐截止 2026-11-20', date_role: '企业申报截止；推荐截止', sites: '某厅网站', urls: 'https://a.example.gov.cn/4', unchecked: '无' },
  ];
  const page = pageOf('pages/results/index.js', {
    '../../utils/api.js': { exportPreview: () => Promise.resolve({ kind: 'catalogue', title: '本次所选文件目录', revision: 'r1', notice: '本文件由第三方整理……“未核项”列出这一行还没有人工核对的字段', generatedAt: '2026-10-29', total: 4, items }) },
    '../../utils/exporter.js': { exportDocs() {} },
  });
  page.onLoad({ ids: '1,2,3,4' }); await page.onShow();
  // 自带类型的照写，不再接一遍"日期角色"（原来写成"企业申报截止 2026-11-06 企业申报截止"，Codex 第 9 轮）
  assert.deepEqual(page.data.result.items.map((i) => i.deadlineText), ['企业申报截止 2026-11-06（原文摘录）', '截止时间见原文', '截止时间见原文', '企业申报截止 2026-11-06；推荐截止 2026-11-20']);
  const { render, textOf } = require('./wxml-render.js');
  const shown = textOf(render('pages/results/index.wxml', page.data));
  assert.doesNotMatch(shown, /未核项|未核对：|以原文为准|example\.gov\.cn|机器摘录/);
  assert.match(shown, /原句：请于2026年11月6日前报送。/);
  assert.match(shown, /这里展示文件目录，不是企业入选结论或信用报告。/);
});

test('查名单结果：整行可点进发布这份名单的通知，只另放"看原名单里的这一行"', () => {
  const lists = wxml('lists');
  assert.doesNotMatch(lists, /看发布这份名单的通知|bindtap="evidence"/);
  assert.match(lists, /<view wx:if="{{item.matched.entryToken}}" class="hit-actions">\s*<button class="hit-btn hit-btn--main" data-token="{{item.matched.entryToken}}" bindtap="record">看原名单里的这一行<\/button>/);
  assert.doesNotMatch(read('pages/lists/index.js'), /evidence\(e\)/);
});

// ---- 10/29 独立审阅第 3 轮（按本机真实数据渲染看到的） ----
const today8 = () => new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
const plusDays = (n) => new Date(Date.parse(`${today8()}T00:00:00Z`) + n * 86400000).toISOString().slice(0, 10);
function detailOf(getDoc) {
  return pageOf('pages/detail/index.js', {
    '../../utils/api.js': { detail: () => Promise.resolve(JSON.parse(JSON.stringify(getDoc()))) },
    '../../utils/subscribe.js': { remindOpen: () => false, requestOnce: () => Promise.resolve(false) }, '../../utils/reminders.js': { remindedDocs: () => Promise.resolve({}) },
    '../../utils/consent.js': { loginOpen: () => true }, '../../utils/exporter.js': {}, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/attachments.js': { decorate: (list) => (list || []).map((a) => ({ ...a, canOpen: !!a.readable, meta: '', action: '阅读' })) },
  });
}

test('详情页：核对过的截止原句只在截止卡片里写一次；"原文摘录"只标机器摘出的截止，申报对象标"已核对原文"', async () => {
  const { render, textOf, findAll, hasClass } = require('./wxml-render.js');
  const base = { id: '9', kind: '申报通知', tags: ['申报通知'], title: '关于组织申报的通知', issuer: '某厅', date: '2026-10-28', region: '山东 · 省级', attachments: [], points: [],
    excerpt: { section: '截止时间原文', text: '请于11月20日前报送。' }, checked: '2026-10-27',
    fields: { audience: { text: '注册在本省的企业' } } };
  let doc = { ...base, deadlines: [{ kind: '企业申报截止', source: 'verified', date: plusDays(20), time: null, remainingDays: 20, quote: '请于11月20日前报送。' }] };
  const page = detailOf(() => doc);
  page.onLoad({ id: '9' }); await settle();
  let shown = textOf(render('pages/detail/index.wxml', { ...page.data, loading: false }));
  assert.equal(shown.split('请于11月20日前报送。').length - 1, 1, '逐类写截止时原句只写一次');
  assert.doesNotMatch(shown, /原文摘录/);
  assert.match(shown, /申报对象（已核对原文）/);
  assert.match(shown, /截止日期已于 2026年10月27日 对照原文核对。/);
  // 旧接口（没有 deadlines、只有核对过的 deadline）：原句跟在日期下面，标"已人工核对"，也只写一次
  doc = { ...base, kind: '政策文件', tags: ['政策文件'], deadline: { label: '截止', date: plusDays(20), time: null, verified: true, notes: [] } };
  delete doc.deadlines;
  page.refreshing = false; page.load(); await settle();
  const root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  shown = textOf(root);
  assert.equal(shown.split('请于11月20日前报送。').length - 1, 1);
  assert.match(textOf(findAll(root, hasClass('due-card'))[0]), /已人工核对\s*原文：请于11月20日前报送。/);
  // 别的节的摘录（演示数据里是"二、申报条件"）不当截止原句写在日期下面（Codex 第 6 轮）
  doc = { ...doc, excerpt: { section: '二、申报条件', text: '（一）申报单位原则上应为制造业企业。' } };
  page.refreshing = false; page.load(); await settle();
  assert.equal(page.data.dueQuote, '');
  assert.doesNotMatch(textOf(render('pages/detail/index.wxml', { ...page.data, loading: false })), /申报单位原则上应为制造业企业/);
});

test('详情页：没核对、候选在 7 天内的那一类写"可能 7 天内截止"；原文链接已失效的不写"截止时间见原文"、正文卡片不再写一遍', async () => {
  const { render, textOf, findAll, hasClass } = require('./wxml-render.js');
  const base = { id: '9', kind: '申报通知', tags: ['申报通知'], title: '关于组织申报的通知', issuer: '某厅', date: '2026-10-28', region: '河南 · 省级', attachments: [], points: [] };
  let doc = { ...base, deadlines: [
    { kind: '企业申报截止', source: 'unverified', date: null, time: null, remainingDays: null, quote: null, soon: true },
    { kind: '推荐或审核截止', source: 'excerpt', date: plusDays(9), time: '18:00', remainingDays: 9, quote: '审核提交时间截止到某日18:00' },
    { kind: '材料报送截止', source: 'unverified', date: null, time: null, remainingDays: null, quote: null },
  ] };
  const page = detailOf(() => doc);
  page.onLoad({ id: '9' }); await settle();
  let rows = findAll(render('pages/detail/index.wxml', { ...page.data, loading: false }), hasClass('dl')).map((n) => textOf(n).replace(/\s+/g, ''));
  assert.match(rows[0], /^企业申报截止可能7天内截止$/);
  assert.match(rows[1], /^推荐或审核截止.*原文摘录/);
  assert.match(rows[2], /^材料报送截止截止时间见原文$/);
  // 原文链接已失效：没日期的那几类不写"截止时间见原文"（"可能 7 天内截止"照写）；没有一条能写的就不放截止卡片
  doc = { ...doc, link_gone: '2026-10-27', body: { status: 'none', reason: 'link_gone' } };
  page.refreshing = false; page.load(); await settle();
  let root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.deepEqual(page.data.dls.map((r) => r.kind), ['企业申报截止', '推荐或审核截止']);
  assert.doesNotMatch(textOf(root), /截止时间见原文/);
  doc = { ...base, deadlines: [], deadline_state: 'none', link_gone: '2026-10-27', body: { status: 'none', reason: 'link_gone' } };
  page.refreshing = false; page.load(); await settle();
  root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.equal(page.data.dueNote, '');
  assert.equal(findAll(root, hasClass('due-card')).length, 0);
  // "正文和附件不再提供"标题下写了一次，正文卡片不再写"原网页已打不开，正文暂不提供"
  assert.equal(findAll(root, (n) => n.attrs && n.attrs.id === 'body').length, 0);
  assert.equal(textOf(root).split('不再提供').length - 1, 1);
  assert.doesNotMatch(textOf(root), /原网页已打不开/);
  // 附件一个都读不了：附件卡片也不再逐个写"附件暂不提供"（真实数据 70171，10/29 独立审阅）
  doc = { ...doc, attachments: [{ id: '4', name: '申报书.docx', ext: 'docx', readable: false }, { id: '5', name: '汇总表.xlsx', ext: 'xlsx', readable: false }] };
  page.refreshing = false; page.load(); await settle();
  root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.equal(page.data.goneAll, true);
  assert.equal(findAll(root, (n) => n.attrs && n.attrs.id === 'attachments').length, 0);
  assert.equal(textOf(root).split('不再提供').length - 1, 1);
  // 名单类正文：说一句"正文里有名单"，怎么查只在下面的入口写一次（10/29 独立审阅）
  const format = require('../utils/format.js');
  assert.equal(format.noBodyText({ status: 'list' }), '这份通知的正文里有名单');
  // 服务端万一写的是"内容在附件里"：附件卡不显示，正文卡片也不能这么说（Codex 第 8 轮）
  doc = { ...doc, body: { status: 'none', reason: 'attachments' } };
  page.refreshing = false; page.load(); await settle();
  root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.equal(findAll(root, (n) => n.attrs && n.attrs.id === 'body').length, 0);
  assert.doesNotMatch(textOf(root), /内容在附件里/);
  doc = { ...doc, body: { status: 'none', reason: 'link_gone' } };
  // 链接失效、附件却还能读：标题下不说"不再提供"，正文卡片照旧写正文为什么没有（Codex 第 6 轮）
  doc = { ...doc, attachments: [{ id: '3', name: '申报指南.pdf', ext: 'pdf', readable: true }] };
  page.refreshing = false; page.load(); await settle();
  root = render('pages/detail/index.wxml', { ...page.data, loading: false });
  assert.equal(page.data.goneAll, false);
  assert.doesNotMatch(textOf(root), /不再提供/);
  assert.equal(findAll(root, (n) => n.attrs && n.attrs.id === 'body').length, 1);
  assert.match(textOf(root), /原网页已打不开，正文暂不提供/);
});

test('详情页：正文卡顶上不再写"正文约 N 字 · 附件 N 个"（字数下一行写着、附件下面逐个列着）', async () => {
  const { render, textOf } = require('./wxml-render.js');
  const doc = { id: '9', kind: '申报通知', tags: ['申报通知'], title: '关于组织申报的通知', issuer: '某厅', date: '2026-10-28', region: '云南 · 省级', points: [], deadlines: [],
    body: { status: 'ok', text: '一、申报对象。'.repeat(200), total: 1400, truncated: false },
    attachments: [{ id: '1', name: '申报指南.pdf', ext: 'pdf', readable: true }, { id: '2', name: '申报书.docx', ext: 'docx', readable: true }] };
  const page = detailOf(() => doc);
  page.onLoad({ id: '9' }); await settle();
  const shown = textOf(render('pages/detail/index.wxml', { ...page.data, loading: false }));
  assert.equal(page.data.bodySummary, undefined);
  assert.doesNotMatch(shown, /正文约|都能读/);
  assert.equal(shown.split(/全文约 \d+ 字/).length - 1, 1);
});

test('列表行：原文链接已失效的不写"截止时间见原文"；另一类可能 7 天内截止时先说这个，不写更晚或已过的日期；整行和版本行按下有反馈', () => {
  const vm = require('node:vm');
  const load = (doc) => {
    let def;
    vm.runInNewContext(read('components/doc-row/index.js'), {
      module: { exports: {} }, Date, Math,
      require(name) {
        if (name === '../../utils/format.js') return require('../utils/format.js');
        if (name === '../../utils/repost.js') return require('../utils/repost.js');
        throw new Error(name);
      },
      Component(d) { def = d; }, wx: { navigateTo() {} },
    });
    const c = { ...def, data: { ...(def.data || {}) }, properties: {}, setData(v) { Object.assign(this.data, v); } };
    c.observers['doc, q, today'].call(c, doc, '');
    return c.data;
  };
  const base = { id: '1', kind: '申报通知', title: '关于组织申报的通知', issuer: '某厅', date: '2026-10-28' };
  assert.equal(load({ ...base, due: null, link_gone: '2026-10-27' }).dueNote, '');
  assert.equal(load({ ...base, due: null }).dueNote, '截止时间见原文');
  assert.equal(load({ ...base, due: null, dueSoonUnverified: true, link_gone: '2026-10-27' }).dueNote, '可能 7 天内截止');
  // 推荐截止 9 天后（原文摘录）、企业申报截止可能 7 天内：先说"可能 7 天内截止"，不写 9 天后那个日期和原句
  const later = load({ ...base, due: { kind: '推荐或审核截止', source: 'excerpt', date: plusDays(9), time: null, remainingDays: 9, quote: '某日前' }, dueSoonUnverified: true });
  assert.equal(later.due, null); assert.equal(later.excerpt, null); assert.equal(later.dueNote, '可能 7 天内截止');
  const past = load({ ...base, due: { kind: '推荐或审核截止', source: 'verified', date: plusDays(-2), time: null, remainingDays: -2, quote: '' }, dueSoonUnverified: true });
  assert.equal(past.due, null); assert.equal(past.dueNote, '可能 7 天内截止');
  // 核对过的日期本身就在 7 天内：照写这个日期
  const near = load({ ...base, due: { kind: '企业申报截止', source: 'verified', date: plusDays(3), time: null, remainingDays: 3, quote: '' }, dueSoonUnverified: true });
  assert.equal(near.due.days, 3); assert.equal(near.dueNote, '');
  const tpl = read('components/doc-row/index.wxml');
  assert.match(tpl, /<view class="row [^"]*" hover-class="row--pressed"[^>]*bindtap="open"/);
  assert.match(tpl, /class="version" hover-class="version--pressed" hover-stop-propagation[^>]*catchtap="openVersion"[\s\S]*?<t-icon name="chevron-right"/);
  assert.match(tpl, /class="group" catchtap="toggleGroup" hover-stop-propagation/);
  assert.match(read('components/ai-button/index.wxml'), /class="ai-chip" hover-stop-propagation catchtap="open"/);
});

test('我的记录：AI 解读、条款对照的卡片画"›"、按下变淡（整张点开）；批量工具的卡片不画（靠"站内查看结果"按钮）', () => {
  const { render, findAll } = require('./wxml-render.js');
  const advisor = require('../utils/advisor.js');
  const items = [
    advisor.taskView({ id: 1, kind: 'interpret', status: 'done', createdAt: '2026-10-29T01:00:00Z' }),
    advisor.taskView({ id: 2, kind: 'compare', status: 'done', createdAt: '2026-10-29T01:00:00Z', hasFile: true }),
  ];
  assert.deepEqual(items.map((i) => i.opens), [true, false]);
  const root = render('pages/advisor-tasks/index.wxml', { loading: false, toolsEnabled: true, items });
  const cards = findAll(root, (n) => n.attrs && n.attrs.bindtap === 'openTask');
  assert.deepEqual(cards.map((c) => c.attrs['hover-class']), ['task-card--pressed', 'none']);
  assert.deepEqual(cards.map((c) => findAll(c, (n) => n.tag === 't-icon').length), [1, 0]);
  // 卡上的按钮按下时整张卡不变淡
  for (const name of ['readResult', 'again', 'cancel', 'remove']) assert.match(wxml('advisor-tasks'), new RegExp(`hover-stop-propagation catchtap="${name}"`));
});

test('结果页（站内看所选文件）：空的发布状态不留两个"·"；支持方式没核对时不写那一行；出处只写网站名称，去掉括号里的网址', async () => {
  const items = [
    { number: 1, doc_id: '904', title: '甲', kind: '申报通知', status: '', region: '河南 · 省级', support: '见原文', sites: '河南省科学技术厅（kjt.henan.gov.cn）', deadline: '见原文', date_role: '截止时间见原文' },
    { number: 2, doc_id: '905', title: '乙', kind: '申报通知', status: '征集中', region: '山东 · 省级', support: '资金补助、奖励', sites: '甲厅（a.gov.cn）；乙局（https://b.gov.cn/）', deadline: '见原文', date_role: '截止时间见原文' },
    { number: 3, doc_id: '906', title: '丙', kind: '政策文件', status: '', region: '国家部委', support: '—', sites: '某部（筹）（c.gov.cn）', deadline: '见原文', date_role: '截止时间见原文' },
  ];
  const page = pageOf('pages/results/index.js', {
    '../../utils/api.js': { exportPreview: () => Promise.resolve({ kind: 'catalogue', title: '本次所选文件目录', revision: 'r1', notice: '', generatedAt: '2026-10-29', total: 3, items }) },
    '../../utils/exporter.js': { exportDocs() {} },
  });
  page.onLoad({ ids: '904,905,906' }); await page.onShow();
  const got = page.data.result.items;
  assert.deepEqual(got.map((i) => i.metaText), ['申报通知 · 河南 · 省级', '申报通知 · 征集中 · 山东 · 省级', '政策文件 · 国家部委']);
  assert.deepEqual(got.map((i) => i.supportText), ['', '资金补助、奖励', '']);
  assert.deepEqual(got.map((i) => i.siteText), ['河南省科学技术厅', '甲厅；乙局', '某部（筹）']);
  const { render, textOf } = require('./wxml-render.js');
  const shown = textOf(render('pages/results/index.wxml', page.data));
  assert.doesNotMatch(shown, / · {2}· |gov\.cn| 见原文 /);
  assert.match(shown, /支持方式：资金补助、奖励/);
  // 没核对的那一类可能 7 天内到期（due_soon，和列表、详情一致）：没日期写"可能 7 天内截止"；写着的日期都不在 7 天内时补一句
  const soonItems = [
    { number: 1, doc_id: '7', title: '戊', kind: '申报通知', status: '', region: '云南 · 省级', deadline: '截止未核对，以原文为准', date_role: '截止未核对', due_soon: true },
    { number: 2, doc_id: '8', title: '己', kind: '申报通知', status: '', region: '河南 · 省级', deadline: `${plusDays(20)}（机器摘录）`, date_role: '材料报送截止（机器摘录）', deadline_quote: '某日前', due_soon: true },
    { number: 3, doc_id: '9', title: '庚', kind: '申报通知', status: '', region: '河南 · 省级', deadline: `企业申报截止 ${plusDays(3)}`, date_role: '企业申报截止', due_soon: true },
    { number: 4, doc_id: '10', title: '辛', kind: '申报通知', status: '', region: '河南 · 省级', deadline: '截止未核对，以原文为准', date_role: '截止未核对', due_soon: false },
    { number: 5, doc_id: '11', title: '壬', kind: '申报通知', status: '', region: '宁夏 · 省级', deadline: '见原文', date_role: '截止时间见原文', sites: '某厅（a.gov.cn）', due_soon: false, link_gone: true },
  ];
  const soonPage = pageOf('pages/results/index.js', {
    '../../utils/api.js': { exportPreview: () => Promise.resolve({ kind: 'catalogue', title: '本次所选文件目录', revision: 'r2', notice: '', generatedAt: '2026-10-29', total: 4, items: soonItems }) },
    '../../utils/exporter.js': { exportDocs() {} },
  });
  soonPage.onLoad({ ids: '7,8,9,10,11' }); await soonPage.onShow();
  assert.deepEqual(soonPage.data.result.items.map((i) => i.deadlineText), [
    '可能 7 天内截止', `材料报送截止 ${plusDays(20)}（原文摘录）；另有一类可能 7 天内截止`, `企业申报截止 ${plusDays(3)}`, '截止时间见原文', '']);
  // 原文链接已失效：不写"截止："那一行，出处后面标一句（和列表、详情一致）
  const goneText = textOf(render('pages/results/index.wxml', soonPage.data));
  assert.match(goneText, /出处：某厅 · 原文链接已失效/);
  assert.doesNotMatch(goneText.split('壬')[1], /截止：|站内阅读正文和依据/);
  assert.match(goneText.split('壬')[0], /站内阅读正文和依据/);
  // 只看还有效的日期（Codex 第 8 轮）："原定……已延期"那条不算；今天带时刻、已经过了点的不算；只有变更的照写，不写"见原文"
  const now = new Date();
  const pastHour = (() => {
    const hour = new Date(now.getTime() + 8 * 3600 * 1000).getUTCHours();
    return hour >= 1 ? `${String(hour - 1).padStart(2, '0')}:00` : null;
  })();
  const parse = pageOf('pages/results/index.js', {
    '../../utils/api.js': { exportPreview: () => Promise.resolve({ kind: 'catalogue', title: '本次所选文件目录', revision: 'r3', notice: '', generatedAt: '2026-10-29', total: 3, items: [
      { number: 1, doc_id: '21', title: '甲', kind: '申报通知', status: '', region: '河南 · 省级', deadline: `企业申报截止 ${plusDays(20)}；原定 推荐截止 ${plusDays(3)} 已延期，见新通知《乙》`, date_role: '企业申报截止；推荐截止（已延期）', due_soon: true },
      { number: 2, doc_id: '22', title: '丙', kind: '申报通知', status: '', region: '河南 · 省级', deadline: `原定 企业申报截止 ${plusDays(3)} 已撤销，见新通知《丁》`, date_role: '企业申报截止（已撤销）', due_soon: false },
      ...(pastHour ? [{ number: 3, doc_id: '23', title: '戊', kind: '申报通知', status: '', region: '河南 · 省级', deadline: `企业申报截止 ${plusDays(0)} ${pastHour}`, date_role: '企业申报截止', due_soon: true }] : []),
    ] }) },
    '../../utils/exporter.js': { exportDocs() {} },
  });
  parse.onLoad({ ids: '21,22,23' }); await parse.onShow();
  const texts = parse.data.result.items.map((i) => i.deadlineText);
  assert.equal(texts[0], `企业申报截止 ${plusDays(20)}；原定 推荐截止 ${plusDays(3)} 已延期，见新通知《乙》；另有一类可能 7 天内截止`);
  assert.equal(texts[1], `原定 企业申报截止 ${plusDays(3)} 已撤销，见新通知《丁》`);
  if (pastHour) assert.ok(texts[2].endsWith('；另有一类可能 7 天内截止'), texts[2]);
});

test('AI 首页：暂时不可用、连不上时只在上面那张卡里放"联系客服"；鸿蒙手机上客服按钮不再给第二个"使用帮助"', () => {
  const ai = read('pages/ai/index.wxml');
  assert.match(ai, /<view wx:if="{{gate !== 'offline' && gate !== 'paused'}}" class="help-contact"><contact-button from="ai_home" \/><\/view>/);
  assert.match(read('components/contact-button/index.js'), /const NO_HELP_LINK = \['me', 'ai_home', 'help'\];/);
});
