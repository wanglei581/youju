const test = require('node:test');
const assert = require('node:assert/strict');
const { inspect } = require('../miniapp_presubmit_check');
function input() { return {
  legal:{final:true, operator:{icp:'测试备案号',email:'contact@example.test'}},
  config:{cloudEnv:'test',cloudService:'api',devBaseUrl:'',templates:{newDocs:'',deadline:''}},
  app:{pages:['home','follow','files','me','search','detail','report','lists','agreement','feedback','follow-new','my-data','about','clients'].map(p=>`pages/${p}/index`),tabBar:{list:['home','follow','files','me'].map(p=>({pagePath:`pages/${p}/index`}))}}
}; }
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
