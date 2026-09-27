# 有据后端镜像（微信云托管，#32）。构建目录是仓库根目录，.dockerignore 只放进后端代码和来源清单。
# 同一个镜像跑两个服务，用环境变量 YOUJU_ROLE 区分：
#   api   小程序接口：关闭公网，只经 wx.cloud.callContainer 访问；按量伸缩
#   admin 核对台和定时任务：开公网，只开放核对台、管理接口和健康检查（口令）；实例数固定为 1，容器里跑 cron
# 注意：本机没有 Docker，这个文件还没有构建验证过；开通云托管后第一次构建时验证。
# 基础镜像来自 Docker Hub；若云托管构建拉取慢，改用云托管提供的镜像加速地址（待验证）。
FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 系统包走腾讯云 Debian 源
RUN sed -i 's#deb.debian.org#mirrors.cloud.tencent.com#g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
# 依赖：按锁文件导出固定版本清单，再用腾讯云 PyPI 源安装（uv sync --frozen 会按锁文件里的原始地址下载，不走镜像）
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN pip install --no-cache-dir uv \
    && uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt
COPY backend/src ./src
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY backend/deploy ./deploy
COPY ["docs/数据来源", "/app/docs/数据来源"]
RUN pip install --no-cache-dir --no-deps . && chmod +x deploy/start.sh deploy/job.sh

EXPOSE 80
CMD ["deploy/start.sh"]
