#!/bin/sh
# 无界面调用 Grok 的团队脚本。用法：run-grok.sh <材料目录> [web|noweb] [最多轮数]
#   材料目录里放 prompt.txt，结果写到 out.md，日志 log.txt。
#   只读：工作目录用空的临时目录；提示词第一行写“只读：不要创建、修改或删除任何文件”。
set -u
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
R=$(cd "$1" && pwd) || exit 1
[ -s "$R/prompt.txt" ] || { echo "缺少 prompt.txt" >&2; exit 1; }
MODE=${2:-web}
TURNS=${3:-40}
case "$MODE" in web|noweb) ;; *) echo "模式只能是 web 或 noweb" >&2; exit 1 ;; esac
TMP=$(mktemp -d) || exit 1
# 在调用之前清空旧结果；中断也不会留下伪成功。
: > "$R/out.md"
printf '{"ok":false,"state":"running"}\n' > "$R/status.json"
WEB=""
[ "$MODE" = "noweb" ] && WEB="--disable-web-search"
# 新版 grok 的 plain 输出只打印第一段话；用流式 JSON，取最后的 result 字段作为结果。
(cd "$TMP" && grok --cwd "$TMP" --model grok-4.7-build-fast --effort xhigh --permission-mode plan \
  --max-turns "$TURNS" --output-format streaming-messages-json $WEB -p "$(cat "$R/prompt.txt")" \
  > "$R/stream.jsonl" 2> "$R/log.txt")
code=$?
rmdir "$TMP" 2>/dev/null
python3 "$SCRIPT_DIR/grok_result.py" "$R" "$code"
