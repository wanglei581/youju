#!/bin/sh
# 一轮抓取和处理（cron 每天 4 次调用）：按顺序执行，下游任务等抓取完成后再跑，
# 中午、晚上新抓到的文件当轮就补详情、抽截止、解析名单，不用等到第二天。
# 每个任务仍由 job.sh 加锁、限时、失败时发提醒；某一步失败不影响后面的步骤。
D=/app/backend/deploy
# 抓取成功后才核对栏目：抓取没跑完或失败时核对，会把还没检查到的栏目误报成漏检
"$D/job.sh" crawl && "$D/job.sh" check-sources
"$D/job.sh" fetch-details --limit 300 --workers 4
"$D/job.sh" extract-deadlines
"$D/job.sh" parse-lists
exit 0
