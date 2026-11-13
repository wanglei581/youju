#!/bin/bash
# 把服务器上的下架清单带到本机（公开数据的主库），两种：
# - 首发前搬家（D-127，手册第六节第 8 步）：云托管上已下架的网址（youju-shared.sh takedowns 打印）；
# - 紧急下架（手册第八节）：电脑不在线时在服务器上紧急下架的（youju-shared.sh emergency-carry 打印）。
# 都是 gzip 后 base64，可能分几段。在发行版里：
#   cd /opt/youju/local && bash carry-takedowns.sh <清单的 SHA-256> [emergency] <<'EOF'
#   （各段的 base64 行按顺序全部贴上；每段第一行的中文说明贴上了也没关系，会跳过）
#   EOF
# 也可以先存成文件：bash carry-takedowns.sh <SHA-256> [emergency] < 文件
# 核对 SHA-256 → youju takedown-carry apply（搬家的写拦截表、本机已有的同网址文件整组下架；紧急下架的
# 只把本机已有的整组正式下架，不写拦截表）→ check（还有对用户可见的就退出码 1：多半是抓取正好同时
# 收进来一份，再跑一次）→ 马上推一次同步（定时任务正在推就算了，10 分钟内会推），再到服务器上核对
# （搬家的 takedowns-check；紧急下架的 emergency-settle、emergency-list）。可以重复执行。
set -euo pipefail
cd "$(dirname "$0")"

sum=${1:-}
origin=${2:-d127}
if ! [[ "$sum" =~ ^[0-9a-f]{64}$ ]] || ! [[ "$origin" =~ ^(d127|emergency)$ ]]; then
  echo "用法：bash carry-takedowns.sh <SHA-256> [emergency] <<'EOF' … EOF" >&2
  exit 1
fi
if [ "$origin" = emergency ]; then
  # 和搬家的清单分开存：不盖掉那一份（搬家的可以重跑）
  file=emergency-takedowns.jsonl
  verify="在服务器上运行 youju-shared.sh emergency-settle，再 emergency-list 看紧急下架是不是都撤了"
else
  file=takedowns.jsonl
  verify="在服务器上运行 youju-shared.sh takedowns-check"
fi
trap 'rm -f "$file.tmp"' EXIT
# 只留 base64 的行（说明行、空行跳过；网页终端复制出来行尾常带空格，先去掉）
tr -d '\r' | sed -E 's/[[:space:]]+$//' | { grep -E '^[A-Za-z0-9+/=]+$' || true; } | base64 -d | gunzip \
  > "$file.tmp"
echo "${sum}  $file.tmp" | sha256sum -c - >/dev/null \
  || { rm -f "$file.tmp"; echo "SHA-256 对不上：抄的内容不完整，没有应用" >&2; exit 1; }
mv -f "$file.tmp" "$file"
echo "下架清单 $(wc -l < "$file") 个网址，SHA-256 对上了"
docker compose run --rm --no-deps -v "$PWD:/move:ro" worker \
  youju takedown-carry apply --file "/move/$file" --origin "$origin"
docker compose run --rm --no-deps -v "$PWD:/move:ro" worker \
  youju takedown-carry check --file "/move/$file"
# 同一时间只推一路（youju-sync-push 锁）：定时任务正在推时这里直接退出，等下一轮就行；
# 别的原因没推成要看上面的错误，修好了再推
if out=$(docker compose exec -T worker youju sync-push 2>&1); then
  printf '%s\n' "$out"
  echo "CARRY OK：已经推到服务器，${verify}"
elif printf '%s' "$out" | grep -q "另一个同步正在推"; then
  echo "CARRY OK（本机已应用）：定时任务正在推，10 分钟内会推到服务器，再${verify}"
else
  printf '%s\n' "$out"
  echo "本机已应用，但推到服务器没成功（看上面的错误）：修好后运行 docker compose exec -T worker youju sync-push" >&2
  exit 1
fi
