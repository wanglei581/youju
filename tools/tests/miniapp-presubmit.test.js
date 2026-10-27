const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const presubmit = require('../miniapp_presubmit_check');
const { SERVER_TOOL_TERMS_VERSION, agreementFingerprint } = presubmit;
// 下面多数用例只测别的检查项：协议指纹默认按这份样本自己的算（指纹这一项另有用例，D-098）
const inspect = (args) => presubmit.inspect({ fingerprints: { [args.legal && args.legal.version]: args.legal ? agreementFingerprint(args.legal) : '' }, ...args });
const releaseIdentity = require('../release/release-identity.json');
function input() { return {
  // 首发（过审版）协议：不含 AI 条款，版本排在后台 AI 条款版本之前（D-093 ⑤）
  legal:{final:true, version:'2026-10.2', user:{sections:[{h:'一、服务内容与定位',p:['公开信息的检索和查看免费。']}]}, privacy:{sections:[]}, operator:{icp:'测试备案号',email:'contact@example.test'}},
  config:{cloudEnv:releaseIdentity.cloudEnv,cloudService:releaseIdentity.cloudService,devBaseUrl:'',templates:{newDocs:'',deadline:''}},
  project:{appid:releaseIdentity.appid},
  app:{pages:['home','follow','files','me','search','detail','report','lists','agreement','feedback','follow-new','my-data','about','clients','reader','results'].map(p=>`pages/${p}/index`),tabBar:{list:[['home','首页'],['follow','关注'],['files','导出'],['me','我的']].map(([p,text])=>({pagePath:`pages/${p}/index`,text}))}}
}; }
function identityText(result) {
  return JSON.stringify(result);
}
test('所有版本必须包含站内阅读和结果页，等数量替换或重复也不能绕过范围检查',()=>{
 for(const profile of ['first-release','ai-internal','ai-release','complete-candidate','complete-release']) {
  const x=input();
  if(profile!=='first-release') x.app.pages.push(...['advisor','advisor-interpret','advisor-clause','advisor-tasks','complaint'].map(p=>`pages/${p}/index`));
  if(profile.startsWith('complete-')) x.app.pages.push(...['advisor-form','advisor-terms','advisor-trial','pass','orders','ai','help'].map(p=>`pages/${p}/index`));
  const state=y=>inspect({...y,profile,mode:'prepare'}).checks.find(c=>c.id==='pages').state;
  assert.equal(state(x),'pass',profile);
  for(const page of ['reader','results']) {
   for(const replacement of [null,'pages/unreviewed/index','pages/home/index']) {
    const y=JSON.parse(JSON.stringify(x));
    const i=y.app.pages.indexOf(`pages/${page}/index`);
    y.app.pages.splice(i,1,...(replacement?[replacement]:[]));
    assert.equal(state(y),'fail',`${profile}: ${page}: ${replacement}`);
   }
  }
 }
});
test('真实注册页面通过完整候选范围校验，不能只验证测试fixture',()=>{
 const app=JSON.parse(fs.readFileSync(path.join(__dirname,'../../apps/miniapp/app.json'),'utf8'));
 const r=inspect({...input(),app,profile:'complete-release',mode:'prepare'});
 assert.equal(r.checks.find(c=>c.id==='pages').state,'pass');
 assert.equal(r.readyToSubmit,false);
});
test('底栏名字（D-087）：四格叫首页、关注、导出、我的（完整版最后另有 AI）；第三格还叫"文件"、少了名字或换了顺序都不过；仓库的 app.json 也过',()=>{
 const { TAB_NAMES } = require('../miniapp_presubmit_check');
 assert.deepEqual(TAB_NAMES,['首页','关注','导出','我的']);
 const tabs=(x,profile='first-release')=>inspect({...x,profile,mode:'prepare'}).checks.find(c=>c.id==='tabs');
 assert.equal(tabs(input()).state,'pass');
 assert.equal(tabs(input()).name,'底栏为首页、关注、导出、我的');
 for(const change of [y=>{y.app.tabBar.list[2].text='文件';},y=>{delete y.app.tabBar.list[1].text;},y=>{y.app.tabBar.list[0].text='我的';y.app.tabBar.list[3].text='首页';}]) {
  const x=input();change(x);
  assert.equal(tabs(x).state,'fail');
  assert.equal(inspect({...x,mode:'prepare'}).ok,false);
 }
 // 仓库里真实的 app.json 是完整版的五格（最后一格 AI）
 const app=JSON.parse(fs.readFileSync(path.join(__dirname,'../../apps/miniapp/app.json'),'utf8'));
 const full=tabs({...input(),app},'complete-release');
 assert.equal(full.state,'pass');
 assert.equal(full.name,'底栏为首页、关注、导出、我的，另有 AI 一格（AI 开着时才显示）');
 const renamed=JSON.parse(JSON.stringify(app)); renamed.tabBar.list[4].text='助手';
 assert.equal(tabs({...input(),app:renamed},'complete-release').state,'fail');
});
test('备案办理中只允许准备检查，默认提审保持失败',()=>{
 const x=input(); x.legal.operator.icp='';
 assert.equal(inspect(x).ok,false);
 const prepared=inspect({...x,mode:'prepare'});
 assert.equal(prepared.ok,true); assert.equal(prepared.readyToSubmit,false);
 assert.equal(prepared.checks.find(c=>c.id==='filing').state,'warning');
});
test('准备模式不能放过真实配置错误或未确认页面',()=>{
 for (const change of [x=>x.config.devBaseUrl='http://localhost',x=>x.legal.final=false,
  x=>x.legal.operator.email='【待填】',x=>x.app.pages.push('pages/pay/index'),
  x=>x.app.subPackages=[{root:'hidden',pages:['pay']}],x=>x.app.pages.pop(),x=>x.config.aiInternal=true]) {
  const x=input();change(x);assert.equal(inspect({...x,mode:'prepare'}).ok,false);
 }
});
test('部分模板必须逐项报告，不能把已开提醒说成全关',()=>{
 const x=input();x.config.templates.newDocs='test-template';
 const r=inspect(x);assert.equal(r.ok,true);
 assert.equal(r.checks.find(c=>c.id==='template-newDocs').state,'pass');
 assert.equal(r.checks.find(c=>c.id==='template-deadline').state,'warning');
});
test('AI内测永远不能通过提审，正式包必须明确关内测',()=>{
 const x=input();x.app.pages.push(...['advisor','advisor-interpret','advisor-clause','advisor-tasks','complaint'].map(p=>`pages/${p}/index`));x.config.aiInternal=true;
 assert.equal(inspect({...x,profile:'ai-internal',mode:'prepare'}).ok,true);
 assert.equal(inspect({...x,profile:'ai-internal'}).ok,false);
 assert.equal(inspect({...x,profile:'ai-release'}).ok,false);
 x.config.aiInternal=false; assert.equal(inspect({...x,profile:'ai-release'}).ok,false);
 x.legal.version='2026-11';
 // 只升版本号、协议里没有 AI 那一节：不过（Codex 10/26）
 assert.equal(inspect({...x,profile:'ai-release'}).checks.find(c=>c.id==='ai-terms').state,'fail');
 x.legal.user.sections.push({h:'八、AI 功能与生成内容标识',p:['一','二','三']}); x.legal.privacy.sections.push({h:'一、',p:['交给已备案的大模型处理']});
 const formal=inspect({...x,profile:'ai-release'}); assert.equal(formal.ok,true); assert.equal(formal.readyToSubmit,false);
 x.legal.user.sections.at(-1).p=['一','二']; assert.equal(inspect({...x,profile:'ai-release'}).checks.find(c=>c.id==='ai-terms').state,'fail');
});
test('未知参数失败；结构化输出不含运营者数据',()=>{
 assert.throws(()=>inspect({...input(),mode:'bypass'}));
 const x=input();x.legal.operator.email='private@example.test';
 assert.equal(JSON.stringify(inspect(x)).includes('private@example.test'),false);
});

test('完整候选严格限定 28 页（含站内阅读与结果页），不能借此放宽旧版本或提交审核',()=>{
 const x=input(); x.config.aiInternal=true;
 x.app.pages.push(...['advisor','advisor-interpret','advisor-clause','advisor-tasks','complaint','advisor-form','advisor-terms','advisor-trial','pass','orders','ai','help'].map(p=>`pages/${p}/index`)); x.app.tabBar.list.push({pagePath:'pages/ai/index',text:'AI'});
 x.toolTerms={version:SERVER_TOOL_TERMS_VERSION,reviewedForRelease:false,paragraphs:['上传','授权','留存','删除','免费','结果边界']};
 const r=inspect({...x,profile:'complete-candidate',mode:'prepare'});
 assert.equal(r.ok,true);assert.equal(r.readyToSubmit,false);
 assert.equal(r.checks.find(c=>c.id==='tool-review').state,'warning');
 // 付费协议没传或没定稿：只提示，不挡体验准备
 assert.equal(r.checks.find(c=>c.id==='pay-terms').state,'warning');
 assert.equal(inspect({...x,payTerms:{final:true,text:'付费服务协议正文'},profile:'complete-candidate',mode:'prepare'}).checks.find(c=>c.id==='pay-terms').state,'pass');
 assert.equal(inspect({...x,payTerms:{final:true,text:'【付费服务协议：待律师定稿】'},profile:'complete-candidate',mode:'prepare'}).checks.find(c=>c.id==='pay-terms').state,'warning');
 for(const profile of ['first-release','ai-internal','ai-release']) assert.equal(inspect({...x,profile,mode:'prepare'}).ok,false);
 assert.equal(inspect({...x,profile:'complete-candidate',mode:'submit'}).ok,false);
 // 奖励等不在范围的页面、缺了购买页或订单页、子包都不行
 for(const change of [y=>y.app.pages.push('pages/rewards/index'),y=>y.app.pages.pop(),y=>{y.app.pages=y.app.pages.filter(p=>p!=='pages/pass/index');},
   y=>{y.app.pages=y.app.pages.filter(p=>p!=='pages/advisor-trial/index');},y=>{y.app.pages=y.app.pages.filter(p=>p!=='pages/help/index');},
   // AI 板块（D-075）：底栏第 5 项必须是板块首页、放在最后；没有也不行
   y=>{y.app.tabBar.list.pop();},y=>{y.app.tabBar.list.unshift(y.app.tabBar.list.pop());},y=>{y.app.subPackages=[{root:'pay',pages:['index']}];},y=>y.config.aiInternal=false,y=>y.toolTerms.version='old',y=>y.toolTerms=null]) {
   const y=JSON.parse(JSON.stringify(x));change(y);assert.equal(inspect({...y,profile:'complete-candidate',mode:'prepare'}).ok,false);
 }
});
test('完整正式包（10/18）：页面同完整候选，必须关 AI 内测、协议排在 2026-11.1 以后（D-098），备案号齐了才能通过提审检查',()=>{
 const x=input(); x.config.aiInternal=false; x.legal.version='2026-12';
 // 正式 AI 包的协议里要有 AI 那一节、隐私保护指引写到模型（Codex 10/26）
 x.legal.user.sections.push({h:'八、AI 功能与生成内容标识',p:['一','二','三']}); x.legal.privacy.sections.push({h:'一、',p:['交给已备案的大模型处理']});
 x.app.pages.push(...['advisor','advisor-interpret','advisor-clause','advisor-tasks','complaint','advisor-form','advisor-terms','advisor-trial','pass','orders','ai','help'].map(p=>`pages/${p}/index`)); x.app.tabBar.list.push({pagePath:'pages/ai/index',text:'AI'});
 x.toolTerms={version:SERVER_TOOL_TERMS_VERSION,reviewedForRelease:false,paragraphs:['上传','授权','留存','删除','免费','结果边界']};
 const r=inspect({...x,profile:'complete-release',mode:'submit'});
 // 备案号、邮箱、协议都齐：静态提审检查可以通过（工具说明复核、付费协议、模板只提示）；静态结果仍不授予提交权限
 assert.equal(r.ok,true); assert.equal(r.readyToSubmit,false);
 assert.equal(r.checks.find(c=>c.id==='internal-submit').state,'pass');
 assert.equal(r.checks.find(c=>c.id==='ai-terms').state,'pass');
 assert.equal(r.checks.find(c=>c.id==='tool-review').state,'warning');
 // 缺备案号：提审不过，体验准备只提示
 const noIcp=JSON.parse(JSON.stringify(x)); noIcp.legal.operator.icp='';
 assert.equal(inspect({...noIcp,profile:'complete-release',mode:'submit'}).ok,false);
 assert.equal(inspect({...noIcp,profile:'complete-release',mode:'prepare'}).ok,true);
 // 开着 AI 内测、协议还是旧版本、页面不对、工具说明版本不对：都不过
 // 协议还是 2026-11 或过审版加 AI 那一版：只同意过 2026-11.1 的人在完整版里不会被请去重新同意（D-098）
 for(const version of ['2026-11','2026-11.1']) { const y=JSON.parse(JSON.stringify(x)); y.legal.version=version; assert.equal(inspect({...y,profile:'complete-release',mode:'submit'}).checks.find(c=>c.id==='tools-terms-order').state,'fail',version); }
 assert.equal(r.checks.find(c=>c.id==='tools-terms-order').state,'pass');
 for(const change of [y=>y.config.aiInternal=true,y=>y.legal.version='2026-10',y=>y.app.pages.pop(),y=>y.app.pages.push('pages/rewards/index'),
   y=>{y.app.subPackages=[{root:'pay',pages:['index']}];},y=>y.toolTerms.version='old',y=>y.toolTerms=null]) {
   const y=JSON.parse(JSON.stringify(x));change(y);assert.equal(inspect({...y,profile:'complete-release',mode:'submit'}).ok,false);
 }
 // 完整候选仍要求开着内测、且永远不能提交
 assert.equal(inspect({...x,profile:'complete-candidate',mode:'prepare'}).ok,false);
});
function assertNoIdentityValues(result) {
  const text = identityText(result);
  for (const value of [releaseIdentity.appid, releaseIdentity.cloudEnv, releaseIdentity.cloudService, 'wx3251e7a7b42bc59d', 'other-env', 'other-service']) {
    assert.equal(text.includes(value), false);
  }
}
test('旧 AppID、缺失 AppID、错误云环境或服务不能通过准备或提审',()=>{
  const cases = [
    [x => { x.project.appid = 'wx3251e7a7b42bc59d'; }, 'pass'],
    [x => { delete x.project.appid; }, 'pass'],
    [x => { x.project = {}; }, 'pass'],
    [x => { x.project.appid = ''; }, 'pass'],
    [x => { delete x.project; }, 'pass'],
    [x => { x.config.cloudEnv = 'other-env'; }, 'fail'],
    [x => { x.config.cloudEnv = ''; }, 'fail'],
    [x => { delete x.config.cloudEnv; }, 'fail'],
    [x => { x.config.cloudService = 'other-service'; }, 'fail'],
    [x => { x.config.cloudService = ''; }, 'fail'],
    [x => { delete x.config.cloudService; }, 'fail'],
  ];
  for (const [change, cloudState] of cases) {
    for (const mode of ['prepare', 'submit']) {
      const x = input();
      change(x);
      const r = inspect({ ...x, mode });
      assert.equal(r.ok, false);
      assert.equal(r.readyToSubmit, false);
      assert.equal(r.checks.find(c => c.id === 'release-identity').state, 'fail');
      assert.equal(r.checks.find(c => c.id === 'cloud').state, cloudState);
      assertNoIdentityValues(r);
    }
  }
});
test('已确认正式发布身份可以通过静态对照，环境变量不能改写',()=>{
  const previousApp = process.env.YOUJU_APPID;
  const previousEnv = process.env.YOUJU_CLOUD_ENV;
  process.env.YOUJU_APPID = 'wx3251e7a7b42bc59d';
  process.env.YOUJU_CLOUD_ENV = 'other-env';
  try {
    for (const mode of ['prepare', 'submit']) {
      const r = inspect({ ...input(), mode });
      assert.equal(r.ok, true);
      assert.equal(r.readyToSubmit, false);
      assert.equal(r.externalEvidence, 'not_checked');
      assert.equal(r.checks.find(c => c.id === 'release-identity').state, 'pass');
      assert.equal(r.checks.find(c => c.id === 'cloud').state, 'pass');
      assertNoIdentityValues(r);
    }
    const project = JSON.parse(fs.readFileSync(path.join(__dirname, '../../apps/miniapp/project.config.json'), 'utf8'));
    const config = require('../../apps/miniapp/utils/config.js');
    const committed = input();
    committed.project = { appid: project.appid };
    committed.config.cloudEnv = config.cloudEnv;
    committed.config.cloudService = config.cloudService;
    const committedResult = inspect({ ...committed, mode: 'prepare' });
    assert.equal(committedResult.checks.find(c => c.id === 'release-identity').state, 'pass');
    assert.equal(committedResult.externalEvidence, 'not_checked');
    assertNoIdentityValues(committedResult);
  } finally {
    if (previousApp === undefined) delete process.env.YOUJU_APPID;
    else process.env.YOUJU_APPID = previousApp;
    if (previousEnv === undefined) delete process.env.YOUJU_CLOUD_ENV;
    else process.env.YOUJU_CLOUD_ENV = previousEnv;
  }
});

test('小程序里的工具使用说明和后台是同一版本：后台改了版本小程序没改（或反过来），用户同意了也会被拒',()=>{
 const terms=require('../../apps/miniapp/content/advisor-terms.js');
 assert.match(SERVER_TOOL_TERMS_VERSION,/^advisor-\d{4}-\d{2}-v\d+$/);
 assert.equal(terms.version,SERVER_TOOL_TERMS_VERSION);
});

test('过审版协议（D-093 ⑤）：首发包的协议不能写 AI、投诉举报页、原文链接，版本要排在后台 AI 条款版本之前', () => {
  const { SERVER_AI_TERMS_VERSION } = require('../miniapp_presubmit_check');
  const check = (x) => inspect({ ...x, mode: 'prepare' }).checks.find((c) => c.id === 'first-terms');
  assert.equal(check(input()).state, 'pass');
  for (const change of [
    (y) => { y.legal.version = SERVER_AI_TERMS_VERSION; },
    (y) => { y.legal.version = '2026-12'; },
    (y) => { delete y.legal.version; },
    (y) => { y.legal.user.sections[0].p.push('AI 功能的范围见第八条。'); },
    (y) => { y.legal.privacy.sections.push({ h: '四、', p: ['· AI 模型调用：我们调用已备案的大模型'] }); },
    (y) => { y.legal.user.sections[0].p.push('我们在条目中均附有官方原文链接。'); },
    (y) => { y.legal.disclaimer = '在“投诉举报”页提交'; },
  ]) {
    const x = input(); change(x);
    assert.equal(check(x).state, 'fail');
    assert.equal(inspect({ ...x, mode: 'prepare' }).ok, false);
  }
  // 别的版本不做这项检查
  const x = input(); x.config.aiInternal = false; x.legal.version = '2026-11';
  x.app.pages.push(...['advisor', 'advisor-interpret', 'advisor-clause', 'advisor-tasks', 'complaint'].map((p) => `pages/${p}/index`));
  assert.equal(inspect({ ...x, profile: 'ai-release', mode: 'prepare' }).checks.some((c) => c.id === 'first-terms'), false);
});

test('协议指纹（D-098）：正文改了没升版本号、这一版没登记都不过；仓库里两份定稿协议都已登记',()=>{
 const x=input(); const fingerprints={[x.legal.version]:agreementFingerprint(x.legal)};
 const state=(y,f)=>presubmit.inspect({...y,profile:'first-release',mode:'prepare',fingerprints:f}).checks.find(c=>c.id==='terms-fingerprint').state;
 assert.equal(state(x,fingerprints),'pass');
 const changed=JSON.parse(JSON.stringify(x)); changed.legal.user.sections[0].p[0]+='（改了一个字）';
 assert.equal(state(changed,fingerprints),'fail','改了正文没升版本号');
 assert.equal(state(x,{}),'fail','这一版没登记');
 for(const file of ['legal.js','legal-first-release.js']){
  const legal=require(path.join(__dirname,'..','..','apps','miniapp','content',file));
  assert.equal(presubmit.FINGERPRINTS[legal.version],agreementFingerprint(legal),file);
 }
});

test('过审版加 AI（D-098）：协议要是那一版、只写包里有的功能，后台也认它不含工具条款', () => {
  const real = JSON.parse(JSON.stringify(require('../../apps/miniapp/content/legal-first-release-ai.js')));
  const x = input();
  x.legal = { ...real, final: true, operator: x.legal.operator };
  const state = (y) => inspect({ ...y, profile: 'first-release-ai', mode: 'prepare' }).checks.find((c) => c.id === 'first-ai-terms').state;
  assert.equal(state(x), 'pass');
  assert.equal(x.legal.version, presubmit.FIRST_AI_TERMS.VERSION);
  assert.ok(presubmit.SERVER_WITHOUT_TOOLS_TERMS.includes(presubmit.FIRST_AI_TERMS.VERSION), '后台 agreements.WITHOUT_TOOLS_TERMS 里要有这一版');
  // 完整版协议（写着批量工具、加量体验、付费）不过
  const full = JSON.parse(JSON.stringify(require('../../apps/miniapp/content/legal.js')));
  assert.equal(state({ ...x, legal: { ...full, operator: x.legal.operator } }), 'fail');
  // 版本号不对不过
  assert.equal(state({ ...x, legal: { ...x.legal, version: '2026-11' } }), 'fail');
  // 加回一句包里没有的功能也不过
  for (const word of ['批量工具', '加量体验', '订单', '原文链接', '统一社会信用代码']) {
    const y = JSON.parse(JSON.stringify(x));
    y.legal.privacy.sections[1].p.push(`测试：${word}`);
    assert.equal(state(y), 'fail', word);
  }
  // 别的版本没有这一项
  assert.equal(inspect({ ...x, profile: 'first-release', mode: 'prepare' }).checks.find((c) => c.id === 'first-ai-terms'), undefined);
});
