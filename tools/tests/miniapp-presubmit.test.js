const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { inspect, SERVER_TOOL_TERMS_VERSION } = require('../miniapp_presubmit_check');
const releaseIdentity = require('../release/release-identity.json');
function input() { return {
  legal:{final:true, operator:{icp:'测试备案号',email:'contact@example.test'}},
  config:{cloudEnv:releaseIdentity.cloudEnv,cloudService:releaseIdentity.cloudService,devBaseUrl:'',templates:{newDocs:'',deadline:''}},
  project:{appid:releaseIdentity.appid},
  app:{pages:['home','follow','files','me','search','detail','report','lists','agreement','feedback','follow-new','my-data','about','clients'].map(p=>`pages/${p}/index`),tabBar:{list:['home','follow','files','me'].map(p=>({pagePath:`pages/${p}/index`}))}}
}; }
function identityText(result) {
  return JSON.stringify(result);
}
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
 x.legal.version='2026-11'; const formal=inspect({...x,profile:'ai-release'}); assert.equal(formal.ok,true); assert.equal(formal.readyToSubmit,false);
});
test('未知参数失败；结构化输出不含运营者数据',()=>{
 assert.throws(()=>inspect({...input(),mode:'bypass'}));
 const x=input();x.legal.operator.email='private@example.test';
 assert.equal(JSON.stringify(inspect(x)).includes('private@example.test'),false);
});

test('完整候选严格限定 25 页（含加量体验确认页、购买页、订单页、本期清单页），不能借此放宽旧版本或提交审核',()=>{
 const x=input(); x.config.aiInternal=true;
 x.app.pages.push(...['advisor','advisor-interpret','advisor-clause','advisor-tasks','complaint','advisor-form','advisor-terms','advisor-trial','pass','orders','digest'].map(p=>`pages/${p}/index`));
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
   y=>{y.app.pages=y.app.pages.filter(p=>p!=='pages/advisor-trial/index');},y=>{y.app.pages=y.app.pages.filter(p=>p!=='pages/digest/index');},y=>{y.app.subPackages=[{root:'pay',pages:['index']}];},y=>y.config.aiInternal=false,y=>y.toolTerms.version='old',y=>y.toolTerms=null]) {
   const y=JSON.parse(JSON.stringify(x));change(y);assert.equal(inspect({...y,profile:'complete-candidate',mode:'prepare'}).ok,false);
 }
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
