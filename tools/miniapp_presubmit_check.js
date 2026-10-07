#!/usr/bin/env node
// 小程序提审前自检（验收 E5；agy 预审提出"开发版、演示数据这类提示不能留到审核版"）。
// 这些提示只在配置没填时出现，所以逐项查配置：全部通过才提审。
// 协议没定稿（legal.final 为 false）时提审的是不含登录的版本（D-056）：登录、关注、提醒、纠错的入口都不显示，
// 不查订阅模板；协议阅读页仍可打开，所以【】占位照样要补齐。
// 只报哪一项没过，不打印协议正文和运营者信息。
//
// 用法（仓库根目录）：node tools/miniapp_presubmit_check.js
// 放在小程序目录外面，不会被打进小程序代码包。

const path = require('path');
const fs = require('fs');

const root = path.join(__dirname, '..', 'apps', 'miniapp');
const legal = require(path.join(root, 'content', 'legal.js'));
const config = require(path.join(root, 'utils', 'config.js'));
const app = JSON.parse(fs.readFileSync(path.join(root, 'app.json'), 'utf8'));

const results = [];
const check = (name, ok, fix) => results.push({ name, ok: Boolean(ok), fix });

// 协议里还没填的占位：【待确认…】【联系邮箱】【待创始人指定】等，都是【开头
function placeholders(value, where, found) {
  if (typeof value === 'string') {
    const count = (value.match(/【/g) || []).length;
    if (count) found.push(`${where}（${count} 处）`);
  } else if (Array.isArray(value)) {
    value.forEach((item, i) => placeholders(item, `${where}[${i}]`, found));
  } else if (value && typeof value === 'object') {
    Object.keys(value).forEach((key) => placeholders(value[key], where ? `${where}.${key}` : key, found));
  }
  return found;
}

const withLogin = legal.final === true;
console.log(withLogin
  ? '协议已定稿：本次提审含登录、关注、提醒、纠错\n'
  : '协议未定稿：本次提审不含登录、关注、提醒、纠错（D-056），这些入口都不显示\n');
const marks = placeholders(legal, '', []);
check('协议里没有未填的【】占位', marks.length === 0,
  `还有：${marks.slice(0, 8).join('、')}${marks.length > 8 ? ' 等' : ''}`);
const op = legal.operator || {};
check('关于页有备案号', op.icp && !String(op.icp).startsWith('【'), '备案号下来后填进 content/legal.js 的 operator.icp');
check('关于页有联系邮箱', op.email && !String(op.email).startsWith('【'), '填进 content/legal.js 的 operator.email');
check('接了云托管（cloudEnv、cloudService）', config.cloudEnv && config.cloudService,
  '填 utils/config.js 的 cloudEnv、cloudService；都空时整个小程序用演示数据');
check('没有连本机开发后端（devBaseUrl 为空）', !config.devBaseUrl, '提交前清空 utils/config.js 的 devBaseUrl');
const templates = config.templates || {};
if (withLogin) {
  check('订阅消息模板已填（newDocs、deadline）', templates.newDocs && templates.deadline,
    '模板申请下来后填 utils/config.js 的 templates；否则点订阅会提示"订阅消息还没开通"');
}
check('app.json 没开调试（debug）', app.debug !== true, '去掉 app.json 里的 "debug": true');

let failed = 0;
for (const r of results) {
  if (!r.ok) failed += 1;
  console.log(`${r.ok ? '通过' : '没过'}  ${r.name}${r.ok ? '' : `\n      → ${r.fix}`}`);
}
console.log(failed ? `\n${failed} 项没过，先改好再提审` : '\n全部通过');
process.exit(failed ? 1 : 0);
