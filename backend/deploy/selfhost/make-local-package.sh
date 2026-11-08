#!/bin/sh
# 打 Windows 主机用的"本地端安装包"（创始人只能 UU 远程，没有镜像仓库时用它）：
#   youju-local-<提交号>/
#     setup-wsl.ps1、setup-wsl.sh        一键安装（WSL 方式，推荐；不要 Docker Desktop）
#     setup.ps1                           一键安装（Docker Desktop 方式，要 Windows 11 23H2 以上）
#     docker-compose.yml、env.example、mysql.cnf
#     source/                             程序源码（只有构建镜像要用的文件）
#     youju-sync-export.jsonl.gz          （可选）起步用的公开数据
# 打成 zip 后用 UU 远程传到 Windows，解压到数据盘的 E:\youju\local，运行 setup-wsl.ps1：用 source/
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
cp "$here/local/setup-wsl.ps1" "$here/local/setup-wsl.sh" "$here/local/setup.ps1" \
  "$here/local/docker-compose.yml" "$here/local/env.example" "$here/local/mysql.cnf" "$pkg/"
# 只放构建镜像要用的、已提交的文件（和根目录 .dockerignore 的白名单一致）
git -C "$root" archive HEAD Dockerfile .dockerignore backend/pyproject.toml backend/uv.lock \
  backend/README.md backend/alembic.ini backend/alembic backend/src backend/deploy "docs/数据来源" \
  | tar -x -C "$pkg/source"
echo "$sha" > "$pkg/source/VERSION"
if [ "${2:-}" != "" ]; then
  cp "$2" "$pkg/youju-sync-export.jsonl.gz"
fi
# 用 Python 打 zip：中文文件名（docs/数据来源）会标成 UTF-8。macOS 自带的 zip 不标，
# Windows 解压时按本机编码（GBK）读，文件夹名成了乱码，构建时就找不到来源清单
(cd "$out_dir" && rm -f "youju-local-$sha.zip" && python3 -I - "youju-local-$sha" <<'EOF'
import os
import sys
import zipfile

root = sys.argv[1]
with zipfile.ZipFile(root + ".zip", "w") as z:
    for folder, subfolders, files in os.walk(root):
        subfolders.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            # 已经压缩过的（起步数据 .gz）直接存，不再压一遍
            kind = zipfile.ZIP_STORED if name.endswith(".gz") else zipfile.ZIP_DEFLATED
            z.write(path, path, compress_type=kind)
with zipfile.ZipFile(root + ".zip") as z:
    unmarked = [i.filename for i in z.infolist() if not i.filename.isascii() and not i.flag_bits & 0x800]
if unmarked:
    sys.exit(f"这些文件名没标 UTF-8：{unmarked[:3]}")
EOF
)
echo "已打包：$out_dir/youju-local-$sha.zip"
