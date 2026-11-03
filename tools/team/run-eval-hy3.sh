#!/bin/bash
# 在混元 hy3 上跑 AI 解读 30 题评测（AI 对所有人开放前的第 6 道门槛），出打分报告。
#
# 用法（在仓库或它的 worktree 里）：
#   tools/team/run-eval-hy3.sh <云开发 AI 地址> [--out 目录] [--concurrency 1|2|3] [--no-stream]
#   tools/team/run-eval-hy3.sh --self-test [--out 目录]    不要密钥、不联网，用假模型走一遍（自检）
#   tools/team/run-eval-deepseek.sh [--out 目录] [--concurrency 1|2|3] [--no-stream]
#       改在 DeepSeek 官方接口上跑（https://api.deepseek.com，模型 deepseek-v4-pro，关闭思考模式），
#       提示输入 DeepSeek API Key；1.0 改用 DeepSeek 时用这个（类目页登记的是 DeepSeek，创始人 11/2）
#
#   云开发 AI 地址形如 https://<环境ID>.api.tcloudbasegateway.com/v1/ai/cloudbase；也可以不写在命令里，
#   先 export YOUJU_EVAL_HY3_BASE_URL=<地址>。脚本里不写环境 ID。
#   运行后提示输入云开发 API Key：输入时不显示，不写进文件和命令历史，只交给这一次评测进程，用完清掉；
#   没输入就不运行。
#   结果写到 tools/eval-results/hy3-<日期-时间>/（或 --out 给的空目录）：report.md、records.jsonl、
#   summary.json，最后一行打印报告在哪。
#
# 只读：用本机库 youju_eval30（tools/team/fresh-test-db.sh youju_eval30 建的线上公开内容；换库设
# YOUJU_EVAL_DATABASE_URL），不建任务、不写用量表。17 篇通知各调一次模型，默认并发 2、流式打开
# （网关对不走流式、超过 1 分钟的请求不返回；报告照样记下超过 60 秒的次数），一般 10–20 分钟。
# 不要给这个脚本加 set -x：会把密钥打印出来。
set -eu

usage() {
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

ENV_HY3_URL="${YOUJU_EVAL_HY3_BASE_URL:-}"
ENV_HY3_MODEL="${YOUJU_EVAL_HY3_MODEL:-hy3}"
DB_URL="${YOUJU_EVAL_DATABASE_URL:-mysql+pymysql://root@127.0.0.1:33306/youju_eval30?charset=utf8mb4}"
ARG_URL=""
PROVIDER=hy3
OUT=""
CONCURRENCY=2
STREAM=true
SELF_TEST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out)
      shift
      OUT="${1:-}"
      [ -n "$OUT" ] || usage
      ;;
    --concurrency)
      shift
      CONCURRENCY="${1:-}"
      ;;
    --no-stream) STREAM=false ;;
    --self-test) SELF_TEST=1 ;;
    --deepseek) PROVIDER=deepseek ;;
    -h | --help) usage ;;
    -*)
      echo "不认识的参数：$1" >&2
      usage
      ;;
    *) ARG_URL="$1" ;;
  esac
  shift
done
case "$CONCURRENCY" in
  1 | 2 | 3) ;;
  *)
    echo "并发只能是 1、2、3" >&2
    exit 2
    ;;
esac

# DeepSeek 不用外面 shell 里留着的混元地址和模型（YOUJU_EVAL_HY3_*），免得串线
if [ "$PROVIDER" = deepseek ]; then
  BASE_URL="${ARG_URL:-https://api.deepseek.com}"
  MODEL=deepseek-v4-pro
  EXTRA='{"thinking":{"type":"disabled"}}'
  KEY_NAME="DeepSeek API Key"
else
  BASE_URL="${ARG_URL:-$ENV_HY3_URL}"
  MODEL="$ENV_HY3_MODEL"
  EXTRA='{"thinking":{"type":"disabled"}}'
  KEY_NAME="云开发 API Key"
fi

# 评测进程只拿到下面明确设的 YOUJU_ 变量：外面 shell 里留着的（别的密钥、别的库）一概不带进去
for name in $(env | sed -n 's/^\(YOUJU_[A-Za-z0-9_]*\)=.*/\1/p'); do
  unset "$name"
done

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
BACKEND="$REPO/backend"
[ -f "$BACKEND/scripts/eval_questions.py" ] || {
  echo "找不到 backend/scripts/eval_questions.py：要在有据仓库（或它的 worktree）里运行" >&2
  exit 2
}
if [ -z "$OUT" ]; then
  if [ "$SELF_TEST" = 1 ]; then
    OUT="$REPO/tools/eval-results/selftest-$(date +%Y%m%d-%H%M%S)"
  else
    OUT="$REPO/tools/eval-results/$PROVIDER-$(date +%Y%m%d-%H%M%S)"
  fi
fi
# 后面会进 backend/ 运行，相对路径先按当前目录变成绝对路径
case "$OUT" in
  /*) ;;
  *) OUT="$PWD/$OUT" ;;
esac
if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT" 2> /dev/null)" ]; then
  echo "输出目录里已经有文件，换一个：$OUT" >&2
  exit 2
fi

if [ "$SELF_TEST" = 0 ]; then
  if [ -z "$BASE_URL" ]; then
    echo "缺少云开发 AI 地址：写在命令里，或先 export YOUJU_EVAL_HY3_BASE_URL=<地址>" >&2
    exit 2
  fi
  case "$BASE_URL" in
    *[[:space:]]*)
      echo "模型地址里不能有空格" >&2
      exit 2
      ;;
    https://*) ;;
    *)
      echo "模型地址要以 https:// 开头" >&2
      exit 2
      ;;
  esac
  if [ "$PROVIDER" = deepseek ]; then
    case "$BASE_URL" in
      https://api.deepseek.com | https://api.deepseek.com/*) ;;
      *) echo "提醒：地址不是 DeepSeek 官方接口（https://api.deepseek.com），照样继续" >&2 ;;
    esac
  else
    case "$BASE_URL" in
      https://*.api.tcloudbasegateway.com/*) ;;
      *) echo "提醒：地址不像云开发 AI 网关（https://<环境ID>.api.tcloudbasegateway.com/v1/ai/cloudbase），照样继续" >&2 ;;
    esac
  fi
fi

KEY=""
trap 'KEY=""; unset KEY' EXIT
if [ "$SELF_TEST" = 0 ]; then
  # 密钥只在这个进程的变量里：不进命令行参数、不写文件；脚本是非交互 shell，不记历史
  printf '请输入%s（输入时不显示，回车结束）：' "$KEY_NAME" >&2
  IFS= read -r -s KEY || true
  printf '\n' >&2
  KEY="${KEY//[[:space:]]/}"
  if [ -z "$KEY" ]; then
    echo "没有输入密钥，不运行。" >&2
    exit 2
  fi
fi

cd "$BACKEND"
if [ ! -x .venv/bin/python ]; then
  uv sync --quiet --frozen
fi
PY=(uv run --no-sync --quiet python scripts/eval_questions.py)

echo "核对题目集和本机库（不调模型）……" >&2
YOUJU_DATABASE_URL="$DB_URL" "${PY[@]}" --check

if [ "$SELF_TEST" = 1 ]; then
  YOUJU_DATABASE_URL="$DB_URL" "${PY[@]}" --fake-model --out "$OUT" --concurrency "$CONCURRENCY"
else
  # 变量名后面紧跟中文时要加花括号：macOS 的 bash 3.2 在 UTF-8 语言设置下会把中文的第一个字节读进变量名
  # （"$MODEL，" → "MODEL\xef: unbound variable"，11/3 创始人第一次跑时遇到）
  echo "开始评测：模型 ${MODEL}，并发 ${CONCURRENCY}，流式 ${STREAM}" >&2
  # 线路只设给这一个子进程。附加参数：两家都关掉思考模式（和线上一致）。DeepSeek 用 deepseek-v4-pro：
  # 官网价格页（11/3）只列 deepseek-flash 和它、默认开思考；线上后台核对一直用它加关闭思考，D-114 对外 AI 也用它
  YOUJU_DATABASE_URL="$DB_URL" \
    YOUJU_AI_ADVISOR_BASE_URL="$BASE_URL" \
    YOUJU_AI_ADVISOR_API_KEY="$KEY" \
    YOUJU_AI_ADVISOR_MODEL="$MODEL" \
    YOUJU_AI_ADVISOR_REQUEST_EXTRA="$EXTRA" \
    YOUJU_AI_ADVISOR_STREAM="$STREAM" \
    YOUJU_AI_ADVISOR_TIMEOUT_SECONDS=180 \
    "${PY[@]}" --out "$OUT" --concurrency "$CONCURRENCY"
fi
KEY=""
unset KEY
echo "报告在：$OUT/report.md" >&2
