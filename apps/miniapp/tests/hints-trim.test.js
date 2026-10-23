// 10/17 创始人："把小程序上面没有用的提示去掉，只保留有用的提示信息，比如第三方整理、非政府平台这样的"。
// 规则：出处说明（第三方整理 · 非政府平台）由顶部 notice-bar 统一写一次，页面里不再重复；
// 和具体数据挂钩的准确性说明、隐私说明、AI 投诉渠道照旧保留，删提示时不能把它们一起删掉。
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
  assert.match(read('components/notice-bar/index.wxml'), /第三方整理 · 非政府平台 · 每条附原文链接/);
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
  const keep = {
    detail: ['<text class="due-label">截止时间</text><text class="weak">以原文为准</text>', '截止未核对，请以原文为准', '以上要点由有据按原文逐条整理，以原文为准', '各版本的截止日期、报送要求可能不同，以各自原文为准', '条件可能未列全，以原文为准',
      // 10/22 走查："自动提取，未经人工逐字核对"写进正文标题；"截止日期可先看上面的'截止时间'"只在上面有截止卡片时出现
      '条件以原文为准，不作为能否申报的判断', '（自动提取，未经人工逐字核对）', '；截止日期可先看上面的“截止时间”', '，一切以原文为准。',
      '按标题归类，仅供检索参考，非官方分类', '申报请在主管部门的申报系统办理，有据不提供填报入口。'],
    // 10/22 走查：和搜索页同一说法——区县栏"全部"只查这个市的市级来源；直辖市区县栏"全部"是整个直辖市（各级来源都在内）
    report: ['机器摘录仅供参考，以原文为准', '申报请在主管部门的系统办理，以原文为准。', '截止未核对，请以原文为准', '省市区选项来自微信，不代表已收录', '只查这个市已收录的市级来源，不含区县',
      '7 天内到期的只写人工核对过的日期', '不代表当地没有通知', '城市一栏选“全部”查全省各级来源', '直辖市例外：区县一栏选“全部”时查整个直辖市（各级来源都在内）'],
    // 10/22 走查：地区规则拆成三句放在选择器上面；直辖市只选省份时查整个直辖市（各级来源都在内，后端按省份筛、不限层级）；
    // "只看未截止"的口径在面板、结果上面、空状态三处是同一句（search/index.js 的 OPEN_GROUP_NOTE）
    search: ['已合并 {{reposts}} 个各地转发版本，不重复计数', '直辖市例外：区县一栏选“全部”时查整个直辖市（各级来源都在内）', '只查这个市已收录的市级来源，不含区县', '“只看未截止”：{{openNote}}'],
    lists: ['没找到不代表没上过名单', '请核对统一社会信用代码'],
    clients: ['已确认截止的不列', '这里的客户卡片不会自动发送', '要另外确认上传', '选省包含该省已收录的城市、区县', '城市一栏选“全部”查全省各级来源', '直辖市例外：区县一栏选“全部”时查整个直辖市（各级来源都在内）', '只查这个市已收录的市级来源，不含区县', '不判断客户能不能申报', '省市区选项来自微信，不代表已收录', '客户名称只存在本机，导出的表里不含客户名称。',
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
});

test('删掉的重复或没有用的提示不再出现', () => {
  const gone = {
    detail: ['按各版本原文分别核对', '仅供阅读参考', '推荐截止', '有据是第三方整理的信息工具', '非官方分类，以原文为准'],
    me: ['有据是第三方整理的信息工具', '不绕过', '每条都有出处'],
    home: ['有据是第三方整理的信息工具'],
    ai: ['每条都有出处'],
    help: ['每条都有出处'],
    files: ['数据说明（来源网站'],
    report: ['最终以原文为准', '未收录的城市、区县会明确提示', '直辖市不选区县时也是'],
    search: ['选择器无法使用时请升级微信', '同一通知的各地转发版本合为一条', '直辖市不选区县时也是'],
    clients: ['需要批量处理或生成 Excel', '选择器无法使用时请升级微信', '选择具体区县会替换所属城市和全省的条件', '一条一行列出来', '直辖市不选区县时也是'],
  };
  for (const [page, texts] of Object.entries(gone)) {
    const template = wxml(page);
    for (const text of texts) assert.ok(!template.includes(text), `${page} 还有：${text}`);
  }
});
