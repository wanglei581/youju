#!/bin/sh
# cron 调用的任务：读入容器环境变量后执行 youju 命令，日志打到容器标准输出（云托管日志里能看到）。
# 同一任务不重叠（flock，上一次还没跑完就跳过这次），单次最长 3 小时（timeout），超时算失败。
. /app/backend/deploy/env.sh
cd /app/backend
name="$1"
log() { echo "[$(date '+%F %T')] $*" >> /proc/1/fd/1; }
log "开始：youju $*"
if flock -n "/tmp/youju-${name}.lock" timeout 3h youju "$@" >> /proc/1/fd/1 2>&1; then
  log "完成：youju $*"
else
  code=$?
  log "失败：youju $*（退出码 ${code}；1 表示上一次还没跑完，124 表示超时）"
  exit "$code"
fi
