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
# 新版 grok 的 plain 输出只打印第一段话；用流式 JSON，取最后的 result 字段作为结果。
(cd "$TMP" && grok --cwd "$TMP" --model grok-4.7-build-fast --effort xhigh --permission-mode plan \
  --max-turns "$TURNS" --output-format streaming-messages-json $WEB -p "$(cat "$R/prompt.txt")" \
  > "$R/stream.jsonl" 2> "$R/log.txt")
code=$?
python3 - "$R" <<'PY2'
import json, sys, pathlib
r = pathlib.Path(sys.argv[1])
result = ""
for line in (r / "stream.jsonl").read_text(encoding="utf-8", errors="ignore").splitlines():
    try:
        d = json.loads(line)
    except ValueError:
        continue
    if d.get("type") == "result":
        result = d.get("result") or ""
(r / "out.md").write_text(result, encoding="utf-8")
PY2
rmdir "$TMP" 2>/dev/null
if grep -q "402\|Payment Required\|usage balance" "$R/log.txt" "$R/out.md" 2>/dev/null; then
  echo "grok 额度不足（402）"; exit 2
fi
[ -s "$R/out.md" ] && { echo "exit $code (grok)"; cat "$R/out.md"; exit 0; }
echo "grok 没有输出（exit $code）"; tail -3 "$R/log.txt"; exit 1
