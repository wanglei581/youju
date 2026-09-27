// 运行配置。云托管环境（#32）和订阅消息模板（#22）开通后填写；为空时走演示数据、不发起订阅。
module.exports = {
  cloudEnv: '',
  cloudService: '',
  // 一次性订阅消息模板 ID：newDocs 关注条件有新文件；deadline 申报截止前提醒
  templates: { newDocs: '', deadline: '' },
};
