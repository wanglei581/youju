#!/bin/sh
# 容器启动脚本（微信云托管）。
#   YOUJU_ROLE=admin  核对台和定时任务服务（实例数固定为 1）：先迁移数据库，再启动 cron 和接口
#   YOUJU_ROLE=api    小程序接口服务（按量伸缩）：只启动接口；不做迁移
# 迁移用 MySQL 命名锁（GET_LOCK），滚动发布时即使新旧两个 admin 实例同时启动，也只有一个在迁移。
# 发布顺序：先发 admin（完成迁移），再发 api。
set -e
cd /app/backend
case "${YOUJU_ROLE:-}" in
  api|admin) ;;
  *) echo "YOUJU_ROLE 只能是 api 或 admin" >&2; exit 1 ;;
esac
if [ "$YOUJU_ROLE" = "admin" ]; then
  # 核对台开公网，口令至少 32 位随机字符，否则不启动
  if [ "${#YOUJU_ADMIN_TOKEN}" -lt 32 ]; then
    echo "YOUJU_ADMIN_TOKEN 至少 32 位，请在云托管控制台设置后重新发布" >&2
    exit 1
  fi
  youju db-upgrade
  # 按当前代码里的标题规则重算文件类型（规则随版本更新；搜索按类型筛选时只比较算好的这一列）
  youju reclassify
  # cron 看不到容器的环境变量，先写进文件（只有容器内 root 可读），任务脚本里再读
  umask 077
  env | grep -E '^(YOUJU_|TZ=|PATH=)' | sed "s/'/'\\\\''/g; s/=\(.*\)/='\1'/; s/^/export /" > /app/backend/deploy/env.sh
  crontab /app/backend/deploy/crontab
  # 先写一次心跳：健康检查要求心跳 15 分钟内更新过，cron 之后每 5 分钟写一次
  date +%s > /tmp/youju-cron-heartbeat
  cron
fi
# 固定单进程：一个实例一个进程（接口出错提醒按进程计数、核对台口令限流也在进程内）。
# 不写 --workers 时 uvicorn 会读环境变量 WEB_CONCURRENCY，可能起多个进程
exec uvicorn youju.api.app:app --host 0.0.0.0 --port "${PORT:-80}" --workers 1
