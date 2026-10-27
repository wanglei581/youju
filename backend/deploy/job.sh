#!/bin/sh
# cron 调用的任务：读入容器环境变量后执行 youju 命令，日志打到容器标准输出（云托管日志里能看到）。
# 同一任务不重叠（flock，上一次还没跑完就跳过这次），单次最长 3 小时（timeout），超时算失败。
. /app/backend/deploy/env.sh
cd /app/backend
name="$1"
log() { echo "[$(date '+%F %T')] $*" >> /proc/1/fd/1; }
# 记账有独立超时；数据库不可用不延误任务，不把命令参数或输出写入摘要。
# advisor-run、pay-run 每分钟一次，不记运行记录（情况看工具任务表、支付订单和管理台支付待办），
# 免得每分钟多起两次程序、每天多 1440 行；失败仍照常提醒。pay-reconcile 每天一次，照常记账。
# ai-heartbeat 每分钟、ai-eligibility 每 10 分钟一次，同样不记；夜里的 ai-eligibility-rebuild 照常记账。
if [ "$name" = "advisor-run" ] || [ "$name" = "pay-run" ] || [ "$name" = "ai-heartbeat" ] \
  || [ "$name" = "ai-eligibility" ]; then
  run_id=""
else
  run_id=$(timeout 20s youju job-start "$name" 2>> /proc/1/fd/1) || run_id=""
fi
finish() {
  [ -n "$run_id" ] && timeout 20s youju job-finish "$run_id" "$1" >> /proc/1/fd/1 2>&1 || :
}
trap 'finish 143; exit 143' TERM HUP
trap 'finish 130; exit 130' INT
if [ "$name" = "check-sources" ]; then
  # 仅 cron 包装器使用：详细提醒已发送时返回 20，避免重复提醒。
  set -- "$@" --job
fi
# advisor-run 由 admin 服务的 crontab 每分钟调用；开关关闭时 CLI 会直接跳过。它每轮在 50 秒里隔几秒
# 领一次新的 AI 任务，领到的做完才退出，偶尔会跨过下一分钟（advisor_tasks.POLL_WINDOW）。所以拿到锁
# 以后先记下这一轮的开始时间：下一分钟碰到锁时，上一轮开始不到 10 分钟就安静跳过（不算失败、不提醒），
# 超过 10 分钟当作卡住了，照常按"上一次还没跑完"提醒
started="/tmp/youju-${name}.started"
log "开始：youju $*"
if [ "$name" = "advisor-run" ]; then
  flock -n -E 75 "/tmp/youju-${name}.lock" \
    sh -c 'date +%s > "$0"; exec timeout 3h youju "$@"' "$started" "$@" >> /proc/1/fd/1 2>&1
else
  flock -n -E 75 "/tmp/youju-${name}.lock" timeout 3h youju "$@" >> /proc/1/fd/1 2>&1
fi
code=$?
if [ "$code" -eq 0 ]; then
  finish 0
  log "完成：youju $*"
  exit 0
fi
if [ "$name" = "advisor-run" ] && [ "$code" -eq 75 ]; then
  since=$(cat "$started" 2>/dev/null) || since=0
  case "$since" in '' | *[!0-9]*) since=0 ;; esac
  if [ $(($(date +%s) - since)) -lt 600 ]; then
    log "跳过：youju advisor-run（上一轮还在处理任务，不算失败）"
    exit 0
  fi
fi
finish "$code"
if [ "$name" = "check-sources" ] && [ "$code" -eq 20 ]; then
  log "未达标：youju check-sources（详细提醒已发送；任务按失败退出）"
  exit 1
fi
case "$code" in
  75) reason="上一次还没跑完，本次重叠跳过" ;;
  124) reason="运行超过 3 小时，已超时" ;;
  *) reason="任务执行失败" ;;
esac
log "失败：youju ${name}（退出码 ${code}；${reason}）"
youju alert "任务 youju ${name} 失败：退出码 ${code}（${reason}）" >> /proc/1/fd/1 2>&1 || :
exit "$code"
