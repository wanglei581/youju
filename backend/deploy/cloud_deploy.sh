#!/bin/sh
# 在本机用微信云托管 CLI 发布一个服务（部署文档第四节"Claude（创始人开通后）"用）。
# 前提：创始人已在控制台生成 CLI 密钥，并在这台电脑上运行过 wxcloud login。
#
# 用法（在仓库里任意目录）：
#   backend/deploy/cloud_deploy.sh <环境ID> <admin|api> [git 版本，默认 HEAD] [--dry-run] [--skip-main-check]
#   --dry-run          只准备要上传的目录，列出文件和大小，不发布
#   --skip-main-check  不核对是不是 main 上的版本、缺不缺迁移（只在回退到旧版本、GitHub 连不上又必须发、
#                      或创始人同意发没合并的版本时用）
#
# 只上传镜像需要的文件：清单直接取 .dockerignore 的白名单（以 ! 开头的行，和云端构建用的是同一份），
# 用 git archive 取出这个版本里已提交的这些文件（本机的 .env、虚拟环境、数据库目录不会带上），
# 放到临时目录里；发布前再查一遍有没有像密钥的内容，有就停下。
# 上传目录里不放 .dockerignore：目录里本来就只有白名单文件；而 wxcloud CLI（2.3.3）会读它、
# 却把 "!backend/src/**" 这类例外规则转换错（变成 "**/!backend/src/**"），代码目录会被当成要忽略的。
# 环境变量（数据库密码、核对台口令、密钥）不经过这个脚本，只由创始人在云托管控制台填写，
# 所以不用 --envParams（它会把变量写进服务设置）。
# 发布顺序：先 admin（启动时迁移数据库），再 api。
set -eu

usage() {
  echo "用法：$0 <环境ID> <admin|api> [git 版本] [--dry-run] [--skip-main-check]" >&2
  exit 1
}

[ $# -ge 2 ] || usage
ENV_ID=$1
SERVICE=$2
shift 2
REV=HEAD
REV_GIVEN=0
DRY_RUN=0
SKIP_MAIN_CHECK=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --skip-main-check) SKIP_MAIN_CHECK=1 ;;
    -*) echo "不认识的参数：$arg" >&2; usage ;;
    *)
      if [ "$REV_GIVEN" = 1 ]; then
        echo "只能给一个 git 版本（多出来的：${arg}）" >&2
        usage
      fi
      REV=$arg
      REV_GIVEN=1
      ;;
  esac
done
case "$SERVICE" in
  admin|api) ;;
  *) echo "服务名只能是 admin 或 api" >&2; exit 1 ;;
esac
case "$ENV_ID" in
  ""|-*) echo "环境 ID 不对：$ENV_ID" >&2; exit 1 ;;
esac

ROOT=$(git rev-parse --show-toplevel)
SHA=$(git -C "$ROOT" rev-parse --verify --end-of-options "$REV^{commit}")

# 发布前核对（差距清单 R4）：admin 启动时库里的迁移号比代码新只提醒、照常启动（这样回退时
# 起得来），所以发错版本要在这里拦下。先取最新的 main（取不到就停），要发的版本必须已经合进 main，
# 而且 main 上的迁移文件一个都不缺、内容也一样（--no-renames：改了编号的也算缺；文件名没变、
# 里面的版本号或上一个版本号改过的算不一样）。
# 已知的多拦：发的不是最新的 main、main 上又新合并了还没发布过的迁移时也会停，这时发最新的 main，
# 或确认后加 --skip-main-check。
if [ "$SKIP_MAIN_CHECK" = 1 ]; then
  echo "注意：没有核对是不是 main 上的版本、缺不缺迁移（--skip-main-check）" >&2
else
  if ! git -C "$ROOT" fetch -q origin main; then
    echo "取不到最新的 main（GitHub 连不上？），没有发布" >&2
    exit 1
  fi
  if ! git -C "$ROOT" merge-base --is-ancestor "$SHA" origin/main; then
    echo "版本 $SHA 还没合并进 main，没有发布（只发 main 上的版本）" >&2
    exit 1
  fi
  MIGRATIONS=backend/alembic/versions
  MISSING=$(git -C "$ROOT" diff --no-renames --name-only --diff-filter=D origin/main "$SHA" -- "$MIGRATIONS")
  CHANGED=$(git -C "$ROOT" diff --no-renames --name-only --diff-filter=M origin/main "$SHA" -- "$MIGRATIONS")
  if [ -n "$MISSING$CHANGED" ]; then
    echo "这个版本的迁移和 main 上的对不上，没有发布（多半是发错了版本）：" >&2
    [ -z "$MISSING" ] || { echo "缺：" >&2; echo "$MISSING" >&2; }
    [ -z "$CHANGED" ] || { echo "内容和 main 上的不一样：" >&2; echo "$CHANGED" >&2; }
    echo "确实要发这个旧版本（例如回退）时加 --skip-main-check；删表删列的迁移回退前要先降库，见《发版与回退》" >&2
    exit 1
  fi
fi

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# 白名单：.dockerignore 里以 ! 开头的行，去掉结尾的 /**（这个版本里的那份）
git -C "$ROOT" show "$SHA:.dockerignore" > "$STAGE/.dockerignore.list"
PATHS=$(sed -n 's/^!//p' "$STAGE/.dockerignore.list" | sed 's#/\*\*$##')
rm "$STAGE/.dockerignore.list"
[ -n "$PATHS" ] || { echo ".dockerignore 里没有白名单" >&2; exit 1; }

# 分两步，任何一步失败都停下（sh 没有 pipefail）
OLD_IFS=$IFS
IFS='
'
# shellcheck disable=SC2086 # 按行拆开白名单，路径里有中文、没有换行
git -C "$ROOT" archive -o "$STAGE/src.tar" "$SHA" $PATHS
IFS=$OLD_IFS
tar -x -f "$STAGE/src.tar" -C "$STAGE"
rm "$STAGE/src.tar"
# 镜像内保留完整来源提交；探针不依赖部署时手填环境变量。
python3 "$ROOT/tools/release/write_build.py" --revision "$SHA" --output "$STAGE/backend/src/youju/_build.json"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +

# 发布前查一遍：像密钥、口令、机器人地址、带口令的连接地址（任何数据库或服务）的内容一律不上传
if grep -rIlE \
  -e '(YOUJU_[A-Z_]*(TOKEN|KEY|PASSWORD|SECRET|WEBHOOK))[[:space:]]*=[[:space:]]*[^[:space:]$]' \
  -e 'sk-[A-Za-z0-9]{20,}' \
  -e 'BEGIN [A-Z ]*PRIVATE KEY' \
  -e 'qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9-]{8,}' \
  -e '[A-Za-z][A-Za-z0-9+.-]*://[^:/@[:space:]]+:[^@<[:space:]]+@' \
  "$STAGE"; then
  echo "上面这些文件里有像密钥或口令的内容，没有发布。先确认并从提交里去掉" >&2
  exit 1
fi

echo "版本 ${SHA}，要上传的文件 $(find "$STAGE" -type f | wc -l | tr -d ' ') 个，共 $(du -sh "$STAGE" | cut -f1)"
if [ "$DRY_RUN" = 1 ]; then
  (cd "$STAGE" && find . -type f | sort)
  exit 0
fi

cd "$STAGE"
wxcloud run:deploy . --envId="$ENV_ID" --serviceName="$SERVICE" \
  --targetDir=. --dockerfile=Dockerfile --containerPort=80 \
  --releaseType=FULL --remark="$SERVICE $SHA" --noConfirm --detach
# --detach：提交后直接返回。9/29 首次发布时 CLI 拉部署日志一直报 "topic not exist" 卡住不退出；
# 构建和上线进度用下面这条命令看（build_failed 构建失败、normal 已上线）
echo "已提交。查看进度：wxcloud version:list -e $ENV_ID -s $SERVICE"
