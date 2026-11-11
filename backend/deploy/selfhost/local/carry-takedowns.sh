#!/bin/bash
# 首发前搬家（D-127）：把云托管上已下架的网址带到本机（公开数据的主库，手册第六节）。
# 下架清单在服务器上（youju-shared.sh takedowns 打印：gzip 后 base64）。在发行版里：
#   cd /opt/youju/local && bash carry-takedowns.sh <清单的 SHA-256> <<'EOF'
#   （takedowns 打印的 base64 各行）
#   EOF
# 核对 SHA-256 → youju takedown-carry apply（写拦截表，本机已有的同网址文件整组下架）→ check（还有
# 对用户可见的就退出码 1：多半是抓取正好同时收进来一份，再跑一次）。下架状态随下一轮同步（10 分钟内）
# 推到服务器，再在服务器上 youju-shared.sh takedowns-check 核对。可以重复执行。
set -euo pipefail
cd "$(dirname "$0")"

sum=${1:-}
[[ "$sum" =~ ^[0-9a-f]{64}$ ]] || { echo "用法：bash carry-takedowns.sh <SHA-256> <<'EOF' … EOF" >&2; exit 1; }
trap 'rm -f takedowns.jsonl.tmp' EXIT
tr -d '\r' | base64 -d | gunzip > takedowns.jsonl.tmp
echo "${sum}  takedowns.jsonl.tmp" | sha256sum -c - >/dev/null \
  || { rm -f takedowns.jsonl.tmp; echo "SHA-256 对不上：抄的内容不完整，没有应用" >&2; exit 1; }
mv -f takedowns.jsonl.tmp takedowns.jsonl
echo "下架清单 $(wc -l < takedowns.jsonl) 个网址，SHA-256 对上了"
docker compose run --rm --no-deps -v "$PWD:/move:ro" worker \
  youju takedown-carry apply --file /move/takedowns.jsonl
docker compose run --rm --no-deps -v "$PWD:/move:ro" worker \
  youju takedown-carry check --file /move/takedowns.jsonl
echo "CARRY OK：下一轮同步（10 分钟内）推到服务器后，在服务器上运行 youju-shared.sh takedowns-check"
