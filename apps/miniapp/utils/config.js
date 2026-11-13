// 运行配置。
// - cloudEnv / cloudService：微信云托管环境和服务名（#32 部署后填写），正式版走这里；
// - apiBaseUrl：2.0 自建服务器的 https 地址，填了优先走它（见下面）；
// - devBaseUrl：只在开发者工具里连本机后端（uv run youju serve）时填写，例如 http://127.0.0.1:8000，提交前清空；
// - 三者都为空时用 mock/data.js 的演示数据。
module.exports = {
  cloudEnv: 'prod-d5getqftefb61fc92',
  cloudService: 'api',
  // 2.0 自建服务器的接口地址（https://已备案的域名，不带结尾的 /）。填了就不走云托管：wx.request 直连这个域名、
  // 用自己的登录（utils/api.js 的 ensureSession），这个域名要先加进小程序后台"开发管理 → 服务器域名"的 request 合法域名。
  // 切换前一直留空（docs/部署/自建服务器.md）
  apiBaseUrl: '',
  devBaseUrl: '',
  // 一次性订阅消息模板 ID：newDocs 关注条件有新文件；deadline 申报截止前提醒（#22 申请后填写）
  templates: { newDocs: '', deadline: '' },
  // 公众号（公众号方案 PR #287）：username 填公众号的原始 ID（gh_ 开头，公众号后台"设置与开发 → 公众号设置"里），
  // name 是公众号名称（10/18 创始人定：和小程序同名）。username 没填时，截止页、我的、关于的"关注公众号"
  // 和首页的扫码关注条都不显示
  officialAccount: { username: '', name: '有据申报与行业助手' },
  // AI 内测包（D-062）：true 时协议还是草案也开放登录和同意，AI 页面标题和顶部标注写"内部测试"，给白名单成员试用；
  // AI 正式版（D-066，上线即对所有用户开放）改成 false：登录和同意只看 legal.final，标注只写"AI 生成"。
  // 10/18 完整验收后改成 false，主分支就是提审用的完整正式包（提审检查用 --profile complete-release）；
  // AI 对谁开放仍由服务端开关决定（白名单或所有人），和这里无关
  aiInternal: false,
};
