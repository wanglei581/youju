const assert = require('node:assert/strict');
const vm = require('node:vm');

// 仅解释本次页面用到的条件、循环与文本插值，断言实际可见文字而非源文件关键字。
function parse(source) {
  source = source.replace(/<!--[^]*?-->/g, '');
  const root = { children: [] }; const stack = [root];
  const tags = /<\/?[\w-]+\b(?:"[^"]*"|'[^']*'|[^'">])*>/g;
  let start = 0;
  for (const match of source.matchAll(tags)) {
    const parent = stack.at(-1);
    if (match.index > start) parent.children.push({ text: source.slice(start, match.index) });
    const raw = match[0];
    if (raw.startsWith('</')) stack.pop();
    else {
      const node = { tag: /^<([\w-]+)/.exec(raw)[1], attrs: {}, children: [] };
      for (const attr of raw.matchAll(/([\w:-]+)="([^"]*)"/g)) node.attrs[attr[1]] = attr[2];
      if (/\bwx:else\b/.test(raw)) node.attrs['wx:else'] = '';
      parent.children.push(node);
      if (!raw.endsWith('/>')) stack.push(node);
    }
    start = match.index + raw.length;
  }
  if (start < source.length) root.children.push({ text: source.slice(start) });
  assert.equal(stack.length, 1, 'WXML 标签闭合');
  return root;
}
const evaluate = (value, data) => vm.runInNewContext(value.replace(/^\{\{|\}\}$/g, ''), data);
function render(source, data) {
  function children(nodes, scope) {
    let matched = false; let output = '';
    for (const node of nodes) {
      if (node.text !== undefined) {
        output += node.text.replace(/\{\{([^]*?)\}\}/g, (_, expression) => evaluate(expression, scope) ?? '');
        continue;
      }
      const attrs = node.attrs;
      if (attrs['wx:if'] !== undefined) { matched = !!evaluate(attrs['wx:if'], scope); if (!matched) continue; }
      else if (attrs['wx:elif'] !== undefined) { if (matched) continue; matched = !!evaluate(attrs['wx:elif'], scope); if (!matched) continue; }
      else if (attrs['wx:else'] !== undefined) { if (matched) continue; matched = true; }
      else matched = false;
      if (attrs['wx:for']) {
        const values = evaluate(attrs['wx:for'], scope) || [];
        output += values.map((item, index) => children(node.children, { ...scope, [attrs['wx:for-item'] || 'item']: item, [attrs['wx:for-index'] || 'index']: index })).join('');
      } else output += children(node.children, scope);
    }
    return output;
  }
  return children(parse(source).children, data).replace(/\s+/g, ' ').trim();
}
function assertAiGuarded(source) {
  function visit(node, guarded) {
    guarded = guarded || !!(node.attrs && /\baiEnabled\b/.test(node.attrs['wx:if'] || ''));
    if (node.text && /\bAI\b/.test(node.text)) assert.ok(guarded, 'AI 文案必须在 aiEnabled 控制的 wx:if 节点里');
    if (node.attrs && Object.values(node.attrs).some((value) => /\bAI\b/.test(value))) assert.ok(guarded);
    (node.children || []).forEach((child) => visit(child, guarded));
  }
  visit(parse(source), false);
}

module.exports = { parse, render, assertAiGuarded };
