#!/bin/sh
# 第 1 批上线（D-126、D-127）：国家级 + 省级全部门，在 Windows 本机（WSL）打开。
# 步骤和核对见 docs/数据来源/全国覆盖/批次/第1批_上线步骤.md。
# 只在前台跑，不要 nohup、不要放后台；用 sh 跑，不靠文件的执行权限。在仓库根目录下：
#   sh backend/deploy/batch1.sh --check   先试算（不改库）
#   sh backend/deploy/batch1.sh --apply   每一步先试算、核对，对不上就停
# 库是 YOUJU_DATABASE_URL 指的那个（本机）。第一行 log=<结论文件>；每一步打一行纯 ASCII 的结论
# （step=… ok=1）；最后一行 batch1_done=1 enabled=<个数>。对不上的网址、审计行、出错经过写进同名的
# -detail.txt。结论文件默认放仓库的 output/batch1/（WSL 的 /tmp 重启会清空）；仓库路径带中文时
# 放 /tmp；要放别处设 BATCH1_LOG_DIR。
set -eu
case "${1:-}" in
  --check|--apply) ;;
  *) echo "usage: batch1.sh --check|--apply"; exit 2 ;;
esac
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
YOUJU=$(command -v youju || echo "$ROOT/backend/.venv/bin/youju")
if [ -n "${BATCH1_LOG_DIR:-}" ]; then
  LOG_DIR=$BATCH1_LOG_DIR
elif printf %s "$ROOT" | LC_ALL=C grep -q '[^ -~]'; then
  LOG_DIR=/tmp
else
  LOG_DIR=$ROOT/output/batch1
fi
exec "$YOUJU" coverage-golive --batch "第1批_国家级和省级" --level 国家级 --level 省级 \
  --by "Claude（主线）" --operator "协调会话" --tag batch1 --log-dir "$LOG_DIR" \
  --out "$ROOT/docs/数据来源/全国覆盖" "$1"
