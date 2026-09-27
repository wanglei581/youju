#!/bin/sh
# cron 调用的任务：读入容器环境变量后执行 youju 命令，日志打到容器标准输出（云托管日志里能看到）
. /app/backend/deploy/env.sh
cd /app/backend
echo "[$(date '+%F %T')] 开始：youju $*" >> /proc/1/fd/1
if uv run --no-sync youju "$@" >> /proc/1/fd/1 2>&1; then
  echo "[$(date '+%F %T')] 完成：youju $*" >> /proc/1/fd/1
else
  echo "[$(date '+%F %T')] 失败：youju $*（退出码 $?）" >> /proc/1/fd/1
fi
