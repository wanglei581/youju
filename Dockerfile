# 有据后端镜像（微信云托管，#32）。构建目录是仓库根目录，.dockerignore 只放进后端代码和来源清单。
# 同一个镜像跑两个服务，用环境变量 YOUJU_ROLE 区分：
#   api   小程序接口：关闭公网，只经 wx.cloud.callContainer 访问；按量伸缩
#   admin 核对台和定时任务：开公网，只挂核对台和管理接口（口令）；实例数固定为 1，容器里跑 cron
# 注意：本机没有 Docker，这个文件还没有构建验证过；开通云托管后第一次构建时验证。
FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    UV_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/backend/.venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /app/backend
# 先装依赖（依赖不变时可复用这一层）
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src ./src
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY backend/deploy ./deploy
COPY ["docs/数据来源", "/app/docs/数据来源"]
RUN uv sync --frozen --no-dev && chmod +x deploy/start.sh deploy/job.sh

EXPOSE 80
CMD ["deploy/start.sh"]
