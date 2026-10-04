// 用浏览器接口替身执行实际页面脚本，验证复制格式、口令传递和失败回退。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const page = fs.readFileSync(require('node:path').join(__dirname, '../../src/youju/admin/weekly.html'), 'utf8');
const script = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
class Element {
  constructor() { this.value=''; this.children=[]; this.hidden=false; this.disabled=false; this.textContent=''; this.innerHTML=''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  scrollIntoView() {}
  focus() { this.focused=true; }
  select() { this.selected=true; }
  querySelectorAll() { return []; }
}
const elements = new Map();
for (const match of page.matchAll(/id="([^"]+)"/g)) elements.set(match[1], new Element());
let writes=[],plain=[],calls=[],blocked=false,selectionCount=0;
const issue = { id:1,profile_id:1,title:'政策申报周报',generated_at:'2026-10-05T00:00:00Z',status:'draft',counts:{lists:1},note:'',body_html:'<section><p>最新正文</p></section>',body_text:'最新正文',body_text_with_links:'题录｜机关｜2026-09-28｜截止时间见原文｜https://example.gov.cn/1' };
const reminder = {...issue,id:2,kind:'reminder',title:'申报临期提醒',counts:{reminder:3}};
const context = {
  URLSearchParams,Blob,
  document: {
    getElementById:id=>elements.get(id),
    createElement:()=>new Element(),
    createTextNode:text=>({textContent:text}),
    createRange:()=>({selectNodeContents:element=>assert.equal(element,elements.get('preview'))}),
  },
  window: {isSecureContext:true,getSelection:()=>({removeAllRanges(){},addRange(){selectionCount++;}})},
  ClipboardItem: class {constructor(data){this.data=data;}},
  navigator: {clipboard:{write:async items=>writes.push(items),writeText:async value=>plain.push(value)}},
  youjuAdminToken:()=> 'memory-only-test-token',
  fetch:async (path,options)=> {
    calls.push({path,options});
    assert.equal(options.headers['X-Admin-Token'],'memory-only-test-token');
    assert.equal(options.cache,'no-store');
    assert(!path.includes('memory-only-test-token'));
    let data;
    if(path.endsWith('/profiles')) data={items:[{id:1,name:'全国版'}],provinces:['浙江']};
    else if(path.includes('/issues?')) data={items:[issue,reminder],total:2};
    else if(path.endsWith('/generate')) data={issue_id:1,outcome:'generated',reason:'草稿已生成'};
    else if(path.endsWith('/issues/1')) data=blocked?{detail:'内容已下架'}:issue;
    else throw new Error(path);
    return {ok:!blocked || !path.endsWith('/issues/1'),status:blocked?409:200,json:async()=>data};
  },
};
context.window.ClipboardItem=context.ClipboardItem;
vm.runInNewContext(script,context);
(async()=> {
  await elements.get('load').onclick();
  elements.get('profileFilter').value='1';
  await elements.get('generate').onclick();
  assert.equal(elements.get('preview').innerHTML,issue.body_html);
  // 没有类型字段的旧数据按周报显示；生成时带上所选稿件类型。
  assert(elements.get('issueMeta').textContent.startsWith('周报 · '));
  const generated=()=>calls.filter(c=>c.path.endsWith('/generate')).map(c=>JSON.parse(c.options.body));
  assert.equal(generated()[0].kind,'weekly');
  elements.get('kind').value='reminder';
  await elements.get('generate').onclick();
  assert.deepEqual(generated()[1],{profile_id:1,week:null,kind:'reminder'});
  elements.get('kind').value='weekly';
  const cards=elements.get('issues').children.map(card=>card.children[1].textContent);
  assert(cards[0].startsWith('周报 · 草稿 · ') && cards[0].endsWith('本周名单 1 条'));
  assert(cards[1].startsWith('临期提醒 · 草稿 · ') && cards[1].endsWith('未来 7 天截止 3 条'));
  // 方案的栏目开关仍是周报的四个栏目，不含临期提醒。
  elements.get('new').onclick();
  assert.equal(elements.get('columns').children.length,4);
  await elements.get('copyHtml').onclick();
  assert.equal(writes.length,1);
  const item=writes[0][0];
  assert.deepEqual(Object.keys(item.data).sort(),['text/html','text/plain']);
  assert.equal(await item.data['text/html'].text(),issue.body_html);
  assert.equal(await item.data['text/plain'].text(),issue.body_text);
  await elements.get('copyText').onclick();
  await elements.get('copyLinks').onclick();
  await elements.get('copyTitle').onclick();
  assert.deepEqual(plain,[issue.body_text,issue.body_text_with_links,issue.title]);
  // 每次复制都再次取详情，下架后不能沿用已载入正文写入剪贴板。
  blocked=true;
  await elements.get('copyHtml').onclick();
  assert.equal(writes.length,1);
  assert.equal(elements.get('message').textContent,'内容已下架');
  await elements.get('copyLinks').onclick();
  assert.equal(plain.length,3);
  assert.equal(elements.get('message').textContent,'内容已下架');
  blocked=false;
  context.navigator.clipboard.write=async()=>{throw new Error('permission denied');};
  await elements.get('copyHtml').onclick();
  assert.equal(selectionCount,1);
  assert(elements.get('message').textContent.includes('手动复制'));
  context.navigator.clipboard=undefined;
  await elements.get('copyHtml').onclick();
  assert.equal(selectionCount,2);
  await elements.get('copyText').onclick();
  assert.equal(elements.get('preview').children[0].value,issue.body_text);
  assert(elements.get('preview').children[0].selected);
  await elements.get('copyLinks').onclick();
  assert.equal(elements.get('preview').children[0].value,issue.body_text_with_links);
  assert(elements.get('preview').children[0].selected);
  assert(calls.filter(c=>c.path.endsWith('/issues/1')).length>=7);
})().catch(error=>{console.error(error);process.exitCode=1;});
