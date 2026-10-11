// 用轻量 DOM 运行核对页原脚本，覆盖真实事件处理和请求，不复制页面逻辑。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../../src/youju/admin/review.html'), 'utf8');
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
  ids.due.value = 'all'; ids.token.value = 'local-token';
  // 后台按核对顺序（order_key 升序）返回：3 建议通过排最前，2、1 按截止日期从早到晚
  const day = { 3: 15, 2: 15, 1: 16 };
  const hints = { 3: ['suggest', '建议通过'], 2: ['mismatch', '规则与 AI 不一致'], 1: ['unchecked', 'AI 未核'] };
  const items = [3, 2, 1].map(id => ({ id, document_id: id, title: `标题 <img> ${id}`, url: '/official',
    source: '测试来源', publish_date: '2026-09-30', kind: id === 1 ? '推荐或审核截止' : '企业申报截止',
    candidate_date: `2026-10-${day[id]}`, candidate_time: '17:00', evidence: `请于2026年10月${day[id]}日前申报`,
    confidence: '高', hint: hints[id][0], hint_text: hints[id][1],
    order_key: `${id === 3 ? 0 : 1}|2026-10-${day[id]}|${String(id).padStart(12, '0')}` }));
  const status = new Map(items.map(i => [i.id, 'pending']));
  const calls = [];
  let token = 'parent-token', now = 1000, failure = null, release = null, hold = false;
  const timers = new Map(); let timerId = 0;
  const context = { document, URLSearchParams, location: { search: embedded ? '?embed=1&due=all' : '', origin: 'https://admin.example' },
    localStorage: { getItem: () => '创始人', setItem() {} },
    Date: class extends Date { static now() { return now; } },
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, options = {}) => {
      calls.push({ url, ...options });
      const reply = (code, data) => ({ ok: code === 200, status: code, json: async () => data });
      if (url === '/api/admin/deadline-clues') return reply(200, { enabled: false, items: [] });
      if (url.includes('/count?')) return reply(200, { pending: [...status.values()].filter(s => s === 'pending').length });
      if (url.includes('?limit=')) return reply(200, items.filter(i => status.get(i.id) === 'pending'));
      const [, id, action] = url.match(/deadlines\/(\d+)\/(\w+)/);
      if (hold) { hold = false; await new Promise(resolve => { release = resolve; }); }
      if (action === 'undo' && failure) { const code = failure; failure = null; return reply(code, { detail: code === 409 ? '这条已被质检确认，不能撤回核对' : '管理接口未授权' }); }
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
    holdNext: () => { hold = true; }, release: () => release(),
    cards: () => ids.list.children.filter(n => n.tagName === 'SECTION'),
  };
}
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const current = h => h.cards().find(c => c.classList.contains('current'));
const title = card => card.querySelector('.title').textContent;
const posts = h => h.calls.filter(c => c.method === 'POST');

(async () => {
  for (const embedded of [false, true]) {
    const h = harness(embedded);
    if (embedded) h.ready(); else h.ids.load.click();
    await settle();
    assert.equal(h.ids.undoLast.disabled, true);
    assert.equal(title(current(h)), '标题 <img> 3');
    // 按核对顺序取；截止类型用色块显示在日期旁边，AI 预审标签只是提示，载入后不会自动通过任何一条
    const listCalls = () => h.calls.filter(c => c.url.includes('?limit='));
    assert.ok(listCalls()[0].url.includes('&order=priority') && !listCalls()[0].url.includes('after='));
    const kinds = h.cards().map(c => [c.querySelector('.kind-big').textContent, c.querySelector('.kind-big').className]);
    assert.deepEqual(kinds, [['企业申报截止', 'kind-big k-apply'], ['企业申报截止', 'kind-big k-apply'], ['推荐或审核截止', 'kind-big k-recommend']]);
    assert.deepEqual(h.cards().map(c => c.querySelector('.ai').textContent), ['建议通过', '规则与 AI 不一致', 'AI 未核']);
    assert.equal(posts(h).length, 0);
    h.ids.more.click(); await settle();
    assert.ok(listCalls().at(-1).url.endsWith(`&after=${encodeURIComponent('1|2026-10-16|000000000001')}`));
    assert.equal(h.cards().length, 3);
    h.key('j'); assert.equal(title(current(h)), '标题 <img> 2'); assert.equal(current(h).scrolled, true);
    h.key('k'); h.key('k'); assert.equal(title(current(h)), '标题 <img> 3');
    for (const target of [h.ids.reviewer, h.ids.token, h.ids.due, h.document.createElement('textarea')]) h.key('a', target);
    const editable = h.document.createElement('div'); editable.contenteditable = 'true'; h.key('x', editable);
    h.key('a', h.document.body, { ctrlKey: true }); h.key('a', h.document.body, { repeat: true });
    assert.equal(posts(h).length, 0);
    h.holdNext(); h.key('a'); h.key('a'); h.key('u'); h.key('r');
    await settle(); assert.equal(posts(h).length, 1);
    assert.equal(h.ids.due.disabled, true); assert.equal(h.ids.load.disabled, true);
    h.release(); await settle();
    assert.equal(h.ids.due.disabled, false); assert.equal(h.ids.load.disabled, false);
    assert.equal(title(current(h)), '标题 <img> 2');
    assert.equal(h.cards()[0].hidden, true);
    assert.equal(h.ids.reviewNoticeText.textContent, '已通过：标题 <img> 3');
    assert.equal(h.ids.undoLast.disabled, false);
    const expectedToken = embedded ? 'parent-token' : 'local-token';
    assert.equal(posts(h)[0].headers['X-Admin-Token'], expectedToken);
    h.setToken('updated-token'); h.ids.undoNotice.click(); await settle();
    assert.equal(JSON.parse(posts(h).at(-1).body).reviewer, '创始人');
    assert.equal(posts(h).at(-1).headers['X-Admin-Token'], embedded ? 'updated-token' : 'local-token');
    assert.equal(title(current(h)), '标题 <img> 3');
    assert.deepEqual(h.cards().map(title), ['标题 <img> 3', '标题 <img> 2', '标题 <img> 1']);
    assert.equal(h.ids.undoLast.disabled, true);
    h.key('x'); await settle(); assert.equal(h.ids.reviewNoticeText.textContent, '已驳回：标题 <img> 3');
    h.key('e'); const date = h.document.activeElement; assert.equal(date.className, 'd');
    h.key('x'); assert.equal(posts(h).length, 3); // 输入框里不驳回
    date.value = '2026-10-20'; h.key('Enter'); await settle();
    assert.deepEqual(JSON.parse(posts(h).at(-1).body), { reviewed_by: '创始人', date: '2026-10-20', time: '17:00' });
    assert.equal(title(current(h)), '标题 <img> 1'); assert.equal(h.document.activeElement, h.document.body);
    h.key('u'); await settle(); assert.equal(title(current(h)), '标题 <img> 2');
    assert.equal(current(h).querySelector('.d').value, '2026-10-15');
    h.key('a'); await settle();
    const notice = h.ids.reviewNoticeText.textContent;
    h.key('r'); await settle(); assert.equal(h.ids.reviewNoticeText.textContent, notice);
    assert.deepEqual(h.cards().map(title), ['标题 <img> 1']);
    h.key('u'); await settle(); assert.deepEqual(h.cards().map(title), ['标题 <img> 2', '标题 <img> 1']);
    h.key('x'); await settle();
    h.ids.reviewer.value = '另一人'; h.ids.reviewer.oninput();
    assert.equal(h.ids.undoLast.disabled, true);
    h.ids.reviewer.value = '创始人'; h.ids.reviewer.oninput();
    h.failUndo(401); h.key('u'); await settle(); assert.equal(h.ids.undoLast.disabled, false);
    assert.equal(h.ids.undoError.textContent, '管理接口未授权');
    h.failUndo(409); h.key('u'); await settle(); assert.equal(h.ids.undoLast.disabled, true);
    assert.equal(h.ids.undoError.textContent, '这条已被质检确认，不能撤回核对');
    assert.equal(h.status.get(2), 'rejected');
    h.key('a'); await settle(); assert.equal(current(h), undefined);
    h.expire(); assert.equal(h.ids.undoLast.disabled, true);
    const count = posts(h).length; h.key('u'); await settle(); assert.equal(posts(h).length, count);
  }
  console.log('独立页面与嵌入页面：快捷键、输入保护、原位撤回、重载、超时和失败处理通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
