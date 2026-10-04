#!/bin/sh
# 无界面调用 Grok 的团队脚本。用法：run-grok.sh <材料目录> [web|noweb] [最多轮数]
#   材料目录里放 prompt.txt，结果写到 out.md，日志 log.txt。
#   只读：工作目录用空的临时目录；提示词第一行写“只读：不要创建、修改或删除任何文件”。
R=$(cd "$1" && pwd)
MODE=${2:-web}
TURNS=${3:-40}
TMP=$(mktemp -d)
WEB=""
[ "$MODE" = "noweb" ] && WEB="--disable-web-search"
(cd "$TMP" && grok --cwd "$TMP" --model grok-4.7-build-fast --effort xhigh --permission-mode plan \
  --max-turns "$TURNS" --output-format plain $WEB -p "$(cat "$R/prompt.txt")" > "$R/out.md" 2> "$R/log.txt")
code=$?
rmdir "$TMP" 2>/dev/null
if grep -q "402\|Payment Required\|usage balance" "$R/log.txt" "$R/out.md" 2>/dev/null; then
  echo "grok 额度不足（402）"; exit 2
fi
[ -s "$R/out.md" ] && { echo "exit $code (grok)"; cat "$R/out.md"; exit 0; }
echo "grok 没有输出（exit $code）"; tail -3 "$R/log.txt"; exit 1
