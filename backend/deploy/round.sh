#!/bin/sh
# 一轮抓取和处理（cron 每天 2 次调用：早 6:30、晚 20:30，D-039）：按顺序执行，下游任务等抓取完成后再跑，
# 中午、晚上新抓到的文件当轮就补详情、取附件文字、抽截止、解析名单，不用等到第二天。
# 每个任务仍由 job.sh 加锁、限时、失败时发提醒；某一步失败不影响后面的步骤。
# 带 --notify（只有早上那一轮）：这一轮全部做完后再发微信提醒，保证当天的提醒包含这一轮的新文件。
D=/app/backend/deploy
. "$D/env.sh"
cd /app/backend
log() { echo "[$(date '+%F %T')] $*" >> /proc/1/fd/1; }
run_id=$(timeout 20s youju job-start round 2>> /proc/1/fd/1) || run_id=""
finish() {
  [ -n "$run_id" ] && timeout 20s youju job-finish "$run_id" "$1" >> /proc/1/fd/1 2>&1 || :
}
trap 'finish 143; exit 143' TERM HUP
trap 'finish 130; exit 130' INT
round_code=0
run() {
  "$D/job.sh" "$@"
  code=$?
  [ "$code" -eq 0 ] || round_code=1
  return "$code"
}
# 整轮加锁：上一轮还没跑完就跳过这一轮并提醒，避免两轮的步骤交错（如本轮补详情被上一轮的锁挡住）
exec 9>/tmp/youju-round.lock
if ! flock -n 9; then
  finish 75
  log "跳过：上一轮抓取和处理还没跑完"
  "$D/job.sh" alert "上一轮抓取和处理还没跑完，跳过 $(date '+%H:%M') 这一轮" || :
  # 早上那一轮被跳过时照样发微信提醒（提醒只用已抓到的数据，有自己的锁），当天不漏发
  [ "$1" = "--notify" ] && "$D/job.sh" notify
  exit 0
fi
# 抓取成功后才核对栏目：抓取没跑完或失败时核对，会把还没检查到的栏目误报成漏检。
# 早上那一轮另外提醒今天正好连续 3 天没抓到的栏目（每个栏目只提醒这一次，晚上那轮不重复）
if [ "$1" = "--notify" ]; then
  run crawl && run check-sources --streaks
else
  run crawl && run check-sources
fi
# 每天 2 轮，每轮 600 份，每天补详情总量和原来 4 轮 × 300 份一样
run fetch-details --limit 600 --workers 4
# 附件文字（小程序站内阅读附件用）：补完详情才有新附件的链接，紧接着取，当轮就能读，早上的提醒发出去时
# 附件已经能读（以前只在 10:15、23:30 的附件副本任务里顺带取，新附件要等半天，点开都是“正在准备”）。
# 后面的步骤都不读附件文字，放在最前面附件最早能读；同一轮里按顺序跑，不和补详情、解析名单同时下载。
# 每轮最多 200 个，新的先取：平时每轮新附件几个到十几个、多的几十个，几分钟取完；积压时（刚上线、
# 取文字规则升级后全部重取）满 200 个按每个 3–5 秒（同一网站间隔 3 秒）估算约 10–17 分钟，早上的
# 提醒一般晚这么多；遇到下载超时（20 秒）、难取的文件（取文字最长 180 秒）会更久，job.sh 单步最长
# 3 小时。每天两轮能消化 400 个，没轮到的下一轮或附件副本任务接着取。
# 和附件副本任务、手动补齐共用一把锁：另一个正在跑时这一步一个也不取，返回 0，不算失败、不发提醒
# （日志里那句"这次一个也没取"就是跳过了）
run attachment-texts --limit 200
# 转发归组（D-055）：补完详情（有正文才看得到引用的部里文号）后，把同一份部里通知的各地版本归组，
# 送管理台“转发归组”页确认；确认前对外仍按单条显示
run repost-groups
run extract-deadlines
run extract-fields
# 规则抽完再让 AI 检查，才能比对哪些候选两边一致；没配 AI 密钥时这一步什么也不做。
# 先补跑 30 天内截止的待核对候选所在的文件（核对台先核这些，要先有 AI 的结果），再查新文件，合计 600 份
run ai-check --limit 600 --due-first 30
# 申报条件（D-052 第④项）：后台模型抽条件、每条带原文引用，送核对台，人工整份确认后才对外。
# 30 天内截止的先抽，每轮最多 60 份（核对要人工，抽多了也核不完）；没配 AI 密钥时这一步什么也不做
run extract-conditions --limit 60 --due-days 30
# 用户纠错过的文件，用规则和 AI 复查已发布的截止日期，结果写在纠错旁边（纠错内容不发给 AI）
run triage-feedback
run parse-lists
[ "$1" = "--notify" ] && run notify
finish "$round_code"
exit 0
