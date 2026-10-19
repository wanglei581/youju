// 只读：经 wxcloud CLI 的官方接口代理调用 tcbr SearchClsLog（2022-02-17），按时间窗和关键词读运行日志。
// 用法：node tools/team/cloud-logs.cjs --out <结果文件> <envId> "<开始 YYYY-MM-DD HH:MM:SS>" "<结束>" "<查询串>" [条数]
//       python3 tools/team/cloud_logs_show.py <结果文件> [中文关键词…]
// 查到的日志原文只写进 <结果文件>，终端只打印条数、页数和文件路径。原文里有用户的来源 IP 和用户编号，
// 读日志的是 AI 成员，打印出来的内容会发到境外的模型服务（个人信息保护影响评估第六节（二）第 6 条、
// （三）第 7 条）。所以：结果文件放在调用方自己的临时目录（放进仓库会被拒绝，免得提交上去），文件只有本人可读；
// 不要直接打开、cat 或贴出来，一律用 cloud_logs_show.py 看（它显示前遮掉 IP、用户编号和文件编号）。
// 要看完整的 IP、按某个用户查，由创始人在云托管控制台的日志页自己查；也不要拿 IP 或用户编号当查询串。
// 出错时结果文件里写的是出错信息（终端只打印出错类别），同样用 cloud_logs_show.py 看。
// 查询串只认英文分词（如 cache-attachments、batchdeletefile）；中文关键词搜不到，要用 "*" 取整段时间窗，
// 再交给 cloud_logs_show.py 在本机按中文过滤。时间是北京时间；一次最多取 20 页（每页 [条数] 行）。
// 用本机已登录的 wxcloud CLI（npm 全局包 @wxcloud/cli）里的官方接口代理；路径可用 WXCLOUD_CLI_LIB 覆盖
const fs = require('fs');
const path = require('path');

const USAGE = '用法：node tools/team/cloud-logs.cjs --out <结果文件> <envId> "<开始 YYYY-MM-DD HH:MM:SS>" "<结束>" "<查询串>" [条数]';
const args = [];
let out;
for (let i = 2; i < process.argv.length; i++) {
  const a = process.argv[i];
  if (a === '--out') out = process.argv[++i];
  else if (a.startsWith('--out=')) out = a.slice(6);
  else args.push(a);
}
const [envId, start, end, query, limit = '100'] = args;
if (!out || !envId || !start || !end || !query || args.length > 5) {
  console.log(USAGE);
  console.log('结果只写进 --out 指定的文件（放在自己的临时目录），终端不打印日志原文；看结果用 python3 tools/team/cloud_logs_show.py <结果文件>');
  process.exit(2);
}
let dir;
try {
  dir = fs.realpathSync(path.dirname(path.resolve(out)));
} catch (e) {
  console.log(`结果文件所在的目录不存在：${path.dirname(path.resolve(out))}`);
  process.exit(2);
}
const repo = fs.realpathSync(path.resolve(__dirname, '..', '..'));
if (dir === repo || dir.startsWith(repo + path.sep)) {
  console.log('结果文件不能放在仓库里（日志原文有用户的 IP 和用户编号，免得提交上去）：放到调用方自己的临时目录');
  process.exit(2);
}
out = path.join(dir, path.basename(out));
const show = `python3 tools/team/cloud_logs_show.py ${JSON.stringify(out)}`;

function save(result) {
  fs.writeFileSync(out, JSON.stringify(result), { mode: 0o600 });
  fs.chmodSync(out, 0o600);
}

// 出错信息也写进结果文件（同时盖掉上一次的结果，免得把旧结果当成这次的），终端只打印出错类别
function fail(result, summary) {
  try {
    save(result);
    console.log(JSON.stringify({ ...summary, out }));
    console.log(`详情用 ${show} 看`);
  } catch (e) {
    console.log(JSON.stringify({ ...summary, write: e.code || e.name }));
  }
  process.exit(1);
}

(async () => {
  const P = process.env.WXCLOUD_CLI_LIB || path.join(require('os').homedir(), '.local/lib/node_modules/@wxcloud/cli/lib');
  const base = require(P + '/api/base');
  const all = [];
  let context;
  for (let page = 0; page < 20; page++) {
    const data = { EnvId: envId, StartTime: start, EndTime: end, QueryString: query, Limit: Number(limit), Sort: 'asc' };
    if (context) data.Context = context;
    const res = await base.fetchApi('wxa-dev-qbase/apihttpagent', {
      action: 'SearchClsLog', postdata: JSON.stringify(data), region: '', service: 'tcbr', version: '2022-02-17',
    });
    if (!res || !res.base_resp || res.base_resp.ret !== 0) {
      const ret = res && res.base_resp && res.base_resp.ret;
      fail({ error: 'agent', ret }, { error: 'agent', ret });
    }
    const r = JSON.parse(res.content).Response || {};
    if (r.Error) fail({ error: r.Error.Code, message: (r.Error.Message || '').slice(0, 160) }, { error: r.Error.Code });
    const lr = r.LogResults || {};
    for (const item of lr.Results || []) all.push(item);
    if (lr.ListOver || !lr.Context) {
      save({ count: all.length, listOver: !!lr.ListOver, pages: page + 1, results: all });
      console.log(JSON.stringify({ count: all.length, pages: page + 1, listOver: !!lr.ListOver, out }));
      console.log(`用 ${show} [关键词…] 看（遮掉了 IP、用户编号和文件编号）；结果文件不要直接打开或贴出来`);
      return;
    }
    context = lr.Context;
  }
  save({ count: all.length, listOver: false, pages: 20, truncated: true, results: all });
  console.log(JSON.stringify({ count: all.length, pages: 20, listOver: false, truncated: true, out }));
  console.log(`到了 20 页上限没取完，缩小时间窗再查；已取到的用 ${show} [关键词…] 看`);
})().catch((e) => fail({ error: 'exception', message: String((e && e.message) || e).slice(0, 160) }, { error: 'exception', code: e && (e.code || e.name) }));
