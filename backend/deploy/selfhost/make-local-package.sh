#!/bin/sh
# 打 Windows 主机用的"本机端安装包"（创始人只能 UU 远程，没有镜像仓库时用它）。zip 里是一个 local 文件夹：
#   local/
#     setup-wsl.ps1、setup-wsl.sh        一键安装（WSL 方式，推荐；不要 Docker Desktop）
#     setup.ps1                           一键安装（Docker Desktop 方式，要 Windows 11 23H2 以上）
#     docker-compose.yml、env.example、mysql.cnf
#     source/                             程序源码（只有构建镜像要用的文件）
#     youju-sync-export.jsonl.gz          （可选）起步用的公开数据
# 用 UU 远程传到 Windows，解压到数据盘的 youju 文件夹（得到 E:\youju\local），运行 setup-wsl.ps1：用 source/
# 在 Windows 上构建（Windows 一般是 x86 芯片，苹果芯片的 Mac 上构建的镜像在它上面跑不了）。
# 全部文件都取自已提交的 HEAD（包名里的提交号和内容对得上，工作区没提交的改动不会混进去）；
# 给了起步数据时核对它的迁移版本等于源码的最新迁移，不一致就不打包（装到导入那一步必然失败）。
# 用法：sh make-local-package.sh <输出目录> [起步用的公开数据文件]
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
out_dir=$(cd "${1:?要给输出目录}" && pwd)
sha=$(git -C "$root" rev-parse --short=8 HEAD)
work="$out_dir/youju-local-$sha"
pkg="$work/local"
rm -rf "$work"
mkdir -p "$pkg/source"
for name in setup-wsl.ps1 setup-wsl.sh setup.ps1 docker-compose.yml env.example mysql.cnf; do
  git -C "$root" show "HEAD:backend/deploy/selfhost/local/$name" > "$pkg/$name"
done
# 只放构建镜像要用的文件（和根目录 .dockerignore 的白名单一致）
git -C "$root" archive HEAD Dockerfile .dockerignore backend/pyproject.toml backend/uv.lock \
  backend/README.md backend/alembic.ini backend/alembic backend/src backend/deploy "docs/数据来源" \
  | tar -x -C "$pkg/source"
echo "$sha" > "$pkg/source/VERSION"
if [ "${2:-}" != "" ]; then
  python3 -I - "$2" "$pkg/source/backend/alembic/versions" <<'EOF'
import gzip
import json
import pathlib
import re
import sys

with gzip.open(sys.argv[1], "rt", encoding="utf-8") as f:
    header = json.loads(f.readline())
revisions, parents = set(), set()
for path in pathlib.Path(sys.argv[2]).glob("*.py"):
    text = path.read_text(encoding="utf-8")
    found = re.search(r"^revision\b[^=]*=\s*['\"]([^'\"]+)", text, re.M)
    if found:
        revisions.add(found.group(1))
    for line in re.findall(r"^down_revision\b[^=]*=(.*)$", text, re.M):
        parents.update(re.findall(r"['\"]([^'\"]+)['\"]", line))
heads = revisions - parents
if header.get("format") != "youju-sync-export" or heads != {header.get("revision")}:
    sys.exit(f"起步数据的迁移版本 {header.get('revision')} 和源码的最新迁移 {sorted(heads)} 不一致，不打包")
EOF
  cp "$2" "$pkg/youju-sync-export.jsonl.gz"
fi
# 用 Python 打 zip：中文文件名（docs/数据来源）会标成 UTF-8。macOS 自带的 zip 不标，
# Windows 解压时按本机编码（GBK）读，文件夹名成了乱码，构建时就找不到来源清单
rm -f "$out_dir/youju-local-$sha.zip"
(cd "$work" && python3 -I - local "$out_dir/youju-local-$sha.zip" <<'EOF'
import os
import sys
import zipfile

top, target = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(target, "w") as z:
    for folder, subfolders, files in os.walk(top):
        subfolders.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            # 已经压缩过的（起步数据 .gz）直接存，不再压一遍
            kind = zipfile.ZIP_STORED if name.endswith(".gz") else zipfile.ZIP_DEFLATED
            z.write(path, path, compress_type=kind)
with zipfile.ZipFile(target) as z:
    unmarked = [i.filename for i in z.infolist() if not i.filename.isascii() and not i.flag_bits & 0x800]
if unmarked:
    sys.exit(f"这些文件名没标 UTF-8：{unmarked[:3]}")
EOF
)
rm -rf "$work"
echo "已打包：$out_dir/youju-local-$sha.zip"
