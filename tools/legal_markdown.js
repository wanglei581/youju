#!/usr/bin/env node
// 把小程序的协议文件（content/legal*.js）转成可读的 Markdown：用户协议、隐私保护指引、免责声明三份全文，
// 加粗段落和要点照小程序里的样子标出来。给创始人过目、给审阅的人看都用它，正文以协议文件为准。
// --sanitize：运营主体的名称、地址、联系邮箱、备案号换成占位（发给外部模型审阅前用，CLAUDE.md 的规矩）。
// 用法：node tools/legal_markdown.js <协议文件> [--sanitize] [--out <文件>]
const fs = require('node:fs');
const path = require('node:path');

const PLACEHOLDERS = { name: '【运营主体】', address: '【地址】', email: '【联系邮箱】', icp: '【备案号】' };

// operator 里每个非空字段都换成占位（不认识的字段换成【运营信息】）：先换长的（名称是地址的一部分时，
// 地址也能整段换掉），不分大小写；换完再查一遍，还有原值就报错，不给出半脱敏的结果
function sanitize(text, operator) {
  const values = Object.entries(operator || {})
    .filter(([, v]) => typeof v === 'string' && v.trim())
    .map(([k, v]) => [v.trim(), PLACEHOLDERS[k] || '【运营信息】'])
    .sort((a, b) => b[0].length - a[0].length);
  let out = text;
  for (const [value, mark] of values) {
    out = out.replace(new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), mark);
  }
  const left = values.filter(([value]) => out.toLowerCase().includes(value.toLowerCase()));
  if (left.length) throw new Error('脱敏后还有运营主体信息，没有输出');
  return out;
}

function render(legal, { clean = false } = {}) {
  const lines = [];
  const doc = (d) => {
    lines.push(`## ${d.title}`, '');
    if (Array.isArray(d.keyPoints) && d.keyPoints.length) {
      lines.push('**要点：**', '');
      for (const k of d.keyPoints) lines.push(`- ${k}`);
      lines.push('');
    }
    const keys = Array.isArray(d.keyParagraphs) ? d.keyParagraphs : [];
    for (const s of d.sections) {
      lines.push(`### ${s.h}`, '');
      for (const p of s.p) lines.push(keys.some((k) => p.startsWith(k)) ? `**${p}**` : p, '');
    }
  };
  lines.push(`# 协议全文：版本 ${legal.version}（${legal.final === true ? '已定稿' : '草稿'}）`, '');
  if (legal.status) lines.push(`状态：${legal.status}`, '');
  doc(legal.user);
  doc(legal.privacy);
  lines.push('## 免责声明', '', legal.disclaimer, '');
  const text = lines.join('\n');
  return clean ? sanitize(text, legal.operator) : text;
}

function main(args) {
  const file = args.find((a) => !a.startsWith('--') && args[args.indexOf(a) - 1] !== '--out');
  if (!file) throw new Error('用法：node tools/legal_markdown.js <协议文件> [--sanitize] [--out <文件>]');
  const legal = require(path.resolve(file));
  const text = render(legal, { clean: args.includes('--sanitize') });
  const out = args.includes('--out') ? args[args.indexOf('--out') + 1] : null;
  if (out) fs.writeFileSync(out, text);
  else process.stdout.write(text);
}

if (require.main === module) {
  try { main(process.argv.slice(2)); } catch (err) { console.error(`没转成：${err.message}`); process.exitCode = 1; }
}

module.exports = { render, sanitize };
