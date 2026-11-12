#!/bin/sh
# 构建 2.0 自建服务器和本机用的镜像（在仓库根目录以外的任何地方执行都行）：
#   1. 用仓库根目录的 Dockerfile（云托管同一个）构建 youju-base:<版本>；
#   2. 在它上面加 age、curl，得到 youju-selfhost:<版本>，并打上 latest。
# 要推到镜像仓库（如腾讯云容器镜像服务个人版）时设 PUSH_TO，例如：
#   PUSH_TO=ccr.ccs.tencentyun.com/<命名空间>/youju sh build.sh
# 先 docker login 那个仓库（账号由创始人开，密码不写进任何文件）。
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
version=$(git -C "$root" rev-parse --short=8 HEAD 2>/dev/null || date +%Y%m%d%H%M)
docker build -t "youju-base:$version" -f "$root/Dockerfile" "$root"
docker build -t "youju-selfhost:$version" -t youju-selfhost:latest \
  --build-arg "BASE=youju-base:$version" -f "$here/Dockerfile" "$here"
echo "已构建 youju-selfhost:$version"
if [ -n "${PUSH_TO:-}" ]; then
  docker tag "youju-selfhost:$version" "$PUSH_TO:$version"
  docker tag "youju-selfhost:$version" "$PUSH_TO:latest"
  docker push "$PUSH_TO:$version"
  docker push "$PUSH_TO:latest"
  echo "已推到 $PUSH_TO:$version"
fi
