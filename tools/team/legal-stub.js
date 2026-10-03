// 去敏占位：真实的用户协议、隐私保护指引和运营主体信息不提供给外部模型。不要修改本文件。
// 用法：给 Codex 的开发 worktree 或审阅副本里，用它替换 apps/miniapp/content/legal.js
// （worktree 里先 git update-index --skip-worktree apps/miniapp/content/legal.js，用完恢复）。
module.exports = {
  version: '0',
  final: false,
  status: '草稿',
  operator: { name: '【运营主体】', address: '【地址】', email: '', icp: '' },
  user: { title: '用户协议', sections: [] },
  privacy: { title: '隐私保护指引', sections: [] },
  disclaimer: '第三方整理，非政府平台。'
};
