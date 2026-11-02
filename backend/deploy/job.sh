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
# advisor-run 由 admin 服务的 crontab 每分钟调用；开关关闭时 CLI 会直接跳过。它每轮在开始后 50 秒里
# 隔几秒领一次新的 AI 任务，领到的做完才退出，常会跨过下一分钟（advisor_tasks.POLL_WINDOW）：
# - 下一分钟这一次不马上放弃，最多等 55 秒，上一轮一结束就接着跑，中间最多空几秒；等的时间短于
#   cron 的一分钟，同一时刻最多一个在跑、一个在等，不会堆积；
# - 拿到锁以后先记下这一轮的开始时间（先写临时文件再改名，读的一方不会读到半截）。等满 55 秒还没
#   拿到锁时，上一轮开始不到 10 分钟就安静跳过（不算失败、不提醒）；超过 10 分钟、读不到开始时间、
#   或时钟往回调过（差值是负数），都当作卡住了，照常按"上一次还没跑完"提醒。
# sh -c 里 $0 是开始时间文件，"$@" 才是传给 youju 的参数
started="/tmp/youju-${name}.started"
log "开始：youju $*"
if [ "$name" = "advisor-run" ]; then
  flock -w 55 -E 75 "/tmp/youju-${name}.lock" \
    sh -c 'date +%s > "$0.tmp" && mv "$0.tmp" "$0"; exec timeout 3h youju "$@"' \
    "$started" "$@" >> /proc/1/fd/1 2>&1
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
  age=$(($(date +%s) - since))
  if [ "$age" -ge 0 ] && [ "$age" -lt 600 ]; then
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
