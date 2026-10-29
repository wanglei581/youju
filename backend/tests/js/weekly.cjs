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
const paths = [{label:'本期清单',path:'pages/digest/index?id=1',published_only:true},{label:'看截止',path:'pages/report/index',published_only:false}];
const issue = { id:1,profile_id:1,title:'申报周报｜9月28日—10月4日（全国）：新通知 4 条',generated_at:'2026-10-05T00:00:00Z',status:'draft',counts:{lists:1},note:'',body_html:'<section><p>最新正文</p></section>',body_text:'最新正文',body_text_with_links:'题录｜机关｜2026-09-28｜截止时间见原文｜https://example.gov.cn/1',paths };
const reminder = {...issue,id:2,kind:'reminder',title:'临期提醒',counts:{reminder:3}};
const flashIssue = {...issue,id:3,kind:'list_flash',title:'名单速报｜关于公示2026年第一批拟认定高新技术企业名单的通知（共12家）',counts:{entries:12},subject_id:5,paths:[]};
const lists = [{id:5,title:'名单公示',issuer:'省科技厅',province:'浙江',published:'2026-10-09',entries:12,issue_id:3,issue_status:'draft'},{id:6,title:'名单',issuer:'省工信厅',province:'国家部委',published:'2026-10-08',entries:0,issue_id:null,issue_status:null}];
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
    else if(path.includes('/issues?')) data={items:[issue,reminder,flashIssue],total:3};
    else if(path.includes('/lists?profile_id=1')) data={items:lists};
    else if(path.endsWith('/generate')) data={issue_id:1,outcome:'generated',reason:'草稿已生成'};
    else if(path.endsWith('/issues/1')) data=blocked?{detail:'内容已下架'}:issue;
    else if(path.endsWith('/issues/3')) data=flashIssue;
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
  // 标题字数照实写（公众号后台超出上限会提示），不写死上限
  assert(elements.get('issueMeta').textContent.endsWith(`标题 ${Array.from(issue.title).length} 字`));
  // 公众号编辑器里要填的小程序页面路径：本期清单还没标为已发布时写明读者打不开
  const pathRows=elements.get('paths').children;
  assert.equal(pathRows[0].textContent,'在公众号文章里插入小程序链接时填的页面路径');
  assert.deepEqual(pathRows.slice(1).map(row=>row.children[1].textContent),['pages/digest/index?id=1','pages/report/index']);
  assert(pathRows[1].children[3].textContent.includes('还没标为已发布'));
  assert.equal(pathRows[2].children.length,3);
  const generated=()=>calls.filter(c=>c.path.endsWith('/generate')).map(c=>JSON.parse(c.options.body));
  assert.equal(generated()[0].kind,'weekly');
  elements.get('kind').value='reminder';
  await elements.get('generate').onclick();
  assert.deepEqual(generated()[1],{profile_id:1,week:null,kind:'reminder'});
  elements.get('kind').value='weekly';
  const cards=elements.get('issues').children.map(card=>card.children[1].textContent);
  assert(cards[0].startsWith('周报 · 草稿 · ') && cards[0].endsWith('本周名单 1 条'));
  assert(cards[1].startsWith('临期提醒 · 草稿 · ') && cards[1].endsWith('未来 7 天截止 3 条'));
  assert(cards[2].startsWith('名单速报 · 草稿 · ') && cards[2].endsWith('名单条数 12 条'));
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
  // 复制页面路径：和复制正文一样写剪贴板，不行就选中文字
  context.navigator.clipboard={write:async items=>writes.push(items),writeText:async value=>plain.push(value)};
  await elements.get('paths').children[1].children[2].onclick();
  assert.equal(plain.at(-1),'pages/digest/index?id=1');
  // 名单速报：选这一类稿件后显示名单和公示期，不用填统计周；生成时带上名单编号和公示期
  elements.get('kind').value='list_flash';
  elements.get('kind').onchange();
  assert.equal(elements.get('flashFields').hidden,false);
  assert.equal(elements.get('weekField').hidden,true);
  assert(elements.get('kindHint').textContent.includes('一份名单一篇'));
  elements.get('flashList').value='';
  await elements.get('generate').onclick();
  assert.equal(elements.get('message').textContent,'请先载入名单并选一份');
  await elements.get('loadLists').onclick();
  const options=elements.get('flashList').children;
  assert.deepEqual(options.map(o=>o.value),[5,6]);
  assert.equal(options[0].textContent,'浙江 · 名单公示 · 12 家 · 已有速报草稿');
  assert.equal(options[1].textContent,'国家部委 · 名单 · 条数见原文');
  elements.get('flashList').value='5';
  elements.get('week').value='2026-09-28';
  elements.get('period').value=' 2026年10月9日至10月15日 ';
  // 填了公示期没勾"已对照原文核对"：不提交
  const before=generated().length;
  await elements.get('generate').onclick();
  assert.equal(generated().length,before);
  assert(elements.get('message').textContent.includes('对照原文核对'));
  elements.get('periodChecked').checked=true;
  await elements.get('generate').onclick();
  assert.deepEqual(generated().at(-1),{profile_id:1,week:null,kind:'list_flash',subject_id:5,period:'2026年10月9日至10月15日',period_confirmed:true});
  // 名单速报照录原标题，超过 32 字（官方新建草稿接口的上限）时提示人工删减
  await elements.get('issues').children[2].children[2].onclick();
  assert(elements.get('issueMeta').textContent.includes('超过 32 字'));
  assert.equal(elements.get('paths').children.length,0);
  elements.get('kind').value='weekly';
  elements.get('kind').onchange();
  assert.equal(elements.get('flashFields').hidden,true);
  assert.equal(elements.get('weekField').hidden,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
