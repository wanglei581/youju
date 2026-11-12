# 2.0 自建服务器和本机用的镜像：在云托管那个镜像（仓库根目录的 Dockerfile，一字不改）上多装两样：
#   age：异地备份加密（youju offsite-backup）；
#   curl：容器健康检查。
# 构建见 deploy/selfhost/build.sh（先构建根目录的镜像，再构建这个）。
ARG BASE=youju-base:local
FROM ${BASE}

ARG DEBIAN_MIRROR=mirrors.cloud.tencent.com
RUN sed -i "s#deb.debian.org#${DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends age curl \
    && rm -rf /var/lib/apt/lists/*
