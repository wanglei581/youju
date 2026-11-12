#!/bin/bash
# 本机端（Windows 主机的 WSL 发行版 youju）录密钥：只在交互终端里用，输入不显示、不进命令历史，
# 只回长度和指纹（SHA-256 的前 4 位，和服务器上 rotate-sync 显示的对一下）。在 UU 远程的终端里：
#   wsl -d youju -u root --cd /opt/youju/local -e bash set-secret.sh YOUJU_SYNC_SECRET
# 填完 docker compose up -d worker 生效（在同一个目录）。
set -euo pipefail
cd "$(dirname "$0")"

NAMES="YOUJU_SYNC_SECRET YOUJU_OFFSITE_S3_ACCESS_KEY YOUJU_OFFSITE_S3_SECRET_KEY YOUJU_ALERT_WEBHOOK"
die() { echo "错误：$*" >&2; exit 1; }

name=${1:-}
[[ " $NAMES " == *" $name "* ]] && [ -n "$name" ] || die "用法：set-secret.sh <名称>；名称是 ${NAMES}"
[ -t 0 ] && [ -t 1 ] || die "只能在交互终端里用"
[ -f .env ] || die "还没有 .env：先跑一遍 setup-wsl.ps1"
case "$name" in
  YOUJU_SYNC_SECRET) re='^[0-9a-f]{64}$' ;;
  YOUJU_ALERT_WEBHOOK) re='^https://[A-Za-z0-9.-]+/[A-Za-z0-9._~:/?=&%+-]*$' ;;
  *) re='^[A-Za-z0-9._+/=:-]{8,200}$' ;;
esac
printf '粘贴 %s 的值（不显示），回车：' "$name"
value=""
IFS= read -rs value || true
echo
value=${value%$'\r'}
[[ "$value" =~ $re ]] || die "格式不对（${#value} 位），没有写"
# 先写临时文件再改名；值经环境变量给 awk，不进命令行参数
(
  umask 077
  KEY="$name" VALUE="$value" awk 'BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; done = 0 }
    { sub(/\r$/, "") }
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' .env > .env.tmp
)
mv -f .env.tmp .env
echo "已写入 ${name}：${#value} 位，指纹 $(printf '%s' "$value" | sha256sum | cut -c1-4)。运行 docker compose up -d worker 生效"
