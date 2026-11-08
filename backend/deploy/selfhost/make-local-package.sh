#!/bin/sh
# 打 Windows 主机用的"本地端安装包"（创始人只能 UU 远程，没有镜像仓库时用它）：
#   youju-local-<提交号>/
#     setup.ps1、docker-compose.yml、env.example、mysql.cnf   本地端的一键安装和配置
#     source/                                                  程序源码（只有构建镜像要用的文件）
#     youju-sync-export.jsonl.gz                               （可选）起步用的公开数据
# 打成 zip 后用 UU 远程传到 Windows 的 D:\youju\local，运行 setup.ps1：没有镜像文件时它用 source/
# 在 Windows 上构建（Windows 一般是 x86 芯片，苹果芯片的 Mac 上构建的镜像在它上面跑不了）。
# 用法：sh make-local-package.sh <输出目录> [起步用的公开数据文件]
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
out_dir=${1:?要给输出目录}
sha=$(git -C "$root" rev-parse --short=8 HEAD)
pkg="$out_dir/youju-local-$sha"
rm -rf "$pkg"
mkdir -p "$pkg/source"
cp "$here/local/setup.ps1" "$here/local/docker-compose.yml" "$here/local/env.example" \
  "$here/local/mysql.cnf" "$pkg/"
# 只放构建镜像要用的、已提交的文件（和根目录 .dockerignore 的白名单一致）
git -C "$root" archive HEAD Dockerfile .dockerignore backend/pyproject.toml backend/uv.lock \
  backend/README.md backend/alembic.ini backend/alembic backend/src backend/deploy "docs/数据来源" \
  | tar -x -C "$pkg/source"
echo "$sha" > "$pkg/source/VERSION"
if [ "${2:-}" != "" ]; then
  cp "$2" "$pkg/youju-sync-export.jsonl.gz"
fi
(cd "$out_dir" && rm -f "youju-local-$sha.zip" && zip -qr "youju-local-$sha.zip" "youju-local-$sha")
echo "已打包：$out_dir/youju-local-$sha.zip"
