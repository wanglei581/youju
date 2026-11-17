#!/bin/bash
# 本机端（Windows 主机的 WSL 发行版 youju）录后台 AI 的不涉密配置：线路、模型、附加参数，和服务器一样。
# 后台 AI（截止日期交叉核对、分类、申报条件抽取）在本机跑，要和服务器用同一条线路、同一个模型：
#   1. 服务器网页终端：sudo /opt/youju/bin/youju-shared.sh show ai，复制打出来的"名称=值"几行；
#   2. UU 远程的终端：wsl -d youju -u root --cd /opt/youju/local -e bash set-config.sh，粘贴，再按一次回车；
#   3. 密钥另用 set-secret.sh YOUJU_AI_API_KEY 录；都录完 docker compose up -d worker 生效（在同一个目录）。
# 只认下面几项；有一行不认识或格式不对，整批都不写。不涉密，写进去的值会照样打出来。
set -euo pipefail
cd "$(dirname "$0")"

NAMES="YOUJU_AI_BASE_URL YOUJU_AI_MODEL YOUJU_AI_BACKGROUND_MODEL YOUJU_AI_REQUEST_EXTRA"
die() { echo "错误：$*" >&2; exit 1; }

[ -f .env ] || die "还没有 .env：先跑一遍 setup-wsl.ps1"

# 附加参数要是一个 JSON 对象（程序启动时按字典读，读不了 worker 就起不来）
json_object() {
  local check='import json, sys; sys.exit(0 if isinstance(json.loads(sys.argv[1]), dict) else 1)'
  if command -v python3 >/dev/null 2>&1; then
    python3 -I -c "$check" "$1" 2>/dev/null
  else
    docker compose run --rm --no-deps -T worker python -I -c "$check" "$1" >/dev/null 2>&1
  fi
}

# 值要能不加引号写成一行、原样读回来：docker compose 读 .env 时 $ 会被当成变量、" #" 后面当注释
value_ok() {
  local name=$1 value=$2
  [[ "$value" != *'$'* && "$value" != *' #'* && "$value" != \"* && "$value" != \'* ]] || return 1
  case "$name" in
    YOUJU_AI_BASE_URL) [[ "$value" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]] ;;
    YOUJU_AI_MODEL | YOUJU_AI_BACKGROUND_MODEL) [[ "$value" =~ ^[A-Za-z0-9._:/-]{1,100}$ ]] ;;
    YOUJU_AI_REQUEST_EXTRA) [ "${#value}" -le 2000 ] && json_object "$value" ;;
    *) return 1 ;;
  esac
}

echo '粘贴服务器 show ai 打出来的"名称=值"几行，粘完再按一次回车（空行结束）：'
names=()
values=()
n=0
while IFS= read -r line || [ -n "$line" ]; do
  n=$((n + 1))
  line=${line%$'\r'}
  [ -n "$line" ] || break
  # show ai 最后那行括号里的说明（密钥只给指纹）不是配置
  [[ "$line" == "（"* ]] && continue
  name=${line%%=*}
  value=${line#*=}
  # 不认识的行不把内容打出来：万一粘进来的是密钥
  [[ "$line" == *=* && " $NAMES " == *" $name "* ]] \
    || die "第 ${n} 行不认识（只认 ${NAMES}），什么都没写"
  value_ok "$name" "$value" || die "${name} 的格式不对，什么都没写"
  names+=("$name")
  values+=("$value")
done
[ "${#names[@]}" -gt 0 ] || die "没有粘贴到配置，什么都没写"

# 逐项先写临时文件再改名；值经环境变量给 awk，不进命令行参数
for i in "${!names[@]}"; do
  (
    umask 077
    KEY="${names[$i]}" VALUE="${values[$i]}" awk 'BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; done = 0 }
      { sub(/\r$/, "") }
      index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
      { print }
      END { if (!done) print k "=" v }' .env > .env.tmp
  )
  mv -f .env.tmp .env
  echo "已写入 ${names[$i]}=${values[$i]}"
done
echo "运行 docker compose up -d worker 生效"
