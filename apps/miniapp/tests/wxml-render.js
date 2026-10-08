// 测试用的 WXML 小渲染器：按页面数据算出 wx:if / wx:elif / wx:else、wx:for 和 {{ }}，
// 得到实际会显示的节点树，用来检查"某种状态下页面上有没有这个入口、这段文字"。
// 只支持本项目模板用到的写法（没有 wxs、template、import）；自定义组件默认不展开，
// 传 components（标签名 → wxml 文件）时按组件自己的模板展开。不是测试文件，node --test 不会单独跑它。
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const VOID = new Set(['input', 'image', 'icon', 'progress', 'checkbox', 'radio', 'switch', 'slider']);

function parse(src) {
  const text = src.replace(/<!--[\s\S]*?-->/g, '');
  const root = { tag: '#root', attrs: {}, children: [] };
  const stack = [root];
  // 下一个标签的开头：文字里 {{ }} 中的 < 不算（如 {{due.days < 0 ? … }}）
  const nextTag = (from) => {
    let k = from;
    while (k < text.length) {
      if (text.startsWith('{{', k)) { const end = text.indexOf('}}', k + 2); k = end < 0 ? text.length : end + 2; continue; }
      if (text[k] === '<') return k;
      k += 1;
    }
    return -1;
  };
  let i = 0;
  while (i < text.length) {
    const lt = nextTag(i);
    if (lt < 0) { stack.at(-1).children.push({ text: text.slice(i) }); break; }
    if (lt > i) stack.at(-1).children.push({ text: text.slice(i, lt) });
    if (text[lt + 1] === '/') {
      const gt = text.indexOf('>', lt);
      const name = text.slice(lt + 2, gt).trim();
      const open = stack.pop();
      if (!open || open.tag !== name) throw new Error(`WXML 标签没有配对：</${name}>，当前是 <${open && open.tag}>`);
      i = gt + 1;
      continue;
    }
    // 开始标签：引号里的 > 不算结束
    let j = lt + 1;
    let quote = null;
    while (j < text.length) {
      const c = text[j];
      if (quote) { if (c === quote) quote = null; } else if (c === '"' || c === "'") quote = c; else if (c === '>') break;
      j += 1;
    }
    let inner = text.slice(lt + 1, j);
    const selfClosing = inner.endsWith('/');
    if (selfClosing) inner = inner.slice(0, -1);
    const name = /^[\w-]+/.exec(inner)[0];
    const attrs = {};
    const re = /([\w:.-]+)(?:\s*=\s*("([^"]*)"|'([^']*)'))?/g;
    re.lastIndex = name.length;
    let m;
    while ((m = re.exec(inner))) attrs[m[1]] = m[2] === undefined ? true : (m[3] !== undefined ? m[3] : m[4]);
    const node = { tag: name, attrs, children: [] };
    stack.at(-1).children.push(node);
    if (!selfClosing && !VOID.has(name)) stack.push(node);
    i = j + 1;
  }
  if (stack.length !== 1) throw new Error(`WXML 标签没有闭合：<${stack.at(-1).tag}>`);
  return root;
}

// 没定义的名字和取不到的属性都当 undefined（和 WXML 一样不报错）
function evaluate(expr, scope) {
  const proxy = new Proxy(scope, {
    has: () => true,
    get: (target, key) => (key === Symbol.unscopables ? undefined : target[key]),
  });
  try {
    // eslint-disable-next-line no-new-func
    return new Function('scope', `with (scope) { return (${expr}); }`)(proxy);
  } catch (e) {
    if (e instanceof SyntaxError) throw new Error(`WXML 表达式写法不支持：${expr}`);
    return undefined;
  }
}

function interpolate(value, scope) {
  if (typeof value !== 'string') return value;
  const whole = /^\s*\{\{([\s\S]*?)\}\}\s*$/.exec(value);
  if (whole && !whole[1].includes('}}')) return evaluate(whole[1], scope);
  return value.replace(/\{\{([\s\S]*?)\}\}/g, (_, expr) => {
    const v = evaluate(expr, scope);
    return v === undefined || v === null ? '' : String(v);
  });
}

function renderChildren(children, scope, options) {
  const out = [];
  let chain = null; // null：前面没有 wx:if；true：已经有一支显示了；false：前面都没显示
  for (const child of children) {
    if (child.text !== undefined) {
      const t = interpolate(child.text, scope);
      if (String(t).trim()) chain = null;
      out.push({ text: String(t) });
      continue;
    }
    const a = child.attrs;
    if (a['wx:for'] !== undefined) {
      chain = null;
      const list = interpolate(a['wx:for'], scope) || [];
      const itemName = a['wx:for-item'] || 'item';
      const indexName = a['wx:for-index'] || 'index';
      const entries = Array.isArray(list) ? list.map((v, k) => [k, v]) : Object.entries(list);
      for (const [k, v] of entries) {
        const inner = Object.assign(Object.create(null), scope, { [itemName]: v, [indexName]: k });
        if (a['wx:if'] !== undefined && !interpolate(a['wx:if'], inner)) continue;
        out.push(...renderNode(child, inner, options));
      }
      continue;
    }
    let show;
    if (a['wx:if'] !== undefined) {
      show = !!interpolate(a['wx:if'], scope);
      chain = show;
    } else if (a['wx:elif'] !== undefined) {
      if (chain === null) throw new Error('wx:elif 前面没有 wx:if');
      show = !chain && !!interpolate(a['wx:elif'], scope);
      chain = chain || show;
    } else if (a['wx:else'] !== undefined) {
      if (chain === null) throw new Error('wx:else 前面没有 wx:if');
      show = !chain;
      chain = null;
    } else {
      show = true;
      chain = null;
    }
    if (show) out.push(...renderNode(child, scope, options));
  }
  return out;
}

function renderNode(node, scope, options) {
  const children = renderChildren(node.children, scope, options);
  if (node.tag === 'block') return children;
  const attrs = {};
  for (const [k, v] of Object.entries(node.attrs)) {
    if (!k.startsWith('wx:')) attrs[k] = interpolate(v, scope);
  }
  const file = options.components && options.components[node.tag];
  if (file) return [{ tag: node.tag, attrs, children: renderChildren(parse(read(file)).children, Object.create(null), options) }];
  return [{ tag: node.tag, attrs, children }];
}

function read(file) {
  return fs.readFileSync(path.join(ROOT, file), 'utf8');
}

// 渲染一个 wxml 文件：data 是页面数据；返回根节点（tag 为 #root）
function render(file, data, options = {}) {
  const scope = Object.assign(Object.create(null), data);
  return { tag: '#root', attrs: {}, children: renderChildren(parse(read(file)).children, scope, options) };
}

// 显示出来的全部文字（相邻节点直接连起来，空白压成一个空格）
function textOf(node) {
  if (node.text !== undefined) return node.text;
  return node.children.map(textOf).join('').replace(/\s+/g, ' ');
}

function findAll(node, pred, found = []) {
  if (node.text === undefined) {
    if (node.tag !== '#root' && pred(node)) found.push(node);
    node.children.forEach((c) => findAll(c, pred, found));
  }
  return found;
}

const hasClass = (name) => (node) => String(node.attrs.class || '').split(/\s+/).includes(name);
const tapsTo = (handler) => (node) => node.attrs.bindtap === handler || node.attrs.catchtap === handler;

module.exports = { parse, render, textOf, findAll, hasClass, tapsTo };
