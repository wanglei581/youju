#!/bin/bash
# 有据 2.0 本机端（Windows 主机，WSL 方式）：在 WSL 发行版 youju 里以 root 运行，
# 装 Docker 引擎 → 构建镜像 → 生成 .env → 建表、导入起步数据 → 启动。
# 由 setup-wsl.ps1 调用（它先把安装包拷到 /opt/youju/local）。也可以手动重跑：
#   wsl -d youju -u root -e bash /opt/youju/local/setup-wsl.sh
# 重复运行安全：已有的 .env、数据库都不动；源码换了会重新构建镜像、迁移、重建容器。
# 起步数据只在「库是空的、还没配服务器」时自动导入；配了服务器的起步按手册第八节「本机重新起步」手动做。
# 退出码 10：刚改了发行版设置，要重启一次发行版（setup-wsl.ps1 会自动重启后再跑一遍）。
set -euo pipefail
# 全部输出走标准输出：Windows PowerShell 会把外部命令的标准错误当成报错
exec 2>&1
cd "$(dirname "$0")"

step() { printf '\n== %s\n' "$*"; }
# .env 里的一项（还没有 .env 时看 env.example）
env_value() {
  local file=.env
  [ -f "$file" ] || file=env.example
  sed -n "s/^$1=//p" "$file" | tr -d '\r' | head -n 1
}

step "1/5 发行版设置（systemd、默认用户）"
marker='# setup-wsl.sh 写的'
if ! grep -qsF "$marker" /etc/wsl.conf; then
  cat > /etc/wsl.conf <<EOF
$marker：Docker 引擎要 systemd；默认用户 root（这个发行版只跑有据本机端）；不把 Windows 的 PATH 加进来
[boot]
systemd=true
[user]
default=root
[interop]
appendWindowsPath=false
EOF
  echo "改了 /etc/wsl.conf，要重启一次发行版"
  exit 10
fi
if [ "$(ps -o comm= -p 1)" != "systemd" ]; then
  echo "systemd 还没生效，要重启一次发行版"
  exit 10
fi
# Ubuntu 的 WSL 包第一次打开交互窗口时会问要不要建普通用户：这个发行版只用 root，关掉这个向导
if [ -f /etc/wsl-distribution.conf ]; then
  sed -i -E 's|^([[:space:]]*command[[:space:]]*=[[:space:]]*/usr/lib/wsl/wsl-setup)|# \1|' /etc/wsl-distribution.conf
fi

step "2/5 Docker 引擎（腾讯云软件源）"
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
    sed -i -E 's#https?://(archive|security)\.ubuntu\.com/ubuntu/?#http://mirrors.cloud.tencent.com/ubuntu/#g' \
      /etc/apt/sources.list.d/ubuntu.sources
  fi
  export DEBIAN_FRONTEND=noninteractive
  # 第一次开机时自动更新可能占着锁：最多等 10 分钟；上次装到一半被打断的先收拾好
  apt_opts=(-o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
  dpkg --configure -a || true
  apt-get "${apt_opts[@]}" update
  apt-get "${apt_opts[@]}" -y upgrade
  apt-get "${apt_opts[@]}" install -y docker.io docker-compose-v2
fi

step "3/5 Docker 设置"
# Docker Hub 在国内连不上，经 DaoCloud 镜像站拉。compose 和 Dockerfile 里的基础镜像都按摘要锁定：
# 拉下来的内容 Docker 按摘要校验，镜像站换不了内容
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  echo '{"registry-mirrors": ["https://docker.m.daocloud.io"]}' > /etc/docker/daemon.json
  systemctl restart docker
fi
systemctl enable --now docker
docker info --format 'Docker {{.ServerVersion}}'

step "4/5 构建镜像（第一次 10-20 分钟，之后只重建改过的部分）"
image=$(env_value YOUJU_IMAGE)
image=${image:-youju-selfhost:latest}
docker build -t youju-base:local -f source/Dockerfile source
docker build -t "$image" --build-arg BASE=youju-base:local \
  -f source/backend/deploy/selfhost/Dockerfile source/backend/deploy/selfhost

step "5/5 配置、建表、导入起步数据、启动"
if [ -f .env ]; then
  echo "已经有 .env，不改它（/opt/youju/local/.env）"
else
  rand() { od -An -tx1 -N24 /dev/urandom | tr -d ' \n'; }
  db_password=$(rand)
  umask 077
  sed -e "s/^MYSQL_ROOT_PASSWORD=.*/MYSQL_ROOT_PASSWORD=$(rand)/" \
    -e "s/^MYSQL_PASSWORD=.*/MYSQL_PASSWORD=${db_password}/" \
    -e "s/换成上面的MYSQL_PASSWORD/${db_password}/" \
    -e "s/^YOUJU_ADMIN_TOKEN=.*/YOUJU_ADMIN_TOKEN=$(rand)/" \
    -e "s#^YOUJU_SYNC_URL=.*#YOUJU_SYNC_URL=#" \
    -e "s/^YOUJU_SYNC_SECRET=.*/YOUJU_SYNC_SECRET=/" \
    env.example > .env.new
  mv .env.new .env
  umask 022
  echo "已生成 .env：数据库密码、核对台口令随机生成；服务器地址和同步密钥留空，先只在本机跑"
fi
docker compose up -d --wait --wait-timeout 300 mysql
# 升级时先停旧 worker：迁移不能让旧代码跑在新结构上（deploy/start.sh 开头的规矩）；迁移中途失败时
# worker 也停着、明确报错，不会让旧代码接着跑在迁移了一半的库上。第一次安装时这一步什么也不做
docker compose stop worker
# 用一次性容器建表、迁移到这一版的结构（worker 启动时也会做；先做是为了下面判断和导入）
docker compose run --rm --no-deps worker youju db-upgrade
# shellcheck disable=SC2016  # $MYSQL_ROOT_PASSWORD 要在容器里展开
sources=$(docker compose exec -T mysql sh -c \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B -e "SELECT COUNT(*) FROM youju.sources"' 2>/dev/null || true)
if [ -z "$sources" ]; then
  # 查不到就停下：照样起 worker 的话，空库会跳过起步导入、从头抓，之后手动导入会被"表不是空的"拒掉
  echo "查不到库里的数据量（数据库连不上？），worker 先不启动。把上面的输出发给 Claude。"
  exit 1
fi
if [ "$sources" = "0" ] && [ -n "$(env_value YOUJU_SYNC_URL)" ]; then
  # 配了服务器的空库：起步数据必须是服务器刚导出的那一份，导入后还要对齐服务器进度，不能自动做；
  # 也先不启动 worker（它一启动就开始抓，抓到的新数据会让导入拒绝——导入只接受空库）
  echo "已经配了服务器、库是空的：请按手册第八节「本机重新起步」手动起步（导出文件要是服务器刚导出的），做完再启动 worker"
  exit 0
fi
if [ "$sources" = "0" ] && [ -f youju-sync-export.jsonl.gz ]; then
  # 在定时任务开始之前导入：worker 一启动 cron 就开始跑
  docker compose run --rm --no-deps -v "$PWD:/move:ro" worker \
    youju sync-import --file /move/youju-sync-export.jsonl.gz
  date '+%F %T' > .imported
  echo "起步数据已导入（先只在本机跑；以后配服务器时按手册第五节第 7 条重新起步）"
elif [ "$sources" = "0" ]; then
  echo "库是空的，也没有起步数据：定时任务会从头抓取"
else
  echo "库里已经有数据（来源 ${sources} 个），不导入"
fi
if [ "$sources" != "0" ] && [ -n "$(env_value YOUJU_SYNC_URL)" ]; then
  # 升级可能新加了要同步的表（如 0051 的 takedown_blocks）：按这一版的表重建同步触发器，
  # 不然推送会停在"少了触发器"。可以重复执行，变更流和待推的变更都不变
  docker compose run --rm --no-deps worker youju sync-setup
fi
docker compose up -d --wait --wait-timeout 900
curl -fsS http://127.0.0.1:8080/health
echo
echo "本机端已启动：抓取、处理按定时任务跑（每天 6:30、20:30 各一轮）。"
