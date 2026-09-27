// 运行配置。
// - cloudEnv / cloudService：微信云托管环境和服务名（#32 部署后填写），正式版走这里；
// - devBaseUrl：只在开发者工具里连本机后端（uv run youju serve）时填写，例如 http://127.0.0.1:8000，提交前清空；
// - 三者都为空时用 mock/data.js 的演示数据。
module.exports = {
  cloudEnv: '',
  cloudService: '',
  devBaseUrl: '',
  // 一次性订阅消息模板 ID：newDocs 关注条件有新文件；deadline 申报截止前提醒（#22 申请后填写）
  templates: { newDocs: '', deadline: '' },
};
