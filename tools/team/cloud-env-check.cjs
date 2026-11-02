// 只读：核对云托管某个服务当前在跑的版本里，AI 和协议相关的几项环境变量（创始人在控制台改完后用）。
// 只打印下面白名单里的非密钥项（值用 JSON 写法打印，前后空格看得出来）；密钥只报"填了""没填"，不报长度、格式，
// 读到就丢掉（团队章程：密钥只由创始人配，团队工具不碰密钥内容）。换没换成 DeepSeek 的密钥，用端到端核对：
// 白名单账号试一单，看日志里的模型名和结果页的公示。其他环境变量只数个数，不打印名字和值。
// 用本机已登录的 wxcloud CLI 里的官方接口代理。
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
  'YOUJU_AI_BASE_URL', 'YOUJU_AI_MODEL', 'YOUJU_AI_BACKGROUND_MODEL', 'YOUJU_AI_REQUEST_EXTRA',
];
const SECRET = ['YOUJU_AI_ADVISOR_API_KEY', 'YOUJU_AI_API_KEY'];

// 1.0 改用 DeepSeek（D-114）要填的值：模型和后台核对同一个（deepseek-v4-pro，官网价格页 11/3 只列 deepseek-flash 和它，
// 默认开思考模式，附加参数关掉）；ADVISOR_* 只在 admin（api 不调模型，config.py 不要求）
const EXPECT = {
  deepseek: {
    both: { YOUJU_AI_MODEL_LABEL: '求索对话DeepSeek Chat', YOUJU_AI_MODEL_FILING: 'Beijing-DeepseekChat-202404280016' },
    admin: { YOUJU_AI_ADVISOR_BASE_URL: 'https://api.deepseek.com', YOUJU_AI_ADVISOR_MODEL: 'deepseek-v4-pro', YOUJU_AI_ADVISOR_REQUEST_EXTRA: '{"thinking":{"type":"disabled"}}' },
    adminSecret: ['YOUJU_AI_ADVISOR_API_KEY'],  // 只看填没填；是不是 DeepSeek 的密钥用端到端核对
  },
};

(async () => {
  if (!envId || !service) throw new Error('用法：node tools/team/cloud-env-check.cjs <环境ID> <admin|api> [版本名] [--expect deepseek]');
  if (profile && !EXPECT[profile]) throw new Error(`不认识的 --expect：${profile}`);
  common.setApiCommonParameters({});
  let version = versionArg;
  if (!version) {
    const list = await api.DescribeCloudBaseRunServer({ EnvId: envId, ServerName: service, Limit: 100, Offset: 0 });
    // 接口返回的顺序不保证：取流量最大的，一样大取创建时间最新的（灰度时两个版本都有流量）
    const live = (list.VersionItems || []).filter((v) => Number(v.FlowRatio) > 0);
    if (!live.length) throw new Error('没有在接流量的版本');
    live.sort((a, b) => Number(b.FlowRatio) - Number(a.FlowRatio) || String(b.CreatedTime || '').localeCompare(String(a.CreatedTime || '')));
    version = live[0].VersionName;
    if (live.length > 1) console.log(`注意：${live.length} 个版本在接流量（${live.map((v) => `${v.VersionName} ${v.FlowRatio}%`).join('、')}），看的是 ${version}`);
  }
  const ver = await api.DescribeCloudBaseRunServerVersion({ EnvId: envId, ServerName: service, VersionName: version });
  let env = {};
  try { env = JSON.parse(ver.EnvParams || '{}'); } catch { throw new Error('EnvParams 不是 JSON'); }
  delete ver.EnvParams;  // 原始字符串里也有密钥，解析完就丢掉
  // 不是普通对象（比如被双重编码成字符串）时不往下走：后面的 in 运算报错会把整串配置带进报错信息
  if (!env || typeof env !== 'object' || Array.isArray(env)) { env = null; throw new Error('EnvParams 不是对象'); }
  // 白名单项要是贴进了密钥（或 sk- 开头），只写"像密钥"、不打印；比完就把密钥值丢掉
  // 比之前先去掉空白和引号（控制台里存过前后带空格、换行或引号的值）；值里只要有 sk- 就当密钥
  const norm = (v) => String(v).replace(/[\s"'“”‘’`]/g, '');
  const secretValues = SECRET.map((k) => env[k]).filter((v) => typeof v === 'string').map(norm).filter((v) => v.length >= 8);
  const looksSecret = (v) => /sk-/i.test(v) || secretValues.some((sv) => norm(v).includes(sv));
  const secretLike = new Set(SHOWN.filter((k) => k in env && looksSecret(String(env[k]))));
  secretValues.length = 0;
  const lines = [`${service} ${version}（${ver.Status || ''}，备注：${ver.Remark || '无'}）：环境变量共 ${Object.keys(env).length} 项`];
  const spaced = (v) => v !== v.trim();
  for (const k of SHOWN) {
    if (!(k in env)) { lines.push(`  ${k}：没设`); continue; }
    if (secretLike.has(k)) { lines.push(`  ${k}：值像密钥，不显示 ← 可能贴错了地方`); continue; }
    const v = String(env[k]);
    lines.push(`  ${k} = ${JSON.stringify(v)}${spaced(v) ? '  ← 前后有空格' : ''}`);
  }
  for (const k of SECRET) {
    const filled = Boolean(env[k]);
    delete env[k];  // 只留"填没填"，值不留在内存里继续用
    lines.push(`  ${k}：${filled ? '填了' : '没填'}（密钥不看内容、长度和格式）`);
    env[`__filled__${k}`] = filled;
  }
  if (profile) {
    const exp = EXPECT[profile];
    const want = { ...exp.both, ...(service === 'admin' ? exp.admin : {}) };
    const bad = [];
    // 值是 JSON 的（附加参数）按内容比：官方写法带空格（{"thinking": {"type": "disabled"}}）也算对
    const same = (actual, expected) => {
      if (actual === expected) return true;
      if (typeof actual !== 'string' || !expected.startsWith('{')) return false;
      try { return JSON.stringify(JSON.parse(actual)) === JSON.stringify(JSON.parse(expected)); } catch { return false; }
    };
    for (const [k, v] of Object.entries(want)) {
      if (secretLike.has(k)) { bad.push(`${k} 的值像密钥，可能贴错了地方`); continue; }
      if (!same(env[k], v)) bad.push(`${k} 应为 ${JSON.stringify(v)}，现在是 ${k in env ? JSON.stringify(String(env[k])) : '没设'}`);
    }
    if (service === 'admin') {
      for (const k of exp.adminSecret) {
        if (!env[`__filled__${k}`]) bad.push(`${k} 没填`);
      }
    }
    lines.push(bad.length ? `核对 ${profile}：有 ${bad.length} 处不对` : `核对 ${profile}：都对`);
    for (const b of bad) lines.push(`  ✗ ${b}`);
  }
  console.log(lines.join('\n'));
})().catch((e) => {
  // 报错信息里可能带配置内容（比如某个值被当成代码解析）：太长或像配置的不打印
  const msg = e && e.message ? String(e.message) : String(e);
  const safe = msg.length <= 120 && !/YOUJU_|sk-|\{/.test(msg) ? msg : '出错了（报错信息里可能有配置内容，不打印）';
  console.error('失败：', safe);
  process.exit(1);
});
