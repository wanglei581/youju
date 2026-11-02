// 只读：核对云托管某个服务当前在跑的版本里，AI 和协议相关的几项环境变量（创始人在控制台改完后用）。
// 只打印下面白名单里的非密钥项（值用 JSON 写法打印，前后空格看得出来）；密钥只打印"填了没有、多少个字、有没有空格"，
// 绝不打印密钥内容；其他环境变量只数个数，不打印名字和值。用本机已登录的 wxcloud CLI 里的官方接口代理。
// 用法：node tools/team/cloud-env-check.cjs <环境ID> <admin|api> [版本名] [--expect deepseek]
const P = process.env.WXCLOUD_CLI_LIB || require('path').join(require('os').homedir(), '.local/lib/node_modules/@wxcloud/cli/lib');
const api = require(P + '/api');
const common = require(P + '/api/common');

const args = process.argv.slice(2);
const expectIdx = args.indexOf('--expect');
const profile = expectIdx >= 0 ? args[expectIdx + 1] : '';
const [envId, service, versionArg] = args.filter((a, i) => !(expectIdx >= 0 && (i === expectIdx || i === expectIdx + 1)));

const SHOWN = [
  'YOUJU_ROLE', 'YOUJU_AGREEMENT_VERSION', 'YOUJU_AGREEMENT_MIN_VERSION',
  'YOUJU_AI_ADVISOR_ENABLED', 'YOUJU_AI_ADVISOR_AUDIENCE',
  'YOUJU_AI_ADVISOR_BASE_URL', 'YOUJU_AI_ADVISOR_MODEL', 'YOUJU_AI_ADVISOR_REQUEST_EXTRA', 'YOUJU_AI_ADVISOR_STREAM',
  'YOUJU_AI_MODEL_LABEL', 'YOUJU_AI_MODEL_FILING',
  'YOUJU_AI_BASE_URL', 'YOUJU_AI_MODEL', 'YOUJU_AI_REQUEST_EXTRA',
];
const SECRET = ['YOUJU_AI_ADVISOR_API_KEY', 'YOUJU_AI_API_KEY'];

// 1.0 改用 DeepSeek（D-114）要填的值：模型和后台核对同一个（deepseek-v4-pro，官网价格页 11/3 只列 deepseek-flash 和它，
// 默认开思考模式，附加参数关掉）；ADVISOR_* 只在 admin（api 不调模型，config.py 不要求）
const EXPECT = {
  deepseek: {
    both: { YOUJU_AI_MODEL_LABEL: '求索对话DeepSeek Chat', YOUJU_AI_MODEL_FILING: 'Beijing-DeepseekChat-202404280016' },
    admin: { YOUJU_AI_ADVISOR_BASE_URL: 'https://api.deepseek.com', YOUJU_AI_ADVISOR_MODEL: 'deepseek-v4-pro', YOUJU_AI_ADVISOR_REQUEST_EXTRA: '{"thinking":{"type":"disabled"}}' },
    adminSecret: ['YOUJU_AI_ADVISOR_API_KEY'],
  },
};

(async () => {
  if (!envId || !service) throw new Error('用法：node tools/team/cloud-env-check.cjs <环境ID> <admin|api> [版本名] [--expect deepseek]');
  if (profile && !EXPECT[profile]) throw new Error(`不认识的 --expect：${profile}`);
  common.setApiCommonParameters({});
  let version = versionArg;
  if (!version) {
    const list = await api.DescribeCloudBaseRunServer({ EnvId: envId, ServerName: service, Limit: 100, Offset: 0 });
    const live = (list.VersionItems || []).filter((v) => Number(v.FlowRatio) > 0);
    if (!live.length) throw new Error('没有在接流量的版本');
    version = live[live.length - 1].VersionName;
  }
  const ver = await api.DescribeCloudBaseRunServerVersion({ EnvId: envId, ServerName: service, VersionName: version });
  let env = {};
  try { env = JSON.parse(ver.EnvParams || '{}'); } catch { throw new Error('EnvParams 不是 JSON'); }
  const lines = [`${service} ${version}（${ver.Status || ''}，备注：${ver.Remark || '无'}）：环境变量共 ${Object.keys(env).length} 项`];
  const spaced = (v) => v !== v.trim();
  for (const k of SHOWN) {
    if (!(k in env)) { lines.push(`  ${k}：没设`); continue; }
    const v = String(env[k]);
    lines.push(`  ${k} = ${JSON.stringify(v)}${spaced(v) ? '  ← 前后有空格' : ''}`);
  }
  for (const k of SECRET) {
    if (!(k in env)) { lines.push(`  ${k}：没设`); continue; }
    const v = String(env[k]);
    lines.push(`  ${k}：填了，${v.length} 个字${/\s/.test(v) ? '，里面有空白字符 ← 要去掉' : '，没有空格'}（不显示内容）`);
  }
  if (profile) {
    const exp = EXPECT[profile];
    const want = { ...exp.both, ...(service === 'admin' ? exp.admin : {}) };
    const bad = [];
    for (const [k, v] of Object.entries(want)) {
      if (env[k] !== v) bad.push(`${k} 应为 ${JSON.stringify(v)}，现在是 ${k in env ? JSON.stringify(String(env[k])) : '没设'}`);
    }
    if (service === 'admin') {
      for (const k of exp.adminSecret) {
        const v = env[k];
        if (!v) bad.push(`${k} 没填`);
        else if (/\s/.test(String(v))) bad.push(`${k} 里面有空白字符`);
        // 只看长度和公开的前缀格式，不打印内容：云开发的密钥是 800 多字的长串，DeepSeek 的是 sk- 开头、几十个字
        else if (String(v).length > 100 || !String(v).startsWith('sk-')) bad.push(`${k} 不像 DeepSeek 的密钥（${String(v).length} 个字；DeepSeek 的是 sk- 开头、几十个字），可能还是原来云开发那把`);
      }
    }
    lines.push(bad.length ? `核对 ${profile}：有 ${bad.length} 处不对` : `核对 ${profile}：都对`);
    for (const b of bad) lines.push(`  ✗ ${b}`);
  }
  console.log(lines.join('\n'));
})().catch((e) => { console.error('失败：', e && e.message ? e.message.slice(0, 200) : String(e).slice(0, 200)); process.exit(1); });
