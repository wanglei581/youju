#!/bin/sh
# 容器启动脚本（微信云托管）。
#   YOUJU_ROLE=admin  核对台和定时任务服务（实例数固定为 1）：先迁移数据库，再启动 cron 和接口
#   YOUJU_ROLE=api    小程序接口服务（按量伸缩）：只启动接口；不做迁移
# 迁移用 PostgreSQL 咨询锁，滚动发布时即使新旧两个 admin 实例同时启动，也只有一个在迁移。
# 发布顺序：先发 admin（完成迁移），再发 api。
set -e
cd /app/backend
if [ "${YOUJU_ROLE:-api}" = "admin" ]; then
  uv run --no-sync youju db-upgrade
  # cron 看不到容器的环境变量，先写进文件（只有容器内 root 可读），任务脚本里再读
  umask 077
  env | grep -E '^(YOUJU_|TZ=|PATH=|UV_)' | sed "s/'/'\\\\''/g; s/=\(.*\)/='\1'/; s/^/export /" > /app/backend/deploy/env.sh
  crontab /app/backend/deploy/crontab
  cron
fi
exec uv run --no-sync uvicorn youju.api.app:app --host 0.0.0.0 --port "${PORT:-80}"
