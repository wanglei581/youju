// 只读：经 wxcloud CLI 的官方接口代理调用 tcbr SearchClsLog（2022-02-17），按时间窗和关键词读运行日志。
// 用法：node tools/team/cloud-logs.cjs --out <结果文件> <envId> "<开始 YYYY-MM-DD HH:MM:SS>" "<结束>" "<查询串>" [条数]
//       python3 tools/team/cloud_logs_show.py <结果文件> [中文关键词…]
// 查到的日志原文只写进 <结果文件>，终端只打印条数、页数、文件路径和一行查看提示。原文里有用户的来源 IP 和用户编号，
// 读日志的是 AI 成员，打印出来的内容会发到境外的模型服务（个人信息保护影响评估第六节（二）第 6 条、
// （三）第 7 条）。所以：结果文件放在调用方自己的临时目录（放进任何 git 仓库都会被拒绝，免得提交上去），
// 文件只有本人可读（先建权限 600 的新文件再换上去：原来是符号链接的，不会写到它指向的地方）；
// 不要直接打开、cat 或贴出来，一律用 cloud_logs_show.py 看（它显示前遮掉 IP、用户编号、openid 和文件编号）。
// 要看完整的 IP、按某个用户查，由创始人在云托管控制台的日志页自己查；也不要拿 IP 或用户编号当查询串。
// 出错时，出错信息（错误码、原文）整段写进结果文件，终端只打印出错类别（api 接口报错、agent 代理报错、
// exception 异常）、文件路径和一行查看提示，同样用 cloud_logs_show.py 看；连结果文件都写不进时，终端只打印
// 出错类别和本机文件系统的错误名（EACCES 这类，不是这种格式的打印 failed）。wxcloud 库自己打印的东西
// （NODE_ENV=DEBUG 时它会把请求和回应原文打出来，DEBUG=* 时会把带登录密钥的请求参数打出来）一律不显示。
// 查询串只认英文分词（如 cache-attachments、batchdeletefile）；中文关键词搜不到，要用 "*" 取整段时间窗，
// 再交给 cloud_logs_show.py 在本机按中文过滤。时间是北京时间；一次最多取 20 页（每页 [条数] 行）。
// 用本机已登录的 wxcloud CLI（npm 全局包 @wxcloud/cli）里的官方接口代理；路径可用 WXCLOUD_CLI_LIB 覆盖
const fs = require('fs');
const path = require('path');

// 终端输出只有这里一个出口。wxcloud 库在 NODE_ENV=DEBUG 时会把请求和回应原文（日志原文、登录密钥）
// console.log 出来；DEBUG=* 时它用的 debug 包会把带登录密钥的请求参数直接写到 stderr：加载库之前去掉这两个
// 环境变量，再把 console 的输出和 stderr 都换成空函数，库就打印不出任何东西
const say = (line) => process.stdout.write(`${line}\n`);
// 终端提前关掉（如接了 | head -1）时写不出去就算了：不能让这个错误变成异常，把存好的结果盖掉
process.stdout.on('error', () => {});
delete process.env.NODE_ENV;
delete process.env.DEBUG;
process.stderr.write = () => true;
for (const name of ['log', 'info', 'warn', 'error', 'debug', 'trace', 'dir', 'dirxml', 'table']) console[name] = () => {};

const USAGE = '用法：node tools/team/cloud-logs.cjs --out <结果文件> <envId> "<开始 YYYY-MM-DD HH:MM:SS>" "<结束>" "<查询串>" [条数]';
const quote = (s) => `'${s.replace(/'/g, "'\\''")}'`;
// 提示里写查看脚本的绝对路径：在仓库根目录以外照抄也能跑
const SHOW_PY = `python3 ${quote(path.join(__dirname, 'cloud_logs_show.py'))}`;
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
  say(USAGE);
  say(`结果只写进 --out 指定的文件（放在自己的临时目录），终端不打印日志原文；看结果用 ${SHOW_PY} <结果文件>`);
  process.exit(2);
}
let dir;
try {
  dir = fs.realpathSync.native(path.dirname(path.resolve(out)));
} catch (e) {
  say(`结果文件所在的目录不存在：${path.dirname(path.resolve(out))}`);
  process.exit(2);
}
// 目录本身或任何一级上级有 .git（主仓库是目录，worktree 是文件）就是在 git 仓库里
for (let p = dir; ; p = path.dirname(p)) {
  if (fs.existsSync(path.join(p, '.git'))) {
    say('结果文件不能放在 git 仓库里（日志原文有用户的 IP 和用户编号，免得提交上去）：放到调用方自己的临时目录');
    process.exit(2);
  }
  if (path.dirname(p) === p) break;
}
out = path.join(dir, path.basename(out));
const show = `${SHOW_PY} ${quote(out)}`;

// 在同一目录先建一个权限 600 的新文件，写完再改名换上去：已有的宽权限文件不会在写入时被别人读到，
// 目标是符号链接时换掉的是链接本身，不会写到它指向的地方
function save(result) {
  const tmp = path.join(dir, `.${path.basename(out)}.${require('crypto').randomBytes(6).toString('hex')}.tmp`);
  try {
    fs.writeFileSync(tmp, JSON.stringify(result), { mode: 0o600, flag: 'wx' });
    fs.renameSync(tmp, out);
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch (_) { /* 没建成就不用删 */ }
    throw e;
  }
}

// 出错信息写进结果文件（同时盖掉上一次的结果，免得把旧结果当成这次的）。终端只打印固定的出错类别：
// 错误码、ret 号、异常原文都来自接口或运行环境，不保证不带日志内容，只放进文件。原文不截断：
// 截在 IP 或编号中间，残缺的部分遮蔽时认不出来；限长由 cloud_logs_show.py 遮蔽之后再做
function fail(kind, detail) {
  try {
    save({ error: kind, ...detail });
    say(JSON.stringify({ error: kind, out }));
    say(`详情用 ${show} 看`);
  } catch (e) {
    // 写不进结果文件：只打印本机文件系统的错误名（EACCES 这类）
    say(JSON.stringify({ error: kind, write: /^E[A-Z]+$/.test(e && e.code) ? e.code : 'failed' }));
  }
  process.exit(1);
}
// 查到的结果存好以后再出的异常（比如只剩打印提示时）只退出，不用出错信息去盖存好的结果
let saved = false;
const exception = (e) => {
  if (saved) process.exit(0);
  fail('exception', { name: e && e.name, code: e && e.code, message: String((e && e.message) || e) });
};
// 库里没被 await 接住的异常也走这里，免得 Node 把带原文的堆栈打到终端
process.on('uncaughtException', exception);
process.on('unhandledRejection', exception);

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
    if (!res || !res.base_resp || res.base_resp.ret !== 0) fail('agent', { ret: res && res.base_resp && res.base_resp.ret });
    const r = JSON.parse(res.content).Response || {};
    if (r.Error) fail('api', { code: r.Error.Code, message: String(r.Error.Message || '') });
    const lr = r.LogResults || {};
    for (const item of lr.Results || []) all.push(item);
    if (lr.ListOver || !lr.Context) {
      save({ count: all.length, listOver: !!lr.ListOver, pages: page + 1, results: all });
      saved = true;
      say(JSON.stringify({ count: all.length, pages: page + 1, out }));
      say(`用 ${show} [关键词…] 看（遮掉了 IP、用户编号、openid 和文件编号）；结果文件不要直接打开或贴出来`);
      return;
    }
    context = lr.Context;
  }
  save({ count: all.length, listOver: false, pages: 20, truncated: true, results: all });
  saved = true;
  // 终端只给条数、页数、路径；没取完由下面这行提示说，listOver、truncated 留在结果文件里（查看时显示）
  say(JSON.stringify({ count: all.length, pages: 20, out }));
  say(`到了 20 页上限没取完，缩小时间窗再查；已取到的用 ${show} [关键词…] 看`);
})().catch(exception);
