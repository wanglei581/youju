#!/bin/sh
# 在本机用微信云托管 CLI 发布一个服务（部署文档第四节"Claude（创始人开通后）"用）。
# 前提：创始人已在控制台生成 CLI 密钥，并在这台电脑上运行过 wxcloud login。
#
# 用法（在仓库里任意目录）：
#   backend/deploy/cloud_deploy.sh <环境ID> <admin|api> [git 版本，默认 HEAD] [--dry-run]
#   --dry-run  只准备要上传的目录，列出文件和大小，不发布
#
# 只上传镜像需要的文件：用 git archive 取出这个版本里已提交的文件（本机的 .env、虚拟环境、
# 数据库目录都不会带上），范围和 .dockerignore 的白名单一致，放到临时目录里发布。
# 环境变量（数据库密码、核对台口令、密钥）不经过这个脚本，只由创始人在云托管控制台填写，
# 所以不用 --envParams（它会把变量写进服务设置）。
# 发布顺序：先 admin（启动时迁移数据库），再 api。
set -eu

if [ $# -lt 2 ]; then
  echo "用法：$0 <环境ID> <admin|api> [git 版本] [--dry-run]" >&2
  exit 1
fi
ENV_ID=$1
SERVICE=$2
REV=HEAD
DRY_RUN=0
shift 2
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) REV=$arg ;;
  esac
done
case "$SERVICE" in
  admin|api) ;;
  *) echo "服务名只能是 admin 或 api" >&2; exit 1 ;;
esac

ROOT=$(git rev-parse --show-toplevel)
SHA=$(git -C "$ROOT" rev-parse --short "$REV")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
git -C "$ROOT" archive "$REV" \
  Dockerfile .dockerignore \
  backend/pyproject.toml backend/uv.lock backend/README.md backend/alembic.ini \
  backend/alembic backend/src backend/deploy "docs/数据来源" \
  | tar -x -C "$STAGE"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +

echo "版本 $SHA，要上传的文件 $(find "$STAGE" -type f | wc -l | tr -d ' ') 个，共 $(du -sh "$STAGE" | cut -f1)"
if [ "$DRY_RUN" = 1 ]; then
  (cd "$STAGE" && find . -type f | sort)
  exit 0
fi

cd "$STAGE"
wxcloud run:deploy . --envId="$ENV_ID" --serviceName="$SERVICE" \
  --targetDir=. --dockerfile=Dockerfile --containerPort=80 \
  --releaseType=FULL --remark="$SERVICE $SHA" --noConfirm
