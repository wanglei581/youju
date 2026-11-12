#!/bin/sh
# 打共用服务器（D-126：创始人现有的腾讯云轻量服务器）用的安装包 youju-server-<提交号>.tar.gz：
#   shared/   docker-compose.yml、mysql.cnf、nginx-http.conf、nginx-https.conf、youju-shared.sh
#   source/   构建镜像要用的源码（和本机安装包同一份清单；VERSION 是提交号）
# 全部取自已提交的 HEAD（包名里的提交号和内容对得上，工作区没提交的改动不会混进去）。
# 打完打印 SHA-256：传到服务器以后，在控制台"执行命令"里先核对它再解包（见手册第十节）。
# 用法：sh make-server-package.sh <输出目录>
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
out_dir=$(cd "${1:?要给输出目录}" && pwd)
sha=$(git -C "$root" rev-parse --short=8 HEAD)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/shared" "$work/source"
for name in docker-compose.yml mysql.cnf nginx-http.conf nginx-https.conf youju-shared.sh; do
  git -C "$root" show "HEAD:backend/deploy/selfhost/shared/$name" > "$work/shared/$name"
done
# 只放构建镜像要用的文件（和根目录 .dockerignore 的白名单一致）
git -C "$root" archive HEAD Dockerfile .dockerignore backend/pyproject.toml backend/uv.lock \
  backend/README.md backend/alembic.ini backend/alembic backend/src backend/deploy "docs/数据来源" \
  | tar -x -C "$work/source"
echo "$sha" > "$work/source/VERSION"
target="$out_dir/youju-server-$sha.tar.gz"
# 用 Python 打包：属主统一成 root、不带 macOS 的扩展属性（服务器上的 GNU tar 解包不报一堆警告），
# PAX 格式存中文文件名（docs/数据来源）
python3 -I - "$work" "$target" <<'EOF'
import os
import sys
import tarfile

src, target = sys.argv[1], sys.argv[2]


def clean(info):
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = 0o755 if info.isdir() or info.name.endswith(".sh") else 0o644
    return info


with tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as tar:
    for top in ("shared", "source"):
        tar.add(os.path.join(src, top), arcname=top, filter=clean)
EOF
shasum -a 256 "$target"
