#!/bin/sh
# 无界面调用 agy 的团队脚本。用法：run-agy.sh <材料目录>
#   材料目录里放 prompt.txt（材料全部写在提示词里：agy 无界面模式不能运行命令、读不了文件，
#   提示词开头写"不要运行任何命令、不要读取文件、不要上网"），结果写到 out.md。
R=$(cd "$1" && pwd)
TMP=$(mktemp -d)
(cd "$TMP" && agy --model gemini-3.8-flash-high --effort high --mode plan --output-format text \
  --print-timeout 20m -p "$(cat "$R/prompt.txt")" > "$R/out.md" 2> "$R/err.txt")
code=$?
rmdir "$TMP" 2>/dev/null
[ -s "$R/out.md" ] && { echo "exit ok (agy)"; cat "$R/out.md"; exit 0; }
echo "agy 没有输出（exit $code）"; tail -3 "$R/err.txt"; exit 1
