#!/usr/bin/env bash
# 项目内独立的 PostgreSQL 实例：数据放在 backend/.pgdata，不注册系统服务、不改系统设置。
# 用法：scripts/dev_db.sh start | stop | status | reset
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PGDATA="$BACKEND_DIR/.pgdata"
LOG="$BACKEND_DIR/.pgdata.log"
PORT="${YOUJU_PG_PORT:-55432}"
# macOS 上未显式设置区域时，postmaster 会拒绝启动（"became multithreaded during startup"）。
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

init() {
  if [ ! -d "${PGDATA}" ]; then
    initdb -D "${PGDATA}" -E UTF8 --locale=en_US.UTF-8 -A trust -U "$USER" >/dev/null
    echo "已初始化：${PGDATA}"
  fi
}

start() {
  init
  if pg_ctl -D "${PGDATA}" status >/dev/null 2>&1; then
    echo "已在运行（端口 ${PORT}）"
  else
    pg_ctl -D "${PGDATA}" -l "${LOG}" -o "-p ${PORT} -c listen_addresses=localhost -k /tmp" -w start >/dev/null
    echo "已启动（端口 ${PORT}）"
  fi
  for db in youju youju_test; do
    createdb -h localhost -p "${PORT}" "${db}" 2>/dev/null && echo "已创建数据库 ${db}" || true
  done
}

stop() {
  if pg_ctl -D "${PGDATA}" status >/dev/null 2>&1; then
    pg_ctl -D "${PGDATA}" -w stop >/dev/null
    echo "已停止"
  else
    echo "未在运行"
  fi
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  status) pg_ctl -D "${PGDATA}" status || true ;;
  reset) stop; rm -rf "${PGDATA}" "${LOG}"; start ;;
  *) echo "用法：$0 start|stop|status|reset" >&2; exit 2 ;;
esac
