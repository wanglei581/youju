// 运行配置。
// - cloudEnv / cloudService：微信云托管环境和服务名（#32 部署后填写），正式版走这里；
// - devBaseUrl：只在开发者工具里连本机后端（uv run youju serve）时填写，例如 http://127.0.0.1:8000，提交前清空；
// - 三者都为空时用 mock/data.js 的演示数据。
module.exports = {
  cloudEnv: 'prod-d5getqftefb61fc92',
  cloudService: 'api',
  devBaseUrl: '',
  // 一次性订阅消息模板 ID：newDocs 关注条件有新文件；deadline 申报截止前提醒（#22 申请后填写）
  templates: { newDocs: '', deadline: '' },
  // AI 内测包（D-062）：true 时协议还是草案也开放登录和同意，AI 页面标题和顶部标注写"内部测试"，给白名单成员试用；
  // AI 正式版（D-066，上线即对所有用户开放）改成 false：登录和同意只看 legal.final，标注只写"AI 生成"。
  // 10/18 完整验收后改成 false，主分支就是提审用的完整正式包（提审检查用 --profile complete-release）；
  // AI 对谁开放仍由服务端开关决定（白名单或所有人），和这里无关
  aiInternal: false,
};
