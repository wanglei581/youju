#!/usr/bin/env bash
# 项目内独立的 MySQL 8.0 实例（线上用微信云托管自带的 MySQL 8.0）：数据放在 backend/.mysql/data，
# 只监听本机，不注册系统服务、不改系统设置。root 无密码，仅供本机开发和测试。
# 数据目录本身的名字不能以点开头（如 .mysqldata），否则 MySQL 8.0/8.4 第二次启动时报
# "Can't create UNDO tablespace"，所以放在 .mysql/ 下面的 data 目录里。
# 用法：scripts/dev_mysql.sh start | stop | status | reset
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATADIR="$BACKEND_DIR/.mysql/data"
LOG="$BACKEND_DIR/.mysql/mysqld.log"
PORT="${YOUJU_MYSQL_PORT:-33306}"
SOCKET="/tmp/youju-mysql.sock"
BIN="${YOUJU_MYSQL_BIN:-/opt/homebrew/opt/mysql@8.0/bin}"

client() {
  "${BIN}/mysql" --protocol=TCP -h 127.0.0.1 -P "${PORT}" -u root "$@"
}

running() {
  client -e "select 1" >/dev/null 2>&1
}

init() {
  if [ ! -d "${DATADIR}" ]; then
    mkdir -p "$(dirname "${DATADIR}")"
    "${BIN}/mysqld" --no-defaults --initialize-insecure --datadir="${DATADIR}" >/dev/null 2>&1
    echo "已初始化：${DATADIR}"
  fi
}

start() {
  init
  if running; then
    echo "已在运行（端口 ${PORT}）"
  else
    # 和 PostgreSQL 一样区分大小写、不忽略尾部空格：库的默认排序规则用 utf8mb4_0900_bin；会话时区统一 UTC
    "${BIN}/mysqld" --no-defaults --datadir="${DATADIR}" --port="${PORT}" \
      --bind-address=127.0.0.1 --socket="${SOCKET}" --mysqlx=OFF \
      --character-set-server=utf8mb4 --collation-server=utf8mb4_0900_bin \
      --default-time-zone=+00:00 --log-error="${LOG}" >/dev/null 2>&1 &
    for _ in $(seq 1 60); do
      running && break
      sleep 0.5
    done
    running || { echo "启动失败，见 ${LOG}"; exit 1; }
    echo "已启动（端口 ${PORT}）"
  fi
  for db in youju youju_test; do
    client -e "CREATE DATABASE IF NOT EXISTS ${db} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
  done
}

stop() {
  if running; then
    "${BIN}/mysqladmin" --protocol=TCP -h 127.0.0.1 -P "${PORT}" -u root shutdown
    echo "已停止"
  else
    echo "未在运行"
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) running && echo "运行中（端口 ${PORT}）" || echo "未在运行" ;;
  reset)
    stop
    rm -rf "${DATADIR}" "${LOG}"
    start
    ;;
  *)
    echo "用法：$0 start | stop | status | reset" >&2
    exit 2
    ;;
esac
