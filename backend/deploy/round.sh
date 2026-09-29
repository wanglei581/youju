#!/bin/sh
# 一轮抓取和处理（cron 每天 4 次调用）：按顺序执行，下游任务等抓取完成后再跑，
# 中午、晚上新抓到的文件当轮就补详情、抽截止、解析名单，不用等到第二天。
# 每个任务仍由 job.sh 加锁、限时、失败时发提醒；某一步失败不影响后面的步骤。
# 带 --notify（只有早上那一轮）：这一轮全部做完后再发微信提醒，保证当天的提醒包含这一轮的新文件。
D=/app/backend/deploy
log() { echo "[$(date '+%F %T')] $*" >> /proc/1/fd/1; }
# 整轮加锁：上一轮还没跑完就跳过这一轮并提醒，避免两轮的步骤交错（如本轮补详情被上一轮的锁挡住）
exec 9>/tmp/youju-round.lock
if ! flock -n 9; then
  log "跳过：上一轮抓取和处理还没跑完"
  "$D/job.sh" alert "上一轮抓取和处理还没跑完，跳过 $(date '+%H:%M') 这一轮" || :
  # 早上那一轮被跳过时照样发微信提醒（提醒只用已抓到的数据，有自己的锁），当天不漏发
  [ "$1" = "--notify" ] && "$D/job.sh" notify
  exit 0
fi
# 抓取成功后才核对栏目：抓取没跑完或失败时核对，会把还没检查到的栏目误报成漏检
"$D/job.sh" crawl && "$D/job.sh" check-sources
"$D/job.sh" fetch-details --limit 300 --workers 4
"$D/job.sh" extract-deadlines
# 规则抽完再让 AI 检查，才能比对哪些候选两边一致；没配 AI 密钥时这一步什么也不做
"$D/job.sh" ai-check --limit 300
# 用户纠错过的文件，用规则和 AI 复查已发布的截止日期，结果写在纠错旁边（纠错内容不发给 AI）
"$D/job.sh" triage-feedback
"$D/job.sh" parse-lists
[ "$1" = "--notify" ] && "$D/job.sh" notify
exit 0
