// 只读：用本机已登录的 wxcloud CLI 的临时存储凭证，下载线上最新的公开内容快照
// （public-snapshots/youju-public-日期.jsonl.gz：只有通知、正文、附件文字、名单等公开内容，不含用户数据、
// 密钥和日志，见 backend/src/youju/public_snapshot.py）。只认这个目录、这种文件名；整库备份
// （backups/，含用户数据）和其他目录一概不读。
// 用法：node tools/team/public-snapshot.cjs <保存目录> [环境 ID]（不给就用 YOUJU_CLOUD_ENV，再不行用线上环境）
// 前提：本机已运行过 wxcloud login（创始人开通的 CLI 密钥）。
// 最后一行输出本机文件路径；同名、同大小的已下载过就不再下载；保存目录里只留最近 3 份。
// wxcloud CLI 的安装位置可用 WXCLOUD_CLI_DIR 覆盖（默认 npm 全局包 ~/.local/lib/node_modules/@wxcloud/cli）。
const fs = require('fs');
const os = require('os');
const path = require('path');

const CLI = process.env.WXCLOUD_CLI_DIR || path.join(os.homedir(), '.local/lib/node_modules/@wxcloud/cli');
const PREFIX = 'public-snapshots/';
const NAME = /^public-snapshots\/youju-public-\d{8}\.jsonl\.gz$/;
const LOCAL = /^youju-public-\d{8}\.jsonl\.gz$/;
const KEEP_LOCAL = 3;

async function main() {
  const [dir, given] = process.argv.slice(2);
  const envId = given || process.env.YOUJU_CLOUD_ENV || 'prod-d5getqftefb61fc92';
  if (!dir) {
    console.error('用法：node tools/team/public-snapshot.cjs <保存目录> [环境 ID]');
    process.exit(2);
  }
  // 和 wxcloud 命令启动时一样初始化它的云接口（hooks/init.js），再取对象存储的临时凭证
  const { readLoginState } = require(CLI + '/lib/utils/auth');
  require(CLI + '/lib/api/adapter').initCloudAPI((await readLoginState()).appid);
  require(CLI + '/lib/api/common').setApiCommonParameters({ region: 'ap-shanghai' });
  const { CloudAPI } = require(CLI + '/node_modules/@wxcloud/core');
  const { getAuthorizationThunk } = require(CLI + '/lib/utils/storage');
  const COS = require(CLI + '/node_modules/cos-nodejs-sdk-v5');

  const envs = await CloudAPI.tcbDescribeWxCloudBaseRunEnvs({ allRegions: true });
  const env = (envs.envList || []).find((e) => e.envId === envId);
  const storage = env && env.storages && env.storages[0];
  if (!storage) throw new Error(`没找到环境 ${envId} 的对象存储：本机 wxcloud 登录了吗（wxcloud login）？`);
  const cos = new COS({ getAuthorization: await getAuthorizationThunk(storage) });
  const where = { Bucket: storage.bucket, Region: storage.region };

  // 按页列完（一页最多 1000 个，平时只有七八个）
  const items = [];
  let marker;
  for (;;) {
    const page = await cos.getBucket({ ...where, Prefix: PREFIX, MaxKeys: 1000, Marker: marker });
    const contents = page.Contents || [];
    items.push(...contents.filter((item) => NAME.test(item.Key)));
    if (String(page.IsTruncated) !== 'true' || !contents.length) break;
    marker = page.NextMarker || contents[contents.length - 1].Key;
  }
  items.sort((a, b) => (a.Key < b.Key ? 1 : -1));
  const latest = items[0];
  if (!latest) throw new Error('线上还没有公开快照（admin 每天 4:50 生成）');

  fs.mkdirSync(dir, { recursive: true });
  const dest = path.join(dir, path.basename(latest.Key));
  const size = Number(latest.Size);
  if (!(fs.existsSync(dest) && fs.statSync(dest).size === size)) {
    const part = dest + '.part';
    await cos.getObject({ ...where, Key: latest.Key, Output: fs.createWriteStream(part) });
    const got = fs.statSync(part).size;
    if (got !== size) {
      fs.rmSync(part, { force: true });
      throw new Error(`下载不完整：${got} / ${size} 字节`);
    }
    fs.renameSync(part, dest);
    console.error(`已下载 ${latest.Key}（${size} 字节）`);
  } else {
    console.error(`本机已有 ${path.basename(dest)}，不重复下载`);
  }
  // 本机只留最近几份
  const old = fs
    .readdirSync(dir)
    .filter((name) => LOCAL.test(name))
    .sort()
    .reverse()
    .slice(KEEP_LOCAL);
  for (const name of old) fs.rmSync(path.join(dir, name), { force: true });
  console.log(dest);
}

main().catch((e) => {
  console.error('下载公开快照失败：' + String((e && e.message) || e).slice(0, 300));
  process.exit(1);
});
