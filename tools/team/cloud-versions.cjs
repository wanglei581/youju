// 只读：调用 wxcloud CLI 自带的 DescribeCloudBaseRunServer（和 wxcloud version:list 同一个接口），
// 只打印版本名、状态、流量、实例相关字段和备注，不读、不打印环境变量或其他配置。
// 用本机已登录的 wxcloud CLI（npm 全局包 @wxcloud/cli）里的官方接口代理；路径可用 WXCLOUD_CLI_LIB 覆盖
const P = process.env.WXCLOUD_CLI_LIB || require('path').join(require('os').homedir(), '.local/lib/node_modules/@wxcloud/cli/lib');
const api = require(P + '/api');
const common = require(P + '/api/common');
const [envId, service, mode] = process.argv.slice(2);
(async () => {
  common.setApiCommonParameters({});
  const res = await api.DescribeCloudBaseRunServer({ EnvId: envId, ServerName: service, Limit: 100, Offset: 0 });
  const items = res.VersionItems || [];
  if (mode === 'keys') { console.log(JSON.stringify(Object.keys(items[0] || {}))); return; }
  const SAFE = /^(VersionName|Status|FlowRatio|Percent|Remark|CreatedTime|UpdatedTime|MinNum|MaxNum|MinReplicas|MaxReplicas|CurrentReplicas|Replicas|RunningReplicas|InstanceNum|BuildId|UploadType|Architecture|IsDefaultPriority|Priority)$/;
  const out = items.slice(-6).map((v) => Object.fromEntries(Object.entries(v).filter(([k]) => SAFE.test(k))));
  console.log(JSON.stringify({ service, checkedAt: new Date().toISOString(), versions: out }, null, 1));
})().catch((e) => { console.error('failed', e && e.message ? e.message.slice(0, 200) : String(e).slice(0, 200)); process.exit(1); });
