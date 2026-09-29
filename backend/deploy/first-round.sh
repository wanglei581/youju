#!/bin/sh
# 首次部署时在 admin 的"登录实例"里执行一次，准备首轮数据（docs/部署/微信云托管.md 第四节）：
#   导入来源 → 抓取 → 补详情（一次补完，约 30 分钟；定时任务每轮只补 300 份）→ 抽截止 → 名单
# 每一步都经 job.sh：有超时、失败提醒和单任务锁，日志进容器标准输出。某一步失败就停下。
# 执行期间拿着每轮任务的锁：定时任务那一轮会跳过并发提醒（属正常）。
D=/app/backend/deploy
exec 9>/tmp/youju-round.lock
if ! flock -n 9; then
  echo "定时任务的一轮正在进行，等它结束后再执行" >&2
  exit 1
fi
"$D/job.sh" import-sources \
  && "$D/job.sh" crawl \
  && "$D/job.sh" fetch-details --limit 2000 --workers 4 \
  && "$D/job.sh" extract-deadlines \
  && "$D/job.sh" parse-lists
