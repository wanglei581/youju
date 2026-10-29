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
    detail: ['<view wx:else class="due-unverified">截止时间见原文</view>', '以上要点由有据按原文逐条整理，{{doc.checked}} 核对。', '有据不判断能否申报',
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
      '检查时已打不开。', '仅供检索参考', 'attNote',
      // 10/29 晚
      '（自动提取', '非官方分类', '机器摘录', '截止未核对', 'body-more', '发文机关：{{doc.issuer}}', 'doc.points'],
    me: ['有据是第三方整理的信息工具', '不绕过', '每条都有出处', '每条都注明出处', '地市目前只收录部分城市', '区县目前只收录少数地方'],
    home: ['有据是第三方整理的信息工具', '未重复计算', '每条都有出处', '点开看是哪些', '日期还没核对', 'soon-stale', '覆盖国家部委和'],
    ai: ['每条都有出处', '点标题看通知全文', '我的 AI 记录'],
    help: ['每条都有出处'],
    files: ['数据说明（来源网站', '导出不是必须的', '为节省空间'],
    follow: ['就能把当前的关键词和筛选条件存下来'],
    report: ['最终以原文为准', '未收录的城市、区县会明确提示', '直辖市不选区县时也是', '机器摘录仅供参考', '请以原文为准', '只统计已收录网站的文件', '城市一栏选', '人工核对中',
      '日期还没核对', '截止未核对', '核对过的截止　空心点：机器摘录'],
    search: ['选择器无法使用时请升级微信', '同一通知的各地转发版本合为一条', '直辖市不选区县时也是', '不重复计数', '城市一栏选', '直辖市例外', 'tagNote', "' ' + total"],
    clients: ['需要批量处理或生成 Excel', '选择器无法使用时请升级微信', '选择具体区县会替换所属城市和全省的条件', '一条一行列出来', '直辖市不选区县时也是',
      '点一下移除', '省市区选项来自微信', '城市一栏选', '非官方分类'],
    results: ['来源核对：', '变更核对：', '{{item.urls}}', '未核对：', 'item.date_role'],
    reader: ['反馈内容问题', 'page-label'],
    'advisor-tasks': ['查看解读', '查看对照'],
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
    { number: 4, doc_id: '4', title: '丁', deadline: '2026-11-06；2026-11-20', date_role: '企业申报截止；推荐截止', sites: '某厅网站', urls: 'https://a.example.gov.cn/4', unchecked: '无' },
  ];
  const page = pageOf('pages/results/index.js', {
    '../../utils/api.js': { exportPreview: () => Promise.resolve({ kind: 'catalogue', title: '本次所选文件目录', revision: 'r1', notice: '本文件由第三方整理……“未核项”列出这一行还没有人工核对的字段', generatedAt: '2026-10-29', total: 4, items }) },
    '../../utils/exporter.js': { exportDocs() {} },
  });
  page.onLoad({ ids: '1,2,3,4' }); await page.onShow();
  assert.deepEqual(page.data.result.items.map((i) => i.deadlineText), ['2026-11-06 企业申报截止（原文摘录）', '截止时间见原文', '截止时间见原文', '2026-11-06；2026-11-20 企业申报截止；推荐截止']);
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
