# 有据后端镜像（微信云托管，#32）。构建目录是仓库根目录，.dockerignore 只放进后端代码和来源清单。
# 同一个镜像跑两个服务，用环境变量 YOUJU_ROLE 区分：
#   api   小程序接口：关闭公网，只经 wx.cloud.callContainer 访问；按量伸缩
#   admin 核对台和定时任务：开公网，只开放核对台、管理接口和健康检查（口令）；实例数固定为 1，容器里跑 cron
# 注意：本机没有 Docker，这个文件还没有构建验证过；开通云托管后第一次构建时验证。
# 基础镜像来自 Docker Hub；若云托管构建拉取慢，改用云托管提供的镜像加速地址（待验证）。
FROM python:3.12-slim

# 系统包和 Python 包的下载源：默认腾讯云（云托管在国内构建）；CI 在境外机器上构建时改用官方源。
# Python 依赖按锁文件校验哈希，换源装出来的包也完全一样
ARG DEBIAN_MIRROR=mirrors.cloud.tencent.com
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN sed -i "s#deb.debian.org#${DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
# 依赖：按锁文件导出固定版本和哈希，再用腾讯云 PyPI 源安装并校验哈希（镜像上的文件与官方一致，哈希对得上；
# uv sync --frozen 会按锁文件里的原始地址下载，不走镜像）
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN pip install --no-cache-dir uv \
    && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt
COPY backend/src ./src
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY backend/deploy ./deploy
COPY ["docs/数据来源", "/app/docs/数据来源"]
# 用可编辑方式安装：代码留在 /app/backend/src，程序按源码位置找 alembic.ini、迁移目录（/app/backend）和
# 来源清单（/app/docs）。普通安装会把代码复制到 site-packages，这些相对路径就找不到了，admin 启动时迁移会失败
RUN pip install --no-cache-dir --no-deps -e . && chmod +x deploy/start.sh deploy/job.sh deploy/round.sh deploy/first-round.sh

EXPOSE 80
CMD ["deploy/start.sh"]
