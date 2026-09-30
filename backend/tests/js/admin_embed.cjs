const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname,'../../src/youju/admin/embed.js'),'utf8');
function context(search, parent) {
  const c={location:{search,origin:'https://admin.example'},URLSearchParams,
    document:{getElementById:()=>({value:'standalone'}),addEventListener:()=>{}}};
  c.window={parent}; vm.createContext(c); vm.runInContext(script,c); return c;
}
let token='first';
const same={location:{origin:'https://admin.example'},getYoujuAdminToken:()=>token};
const c=context('?embed=1',same);
assert.equal(c.youjuAdminToken(),'first'); token='changed'; assert.equal(c.youjuAdminToken(),'changed');
assert.equal(context('',same).youjuAdminToken(),'standalone');
assert.equal(context('?embed=1',{location:{origin:'https://other.example'},getYoujuAdminToken:()=>{throw Error('must not call')}}).youjuAdminToken(),'standalone');
const foreign={}; Object.defineProperty(foreign,'location',{get(){throw Error('cross origin')}});
assert.equal(context('?embed=1',foreign).youjuAdminToken(),'standalone');
assert.equal(context('?embed=1',{location:{origin:'https://admin.example'}}).youjuAdminToken(),'standalone');

// 隐藏原顶栏时，核对人、筛选、加载控件仍留在页面，首次嵌入自动加载。
let ready, clicked=0, toolbar;
const auth={}, header={style:{},querySelector:()=>auth,after:e=>toolbar=e};
const elements={token:{value:'',style:{}},count:{},due:{value:'30'},load:{click:()=>clicked++}};
const embedded={location:{search:'?embed=1&due=all',origin:'https://admin.example'},URLSearchParams,
  window:{parent:same},document:{
    addEventListener:(_,fn)=>ready=fn,querySelector:()=>header,getElementById:id=>elements[id],
    createElement:()=>({style:{},children:[],append(e){this.children.push(e)}})
  }};
vm.createContext(embedded); vm.runInContext(script,embedded); ready();
assert.equal(header.style.display,'none'); assert.equal(elements.token.style.display,'none');
assert.deepEqual(toolbar.children,[auth,elements.count]);
assert.equal(elements.due.value,'all'); assert.equal(clicked,1);
