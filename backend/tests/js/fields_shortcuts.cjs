// 用轻量 DOM 运行核对页原脚本，覆盖真实事件处理和请求，不复制页面逻辑。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../../src/youju/admin/fields.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
const embed = fs.readFileSync(path.join(__dirname, '../../src/youju/admin/embed.js'), 'utf8');

function harness(embedded) {
  const listeners = {};
  const ids = {};
  let document;
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this.className = '';
      this.hidden = false; this.disabled = false; this.value = ''; this.style = {}; this.dataset = {};
      this.classList = {
        contains: c => this.className.split(' ').includes(c),
        add: c => { if (!this.classList.contains(c)) this.className += ` ${c}`; },
        remove: c => { this.className = this.className.split(' ').filter(v => v !== c).join(' '); },
      };
    }
    set textContent(text) { this.text = text; this.children.forEach(n => { if (typeof n === 'object') n.parentNode = null; }); this.children = []; }
    get textContent() { return this.text || ''; }
    get childNodes() { return this.children; }
    append(...nodes) {
      for (const node of nodes) {
        if (typeof node === 'object') { node.remove(); node.parentNode = this; }
        this.children.push(node);
      }
    }
    remove() {
      if (this.parentNode) {
        this.parentNode.children = this.parentNode.children.filter(n => n !== this);
        this.parentNode = null;
      }
    }
    before(node) {
      node.remove(); node.parentNode = this.parentNode;
      this.parentNode.children.splice(this.parentNode.children.indexOf(this), 0, node);
    }
    after(node) {
      node.remove(); node.parentNode = this.parentNode;
      this.parentNode.children.splice(this.parentNode.children.indexOf(this) + 1, 0, node);
    }
    replaceWith(node) { this.before(node); this.remove(); }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, fn) { this[`on${name}`] = fn; }
    matches(selector) {
      if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
      if (selector.startsWith('[contenteditable]')) return this.contenteditable && this.contenteditable !== 'false';
      return this.tagName.toLowerCase() === selector;
    }
    closest(selector) {
      return selector.split(',').some(s => this.matches(s.trim())) ? this : this.parentNode?.closest(selector);
    }
    querySelectorAll(selector) {
      const parts = selector.split(' ');
      const found = [];
      function visit(node) {
        for (const child of node.children) {
          if (typeof child !== 'object') continue;
          if (child.matches(parts.at(-1)) && (parts.length === 1 || child.parentNode.closest(parts[0]))) found.push(child);
          visit(child);
        }
      }
      visit(this); return found;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    contains(node) { return node === this || this.children.some(c => typeof c === 'object' && c.contains(node)); }
    focus() { document.activeElement = this; }
    blur() { document.activeElement = document.body; }
    scrollIntoView() { this.scrolled = true; }
    click() { if (!this.disabled) return this.onclick?.(); }
  }
  const body = new Element('body');
  const header = new Element('header'); body.append(header);
  const auth = new Element('div'); auth.className = 'auth'; header.append(auth);
  document = { body, activeElement: body, createElement: tag => new Element(tag),
    getElementById: id => ids[id], querySelector: s => body.querySelector(s),
    addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
  };
  for (const match of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = ids[match[2]] = new Element(match[1]);
    (['reviewer', 'token', 'due', 'load'].includes(match[2]) ? auth : body).append(node);
    node.hidden = match[0].includes(' hidden'); node.disabled = match[0].includes(' disabled');
  }
  ids.token.value = 'local-token';
  const items = [1, 2].map(id => ({ document_id: id, title: `标题 <img> ${id}`, url: '/official',
    deadline: '2026-10-15', fields: (id === 1 ? [11, 12] : [21]).map(fieldId => ({
      id: fieldId, field: 'support', value: fieldId === 12 ? '资金支持' : '贷款贴息',
      evidence: '企业可申请贷款贴息。', status: fieldId === 12 ? 'stale' : 'pending', approved_value: null,
    })) }));
  const status = new Map(items.flatMap(i => i.fields.map(f => [f.id, 'pending'])));
  const calls = [];
  let token = 'parent-token', now = 1000, failure = null, release = null, hold = false, reviewFailure = null, loadFailure = false;
  const timers = new Map(); let timerId = 0;
  const context = { document, URLSearchParams, location: { search: embedded ? '?embed=1&due=all' : '', origin: 'https://admin.example' },
    localStorage: { getItem: key => { assert.equal(key, 'youjuReviewer'); return '创始人'; }, setItem(key, value) { assert.equal(key, 'youjuReviewer'); assert.equal(value, '创始人'); } },
    Date: class extends Date { static now() { return now; } },
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, options = {}) => {
      calls.push({ url, ...options });
      const reply = (code, data) => ({ ok: code === 200, status: code, json: async () => data });
      if (url.endsWith('/count')) return reply(200, { pending: [...status.values()].filter(s => s === 'pending').length });
      if (url.includes('?limit=')) {
        if (loadFailure) { loadFailure = false; throw new Error('网络不可用'); }
        return reply(200, items.map(i => ({ ...i, fields: i.fields.filter(f => status.get(f.id) === 'pending') })).filter(i => i.fields.length));
      }
      const [, id, action] = url.match(/fields\/(\d+)\/(\w+)/);
      if (hold) { hold = false; await new Promise(resolve => { release = resolve; }); }
      if (action === 'undo' && failure) { const code = failure; failure = null; return reply(code, { detail: code === 409 ? '原文已变化，不能撤回核对，请重新核对' : '管理接口未授权' }); }
      if (action !== 'undo' && reviewFailure) { const code = reviewFailure; reviewFailure = null; return reply(code, { detail: '支持方式必须是固定标签' }); }
      status.set(Number(id), action === 'undo' ? 'pending' : action === 'reject' ? 'rejected' : 'approved');
      return reply(200, { status: status.get(Number(id)) });
    },
  };
  context.window = { parent: { location: { origin: context.location.origin }, getYoujuAdminToken: () => token } };
  vm.createContext(context); vm.runInContext(embed, context); vm.runInContext(script, context);
  const key = (name, target = document.activeElement, extra = {}) => {
    const event = { key: name, target, preventDefault() { this.defaultPrevented = true; }, ...extra };
    target.onkeydown?.(event);
    for (const handler of listeners.keydown || []) handler(event);
  };
  return { ids, items, calls, key, document, timers, status,
    ready: () => (listeners.DOMContentLoaded || []).forEach(fn => fn()),
    setToken: value => { token = value; }, failUndo: code => { failure = code; },
    expire: () => { now += 30 * 60 * 1000 + 1; timers.forEach(fn => fn()); },
    holdNext: () => { hold = true; }, release: () => release(), failReview: code => { reviewFailure = code; }, failLoad: () => { loadFailure = true; },
    cards: () => ids.list.children.filter(n => n.tagName === 'ARTICLE'),
    fields: () => ids.list.querySelectorAll('.field'),
  };
}
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const current = h => h.fields().find(c => c.classList.contains('current'));
const value = field => field.querySelector('b').textContent;
const title = card => card.querySelector('.title').textContent;
const posts = h => h.calls.filter(c => c.method === 'POST');

(async () => {
  for (const embedded of [false, true]) {
    const h = harness(embedded);
    if (embedded) h.ready(); else h.ids.load.click();
    await settle();
    assert.equal(h.ids.reviewer.value, '创始人');
    assert.equal(h.ids.undoLast.disabled, true);
    assert.equal(h.fields().length, 3);
    assert.deepEqual(h.cards().map(title), ['标题 <img> 1', '标题 <img> 2']);
    assert.equal(value(current(h)), '贷款贴息');
    assert.equal(current(h).querySelector('.edit-panel').hidden, true);
    assert.deepEqual(current(h).querySelectorAll('button').slice(0, 3).map(b => b.textContent), ['通过', '改后通过', '驳回']);
    h.key('j'); assert.equal(value(current(h)), '资金支持'); assert.equal(current(h).scrolled, true);
    h.key('k'); h.key('k'); assert.equal(value(current(h)), '贷款贴息');
    for (const target of [h.ids.reviewer, h.ids.token, h.document.createElement('select'), h.document.createElement('textarea')]) h.key('a', target);
    const editable = h.document.createElement('div'); editable.contenteditable = 'true'; h.key('x', editable);
    for (const extra of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { repeat: true }, { isComposing: true }]) h.key('a', h.document.body, extra);
    assert.equal(posts(h).length, 0);
    h.ids.reviewer.value = ''; h.key('a'); assert.equal(posts(h).length, 0);
    assert.equal(current(h).querySelector('.msg').textContent, '请先填写核对人'); h.ids.reviewer.value = '创始人';
    h.holdNext(); h.key('a'); h.key('a'); h.key('u'); h.key('r'); h.key('j');
    await settle(); assert.equal(posts(h).length, 1); assert.equal(h.ids.load.disabled, true);
    h.release(); await settle();
    assert.equal(h.ids.load.disabled, false);
    assert.equal(value(current(h)), '资金支持');
    assert.equal(h.fields()[0].hidden, true); assert.equal(h.cards()[0].hidden, false);
    assert.equal(h.ids.reviewNoticeText.textContent, '已通过：标题 <img> 1 · 贷款贴息');
    assert.equal(h.ids.undoLast.disabled, false);
    const expectedToken = embedded ? 'parent-token' : 'local-token';
    assert.equal(posts(h)[0].headers['X-Admin-Token'], expectedToken);
    assert.deepEqual(JSON.parse(posts(h)[0].body), { reviewed_by: '创始人' });
    h.setToken('updated-token'); h.ids.undoNotice.click(); await settle();
    assert.equal(JSON.parse(posts(h).at(-1).body).reviewer, '创始人');
    assert.equal(posts(h).at(-1).headers['X-Admin-Token'], embedded ? 'updated-token' : 'local-token');
    assert.equal(value(current(h)), '贷款贴息'); assert.equal(h.ids.undoLast.disabled, true);
    // 按钮路径及修改提交，撤回后清除输入值并回到原候选。
    current(h).querySelectorAll('button')[2].click(); await settle();
    assert.equal(h.ids.reviewNoticeText.textContent, '已驳回：标题 <img> 1 · 贷款贴息');
    current(h).querySelectorAll('button')[1].click();
    const input = h.document.activeElement; assert.equal(input.tagName, 'INPUT');
    assert.equal(current(h).querySelector('.edit-panel').hidden, false);
    h.key('x'); assert.equal(posts(h).length, 3);
    input.value = ''; h.key('Enter'); await settle(); assert.equal(posts(h).length, 3);
    input.value = '资金支持'; h.key('Enter', input, { isComposing: true }); assert.equal(posts(h).length, 3);
    input.value = '非法标签'; h.failReview(422); h.key('Enter'); await settle();
    assert.equal(current(h).querySelector('.msg').textContent, '支持方式必须是固定标签');
    assert.equal(current(h).querySelector('button').disabled, false);
    input.value = '  贷款贴息  '; h.key('Enter'); await settle();
    assert.deepEqual(JSON.parse(posts(h).at(-1).body), { reviewed_by: '创始人', value: '贷款贴息' });
    assert.equal(h.ids.reviewNoticeText.textContent, '已通过：标题 <img> 1 · 贷款贴息');
    assert.equal(h.cards()[0].hidden, true); assert.equal(h.document.activeElement, h.document.body);
    h.key('u'); await settle(); assert.equal(value(current(h)), '资金支持');
    assert.equal(current(h).querySelector('input').value, '资金支持');
    assert.equal(current(h).querySelector('.edit-panel').hidden, true); assert.equal(h.cards()[0].hidden, false);
    // 同文档剩余字段，以及整张文档都被重载移除的两种恢复路径。
    h.key('r'); await settle(); h.key('u'); await settle(); // 已撤回后不可再撤回
    h.key('a'); await settle();
    const notice = h.ids.reviewNoticeText.textContent;
    h.key('r'); await settle(); assert.equal(h.ids.reviewNoticeText.textContent, notice);
    assert.deepEqual(h.cards().map(title), ['标题 <img> 2']);
    h.key('u'); await settle(); assert.deepEqual(h.cards().map(title), ['标题 <img> 1', '标题 <img> 2']);
    assert.equal(value(current(h)), '资金支持');
    h.key('x'); await settle();
    h.ids.reviewer.value = '另一人'; h.ids.reviewer.oninput(); assert.equal(h.ids.undoLast.disabled, true);
    h.ids.reviewer.value = '创始人'; h.ids.reviewer.oninput();
    h.failUndo(401); h.key('u'); await settle(); assert.equal(h.ids.undoLast.disabled, false);
    assert.equal(h.ids.undoError.textContent, '管理接口未授权');
    h.failUndo(409); h.key('u'); await settle(); assert.equal(h.ids.undoLast.disabled, true);
    assert.equal(h.ids.undoError.textContent, '原文已变化，不能撤回核对，请重新核对');
    h.key('e'); assert.equal(h.document.activeElement.tagName, 'INPUT');
    h.document.activeElement.blur(); h.key('a'); await settle(); assert.equal(current(h), undefined);
    h.key('r'); await settle(); assert.equal(h.ids.list.querySelectorAll('.empty').length, 1);
    h.key('u'); await settle(); assert.equal(h.ids.list.querySelectorAll('.empty').length, 0);
    assert.equal(title(h.cards()[0]), '标题 <img> 2');
    h.failLoad(); h.key('r'); await settle(); assert.equal(h.ids.loadError.textContent, '网络不可用');
    assert.equal(value(current(h)), '贷款贴息');
    h.key('x'); await settle(); h.expire(); assert.equal(h.ids.undoLast.disabled, true);
    const count = posts(h).length; h.key('u'); await settle(); assert.equal(posts(h).length, count);
  }
  // 同文档第一字段被重载移除，撤回后仍插在第二字段之前。
  const h = harness(false); h.ids.load.click(); await settle(); h.key('a'); await settle();
  h.key('r'); await settle(); h.key('u'); await settle();
  assert.deepEqual(h.fields().map(value), ['贷款贴息', '资金支持', '贷款贴息']);
  console.log('独立与嵌入页面：逐字段快捷键、按钮、输入保护、修改、撤回、重载及失败处理通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
