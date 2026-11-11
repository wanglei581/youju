#!/bin/bash
# 首发前搬家（D-127）：把云托管上已下架的网址带到本机（公开数据的主库，手册第六节）。
# 下架清单在服务器上（youju-shared.sh takedowns 打印：gzip 后 base64，可能分几段）。在发行版里：
#   cd /opt/youju/local && bash carry-takedowns.sh <下架清单的 SHA-256> <<'EOF'
#   （各段的 base64 行按顺序全部贴上；每段第一行的中文说明贴上了也没关系，会跳过）
#   EOF
# 也可以先存成文件：bash carry-takedowns.sh <SHA-256> < 文件
# 核对 SHA-256 → youju takedown-carry apply（写拦截表，本机已有的同网址文件整组下架）→ check（还有
# 对用户可见的就退出码 1：多半是抓取正好同时收进来一份，再跑一次）→ 马上推一次同步（定时任务正在推
# 就算了，10 分钟内会推），再在服务器上 youju-shared.sh takedowns-check 核对。可以重复执行。
set -euo pipefail
cd "$(dirname "$0")"

sum=${1:-}
[[ "$sum" =~ ^[0-9a-f]{64}$ ]] || { echo "用法：bash carry-takedowns.sh <SHA-256> <<'EOF' … EOF" >&2; exit 1; }
trap 'rm -f takedowns.jsonl.tmp' EXIT
# 只留 base64 的行（说明行、空行跳过；网页终端复制出来行尾常带空格，先去掉）
tr -d '\r' | sed -E 's/[[:space:]]+$//' | { grep -E '^[A-Za-z0-9+/=]+$' || true; } | base64 -d | gunzip \
  > takedowns.jsonl.tmp
echo "${sum}  takedowns.jsonl.tmp" | sha256sum -c - >/dev/null \
  || { rm -f takedowns.jsonl.tmp; echo "SHA-256 对不上：抄的内容不完整，没有应用" >&2; exit 1; }
mv -f takedowns.jsonl.tmp takedowns.jsonl
echo "下架清单 $(wc -l < takedowns.jsonl) 个网址，SHA-256 对上了"
docker compose run --rm --no-deps -v "$PWD:/move:ro" worker \
  youju takedown-carry apply --file /move/takedowns.jsonl
docker compose run --rm --no-deps -v "$PWD:/move:ro" worker \
  youju takedown-carry check --file /move/takedowns.jsonl
# 同一时间只推一路（youju-sync-push 锁）：定时任务正在推时这里直接退出，不影响
docker compose exec -T worker youju sync-push \
  || echo "这次没推成（定时任务可能正在推），10 分钟内的下一轮会推"
echo "CARRY OK：推到服务器后，在服务器上运行 youju-shared.sh takedowns-check"
