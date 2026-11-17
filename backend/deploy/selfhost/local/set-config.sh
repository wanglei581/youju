#!/bin/bash
# 本机端（Windows 主机的 WSL 发行版 youju）录不涉密的配置：后台 AI 的线路、模型、附加参数（和服务器一样），
# 和本机自己的开关。后台 AI（截止日期交叉核对、分类、申报条件抽取）在本机跑，要和服务器用同一条线路、同一个模型：
#   1. 服务器网页终端：sudo /opt/youju/bin/youju-shared.sh show ai，复制打出来的全部几行（含最后一行括号里的说明）；
#   2. UU 远程的终端：wsl -d youju -u root --cd /opt/youju/local -e bash set-config.sh [开关=0或1 …]，粘贴（不显示），
#      读到括号那行就结束（空行不算；没复制全按 Ctrl-C 取消，什么都不写）；
#   3. 密钥另用 set-secret.sh YOUJU_AI_API_KEY 录；都录完 docker compose up -d worker 生效（在同一个目录）。
# 粘贴的内容就是 AI 这几项在本机的全部：服务器没填的，本机也去掉（用程序默认的，和服务器一样）。
# 开关（服务器上没有，show ai 不打）：只收 0 或 1，写在命令后面或粘贴里都行，给了才改、没给的原样留着；
# 只改开关、不粘贴：bash set-config.sh --only YOUJU_DEADLINE_AUTO_VERIFY=0。
# 只认下面几项；有一项不认识、格式不对，或者没粘到最后那行说明（可能没复制全），整批都不写。
# 输入不显示、AI 这几项只回名称和长度：万一粘进来的是密钥，也不会打到屏幕上或进命令行参数。
set -euo pipefail
cd "$(dirname "$0")"

NAMES="YOUJU_AI_BASE_URL YOUJU_AI_MODEL YOUJU_AI_BACKGROUND_MODEL YOUJU_AI_REQUEST_EXTRA"
# 本机自己的开关：YOUJU_DEADLINE_AUTO_VERIFY 截止日期自动核对（D-135），没配当 0
SWITCHES="YOUJU_DEADLINE_AUTO_VERIFY"
die() { echo "错误：$*" >&2; exit 1; }

[ -t 0 ] && [ -t 1 ] || die "只能在交互终端里用"
[ -f .env ] || die "还没有 .env：先跑一遍 setup-wsl.ps1"

# 附加参数要是一个 JSON 对象（程序启动时按字典读，读不了 worker 就起不来）；值从标准输入给，不进命令行参数
json_object() {
  local check='import json, sys; sys.exit(0 if isinstance(json.load(sys.stdin), dict) else 1)'
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$1" | python3 -I -c "$check" 2>/dev/null
  else
    printf '%s' "$1" | docker compose run --rm --no-deps -T worker python -I -c "$check" >/dev/null 2>&1
  fi
}

# 值要能不加引号写成一行、原样读回来：docker compose 读 .env 时 $ 会被当成变量、" #" 后面当注释
value_ok() {
  local name=$1 value=$2 port
  [[ "$value" != *'$'* && "$value" != *' #'* && "$value" != \"* && "$value" != \'* ]] || return 1
  case "$name" in
    YOUJU_AI_BASE_URL)
      [[ "$value" =~ ^https://[A-Za-z0-9.-]+(:([0-9]{1,5}))?(/[A-Za-z0-9._~/-]*)?$ ]] || return 1
      port=${BASH_REMATCH[2]}
      [ -z "$port" ] || { [ $((10#$port)) -ge 1 ] && [ $((10#$port)) -le 65535 ]; }
      ;;
    YOUJU_AI_MODEL | YOUJU_AI_BACKGROUND_MODEL) [[ "$value" =~ ^[A-Za-z0-9._:/-]{1,100}$ ]] ;;
    YOUJU_AI_REQUEST_EXTRA) [ "${#value}" -le 2000 ] && json_object "$value" ;;
    YOUJU_DEADLINE_AUTO_VERIFY) [[ "$value" == 0 || "$value" == 1 ]] ;;
    *) return 1 ;;
  esac
}

# 要写的几项：名称和值放在两个同序的数组里（不用关联数组：macOS 自带的 bash 3.2 也要能跑测试）
wnames=()
wvalues=()
# 这一项在 wnames 里的位置；没有打 -1
pos() {
  local i
  for i in ${wnames[@]+"${!wnames[@]}"}; do
    [ "${wnames[$i]}" = "$1" ] && { echo "$i"; return; }
  done
  echo -1
}
# 记下一项；同一项给了两次以后面的为准
remember() {
  local i
  i=$(pos "$1")
  if [ "$i" -ge 0 ]; then
    wvalues[$i]=$2
  else
    wnames+=("$1")
    wvalues+=("$2")
  fi
}

# 命令后面的参数：--only，和开关（命令里的开关在粘贴之后算，以命令里的为准）
only=0
args=()
for arg in "$@"; do
  if [ "$arg" = "--only" ]; then
    only=1
    continue
  fi
  name=${arg%%=*}
  # 不认识的参数不打出来：万一敲进来的是密钥
  [[ "$arg" == *=* && " $SWITCHES " == *" $name "* ]] \
    || die "参数不认识（只认 --only 和 ${SWITCHES}=0 或 1），什么都没写"
  value_ok "$name" "${arg#*=}" || die "${name} 只能是 0 或 1，什么都没写"
  args+=("$arg")
done

if [ "$only" -eq 1 ]; then
  [ "${#args[@]}" -gt 0 ] || die "--only 要和开关一起给，如 --only YOUJU_DEADLINE_AUTO_VERIFY=0"
else
  printf '%s' '粘贴服务器 show ai 打出来的全部几行（含最后一行括号里的说明；粘贴的内容不显示，读到括号那行就结束，没复制全可以按 Ctrl-C 取消）：'
  complete=0
  n=0
  while IFS= read -rs line || [ -n "$line" ]; do
    n=$((n + 1))
    line=${line%$'\r'}
    # show ai 最后一行是括号里的说明（密钥只给指纹）：读到它就是复制全了
    if [[ "$line" == "（"* ]]; then
      complete=1
      break
    fi
    # 空行跳过：粘贴时换行可能变成两个（回车加换行）
    [ -n "$line" ] || continue
    name=${line%%=*}
    value=${line#*=}
    # 不认识的行不把内容打出来：万一粘进来的是密钥
    [[ "$line" == *=* && " $NAMES $SWITCHES " == *" $name "* ]] \
      || die "第 ${n} 行不认识（只认 ${NAMES} ${SWITCHES}），什么都没写"
    value_ok "$name" "$value" || die "${name} 的格式不对，什么都没写"
    remember "$name" "$value"
  done
  echo
  [ "$complete" -eq 1 ] || die "没粘到最后一行括号里的说明（可能没复制全），什么都没写"
fi
for arg in ${args[@]+"${args[@]}"}; do
  remember "${arg%%=*}" "${arg#*=}"
done

# 整份 .env 先写进临时文件，一次改名：给了的几项照写（原来有的就地改、没有的加在最后，重复的旧行去掉）；
# 粘贴时 AI 那几项没给的也去掉（和服务器一样用默认的）；开关没给的、--only 时 AI 那几项都原样留着；
# 别的行原样留下（CRLF 改成 LF）
(
  umask 077
  written=" "
  {
    while IFS= read -r l || [ -n "$l" ]; do
      l=${l%$'\r'}
      key=${l%%=*}
      if [[ "$l" == *=* && " $NAMES $SWITCHES " == *" $key "* ]]; then
        i=$(pos "$key")
        if [ "$i" -ge 0 ]; then
          if [[ "$written" != *" $key "* ]]; then
            printf '%s=%s\n' "$key" "${wvalues[$i]}"
            written="${written}${key} "
          fi
          continue
        fi
        if [ "$only" -eq 0 ] && [[ " $NAMES " == *" $key "* ]]; then
          continue
        fi
      fi
      printf '%s\n' "$l"
    done < .env
    for i in ${wnames[@]+"${!wnames[@]}"}; do
      [[ "$written" == *" ${wnames[$i]} "* ]] || printf '%s=%s\n' "${wnames[$i]}" "${wvalues[$i]}"
    done
  } > .env.tmp
)
mv -f .env.tmp .env
if [ "$only" -eq 0 ]; then
  for key in $NAMES; do
    i=$(pos "$key")
    if [ "$i" -ge 0 ]; then
      echo "已写入 ${key}（${#wvalues[$i]} 位）"
    else
      echo "${key}：服务器没填，本机也不填（用程序默认的）"
    fi
  done
fi
for key in $SWITCHES; do
  i=$(pos "$key")
  [ "$i" -ge 0 ] || continue
  if [ "${wvalues[$i]}" = 1 ]; then
    echo "已写入 ${key}=1（截止日期自动核对：开）"
  else
    echo "已写入 ${key}=0（截止日期自动核对：关）"
  fi
done
echo "运行 docker compose up -d worker 生效"
