// 只读：经 wxcloud CLI 的官方接口代理调用 tcbr SearchClsLog（2022-02-17），按时间窗和关键词读运行日志。
// 用法：node tools/team/cloud-logs.cjs <envId> "<开始 YYYY-MM-DD HH:MM:SS>" "<结束>" "<查询串>" [条数]
// 查询串只认英文分词（如 cache-attachments、batchdeletefile）；中文关键词搜不到，要用 "*" 取整段时间窗，
// 再交给 cloud_logs_show.py 在本机按中文过滤。时间是北京时间；一次最多取 20 页（每页 [条数] 行）。
// 用本机已登录的 wxcloud CLI（npm 全局包 @wxcloud/cli）里的官方接口代理；路径可用 WXCLOUD_CLI_LIB 覆盖
const P = process.env.WXCLOUD_CLI_LIB || require('path').join(require('os').homedir(), '.local/lib/node_modules/@wxcloud/cli/lib');
const base = require(P + '/api/base');
const [envId, start, end, query, limit = '100'] = process.argv.slice(2);
(async () => {
  const all = [];
  let context;
  for (let page = 0; page < 20; page++) {
    const data = { EnvId: envId, StartTime: start, EndTime: end, QueryString: query, Limit: Number(limit), Sort: 'asc' };
    if (context) data.Context = context;
    const res = await base.fetchApi('wxa-dev-qbase/apihttpagent', {
      action: 'SearchClsLog', postdata: JSON.stringify(data), region: '', service: 'tcbr', version: '2022-02-17',
    });
    if (!res || !res.base_resp || res.base_resp.ret !== 0) { console.log(JSON.stringify({ error: 'agent', ret: res && res.base_resp && res.base_resp.ret })); process.exit(1); }
    const r = JSON.parse(res.content).Response || {};
    if (r.Error) { console.log(JSON.stringify({ error: r.Error.Code, message: (r.Error.Message || '').slice(0, 160) })); process.exit(1); }
    const lr = r.LogResults || {};
    for (const item of lr.Results || []) all.push(item);
    if (lr.ListOver || !lr.Context) { console.log(JSON.stringify({ count: all.length, listOver: !!lr.ListOver, pages: page + 1, results: all })); return; }
    context = lr.Context;
  }
  console.log(JSON.stringify({ count: all.length, listOver: false, truncated: true, results: all }));
})().catch((e) => { console.log(JSON.stringify({ error: 'exception', message: String(e && e.message || e).slice(0, 160) })); process.exit(1); });
