#!/bin/sh
# cron 调用的任务：读入容器环境变量后执行 youju 命令，日志打到容器标准输出（云托管日志里能看到）。
# 同一任务不重叠（flock，上一次还没跑完就跳过这次），单次最长 3 小时（timeout），超时算失败。
. /app/backend/deploy/env.sh
cd /app/backend
name="$1"
log() { echo "[$(date '+%F %T')] $*" >> /proc/1/fd/1; }
if [ "$name" = "check-sources" ]; then
  # 仅 cron 包装器使用：详细提醒已发送时返回 20，避免重复提醒。
  set -- "$@" --job
fi
# advisor-run 由 admin 服务的 crontab 每分钟调用；开关关闭时 CLI 会直接跳过。
log "开始：youju $*"
if flock -n -E 75 "/tmp/youju-${name}.lock" timeout 3h youju "$@" >> /proc/1/fd/1 2>&1; then
  log "完成：youju $*"
else
  code=$?
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
fi
