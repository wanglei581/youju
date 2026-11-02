#!/bin/sh
# 无界面调用 Codex 的团队脚本（Claude 统筹时用）。
# 用法：run-codex.sh <材料目录> [read-only|workspace-write] [工作目录]
#   材料目录里放 prompt.txt，结果写到 out.md、日志写到 log.txt；工作目录默认就是材料目录
#   （开发任务传它自己的 git worktree）。
# 模型（D-103，10/28 创始人改）：gpt-6.1-terra，推理强度 high，可联网检索；超载时换模型（D-024）：
#   gpt-6.1-terra → gpt-6-sol → gpt-6.1-sol → gpt-6-astra → gpt-6-luna；都超载就等 60 秒从头再试；网络中断也等 60 秒再试；
#   最多 5 轮。登录失效（401）直接报出。编号直接写 gpt-6 不行：ChatGPT 账号下的 Codex 不认。
# 开发任务要访问本机测试库时：CODEX_EXTRA='-c sandbox_workspace_write.network_access=true'
R=$1
MODE=${2:-read-only}
WORKDIR=${3:-$R}
MODELS="gpt-6.1-terra gpt-6-sol gpt-6.1-sol gpt-6-astra gpt-6-luna"
EFFORT=${CODEX_EFFORT:-high}
# 用哪个 codex：默认用 PATH 里的；它坏了（如 npm 版缺平台组件）就用 ChatGPT 应用自带的那份
CODEX_BIN=${CODEX_BIN:-codex}
if ! "$CODEX_BIN" --version > /dev/null 2>&1; then
  CODEX_BIN="/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
fi
for round in 1 2 3 4 5; do
  for m in $MODELS; do
    : > "$R/out.md"
    "$CODEX_BIN" --search exec --ignore-user-config -m "$m" -c model_reasoning_effort="\"$EFFORT\"" -s "$MODE" $CODEX_EXTRA \
      --skip-git-repo-check --ephemeral -C "$WORKDIR" -o "$R/out.md" "$(cat "$R/prompt.txt")" \
      < /dev/null > "$R/log.txt" 2>&1
    if [ -s "$R/out.md" ]; then
      echo "exit ok (model $m, round $round)"
      cat "$R/out.md"
      # 结果末尾没换行时补一个：外面追加的 exit= 才会单独成行（10/18 有个等 "^exit=" 的循环因此空转了 4 小时）
      [ -n "$(tail -c1 "$R/out.md")" ] && echo
      exit 0
    fi
    if grep -q "401 Unauthorized" "$R/log.txt"; then
      echo "codex 未登录或登录失效（401），需要创始人重新登录"
      exit 1
    fi
    if grep -qiE "at capacity|overloaded|server is busy|too many requests|rate limit" "$R/log.txt"; then
      echo "模型 $m 超载，换下一个"
      continue
    fi
    if grep -qiE "reconnecting|stream disconnected|error sending request|routing discovery failed|timed out" "$R/log.txt"; then
      echo "网络中断（模型 ${m}），60 秒后重试"
      break
    fi
    echo "failed (model $m)"
    tail -5 "$R/log.txt"
    exit 1
  done
  sleep 60
done
echo "5 轮都没拿到结果"
exit 1
