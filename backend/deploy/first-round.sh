#!/bin/sh
# 首次部署时在 admin 的"登录实例"里执行一次，准备首轮数据（docs/部署/微信云托管.md 第四节）：
#   导入来源 → 抓取 → 补详情（一次补完，约 30 分钟；定时任务每轮只补 300 份）→ 抽截止 → 名单
# 每一步都经 job.sh：有超时、失败提醒和单任务锁，日志进容器标准输出。某一步失败就停下。
# 执行期间拿着每轮任务的锁：定时任务那一轮会跳过并发提醒（属正常）。
D=/app/backend/deploy
exec 9>/tmp/youju-round.lock
# 和 round.sh 一样先留"这一轮要锁"的标记、等补正文队列停下（最多 5 分钟）：队列在途的下载、
# PDF 处理不和首轮抓取同时占内存和数据库连接
date +%s > /tmp/youju-round.wanted
if ! flock -w 300 /tmp/youju-fetch-details-queue.lock true; then
  rm -f /tmp/youju-round.wanted
  echo "补正文队列 5 分钟没停下，等它结束后再执行" >&2
  exit 1
fi
# 等 5 秒：补正文队列查"每轮在跑没有"时会瞬间拿一下共享锁
if ! flock -w 5 9; then
  rm -f /tmp/youju-round.wanted
  echo "定时任务的一轮正在进行，等它结束后再执行" >&2
  exit 1
fi
rm -f /tmp/youju-round.wanted
"$D/job.sh" import-sources \
  && "$D/job.sh" crawl \
  && "$D/job.sh" fetch-details --limit 2000 --workers 4 \
  && "$D/job.sh" extract-deadlines \
  && "$D/job.sh" parse-lists
