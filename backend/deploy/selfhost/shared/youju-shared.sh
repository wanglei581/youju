#!/bin/bash
# 有据 2.0 放在创始人现有的一台腾讯云轻量服务器上（和别的项目共用，D-126）：
# 安装、启停、证书和回退。只动有据自己的东西：
#   /opt/youju、容器和卷（compose 项目名 youju-server）、镜像 youju-base / youju-selfhost、
#   宝塔 nginx 站点目录里的一个文件（<接口域名>.conf）、/etc/cron.d/youju-cert、系统用户 youju；
#   "desktop" 子命令停 / 恢复桌面、远程桌面和打印服务（D-126 腾内存）。
# 每一步改了什么、怎么回退见 docs/部署/自建服务器.md 第十节。以 root 运行（控制台"执行命令"默认就是 root）。
# 要用域名的子命令（check、site、cert、renew、uninstall）先设 YOUJU_DOMAIN=<接口域名>。
#
# 用法：youju-shared.sh <子命令> [参数]
#   check                  只读检查：内存、硬盘、端口、Docker、nginx、sftp、域名解析
#   sites <域名>...        只读：经本机 nginx 取这些站点的 https、http 状态码（改 nginx 前后各跑一次对照）
#   desktop status|off|on  看 / 停 / 恢复桌面、远程桌面和打印服务（off 前记下原来的状态，on 照着恢复）
#   key '<公钥>'           建系统用户 youju：这把钥匙只能往 /opt/youju/incoming 传文件（sftp，不能下载），没有命令行
#   unkey                  删掉用户 youju 和它的公钥
#   unpack <版本> <哈希>   把传上来的安装包拷到只有 root 能进的目录，核对 SHA-256，解包到 /opt/youju/releases/<版本>
#   install <版本>         把 /opt/youju/releases/<版本> 里的配置装到 /opt/youju，构建镜像
#   env <版本>             生成 /opt/youju/server/ 下的配置和口令文件（随机密钥；已有就不动）：.env（接口、worker
#                          共用的配置，不含连库口令）、mysql-root.env（root 和建表迁移用的 youju）、db-api.env、db-worker.env
#   split-db-env           数据库账号按最小权限分开放：root、youju 的口令放进 mysql-root.env，接口、worker 各给一个
#                          自己的账号（db-api.env、db-worker.env），.env 里不留连库口令（用解包出来的新脚本 install 时
#                          会自动做；用旧脚本装的，up 之前补跑这个；已经分好就不动）
#   unsplit-db-env         回退到不认这几个文件的旧版本之前跑：把 root、youju 的口令和 youju 的连接串写回 .env
#   up                     启动：先 MySQL，再用 youju 迁移、建好收同步的进度表，再照 mysql_accounts.sql 设好接口、
#                          worker 的库账号和授权，再起 worker、接口（换镜像、改配置以后都用它起，接口、worker 别直接 docker compose up）
#   site http|https        写 nginx 站点文件，检查通过才重载（http 只为申请证书；https 正式转发并加续期定时任务）
#   cert                   申请证书（Let's Encrypt，HTTP 验证）
#   renew                  续期证书（定时任务每周一跑）；证书和 nginx 已加载的不一样才重载
#   status                 容器、内存、健康检查，接口和 worker 各用哪个库账号、有没有 root 和 youju 的口令（只说有没有）
#   logs mysql|api|worker [行数]
#   uninstall [--purge]    停掉有据、撤掉站点和定时任务；--purge 连数据库、文件、镜像一起删
# 首发前搬家（D-127，手册第六节）：
#   fetch-export <哈希>    提示时粘贴清单链接（不显示），按云托管导出的清单下载：日志、附件副本进存储，
#                          整库备份和下架清单进 /opt/youju/export（只有 root 能进）；可以重跑
#   export-show            只读：导出的汇总数、不涉密的配置、各前缀文件数（不含链接）
#   import-export <旧版本> 用迁移版本和备份一样的旧镜像在临时库里试恢复备份；整库备份、交易记录、批量工具结果分别加密存档（到期日写进文件名），删掉明文
#   takedowns [段号]       只读：打印下架清单（gzip 后 base64，每段 200 行），抄到本机用
#   takedowns-check        只读：下架清单里的网址在服务器上还有没有对用户可见的文件
# 紧急下架（电脑不在线时，手册第八节）：操作人写在 YOUJU_BY 里（不写记作"服务器命令行"）；
# 说明在提示里输入（不进命令历史、不出现在进程列表里；没有终端时不带说明）
#   emergency-find <文件编号|网址|标题里的字>   只读：找文件（编号、对不对外、标题、网址）
#   emergency-takedown <文件编号> <原因>        紧急下架，整组不对外；原因 complaint|authority|error|other
#   emergency-lift <文件编号>                   撤销这份文件所在组的紧急下架（下错了用）
#   emergency-list [--all] [--notes]            只读：紧急下架着的（--all 连撤销了的，--notes 显示说明）
#   emergency-carry [段号]                      只读：打印紧急下架清单（gzip 后 base64），电脑恢复后带回本机
#   emergency-settle                            带回本机已经正式下架的撤掉（定时任务每小时也做）
#   secret <名称>          只在交互终端里用：录一个密钥进 .env（输入不显示，只回长度和指纹）
#   show push|sync|ai      只在交互终端里用：显示消息推送的 Token、EncodingAESKey / 同步密钥 /
#                          后台 AI 的线路、模型（不涉密，照抄到本机 set-config.sh；密钥只给指纹）
#   rotate-sync            换一个新的同步密钥（不显示）
#   env-set <名称>         改一项不涉密的配置（值从标准输入读，如 echo '<值>' | … env-set YOUJU_CRONTAB）
#   env-unset <名称>       删掉一项不涉密的配置
#   config-from-export     把云托管导出的不涉密配置（模型名、价格、模板……）照原值写进 .env
#   cloudrun-gone deregistered|deleted  记下云托管里的库已经删了（注销环境 / 手动删），不再提醒
#   alert-probe            往告警通道发一条验收消息（删库期限的提醒走这个通道）
#   export-reset           退回云托管以后再切回来时用：清掉上一批"存档过"的标记，好重走导出（手册第七节）
# 数据库：
#   binlog-off             关掉 MySQL 的 binlog（install、up 带 disable_log_bin 的版本以后跑一次）：停接口和
#                          worker，清掉现有的 binlog，用新配置重建 MySQL，查 @@log_bin 是 0，删掉留下的
#                          binlog 文件，再起来
# 恢复演练（D-118 ②，手册第八节）：
#   restore-drill          用存储里最新的一份整库备份在临时库里恢复一遍、核对、删掉（不碰正式库）；
#                          先停 worker、接口照常，结束再起来；只在北京时间 10:00–23:30 跑
set -euo pipefail
umask 022

DOMAIN=${YOUJU_DOMAIN:-}
APPID=${YOUJU_WX_APPID:-wxcf20754e505e9bfa}
BASE=/opt/youju
SERVER=$BASE/server
WEBROOT=$BASE/acme-webroot
LE_DIR=$BASE/letsencrypt
STATE_FILE=$BASE/desktop-state.txt
NGINX=/www/server/nginx/sbin/nginx
VHOST_DIR=/www/server/panel/vhost/nginx
SITE=
CRON_FILE=/etc/cron.d/youju-cert
SFTP_SERVER=/usr/lib/openssh/sftp-server
# 这把钥匙的 sftp 不许做的操作：读文件、列目录、读链接、建链接、服务器内复制（只能上传）
SFTP_DENY=read,opendir,readdir,readlink,symlink,hardlink,copy-data
# nginx 已加载的证书的哈希：续期换了证书但当时重载失败，下周还会再试
LOADED_FILE=$BASE/letsencrypt-loaded.sha256
# 首发前搬家（D-127）下载的整库备份、下架清单、汇总（只有 root 能进）
EXPORT_DIR=$BASE/export
# secret 能录的密钥（都写进 .env；数据库密码、登录凭证密钥、核对台口令是装的时候随机生成的，不在这里）
SECRET_NAMES="YOUJU_WX_APPSECRET YOUJU_AI_API_KEY YOUJU_AI_ADVISOR_API_KEY YOUJU_XPAY_APPKEY_PROD
  YOUJU_XPAY_APPKEY_SANDBOX YOUJU_ALERT_WEBHOOK YOUJU_OFFSITE_S3_ACCESS_KEY YOUJU_OFFSITE_S3_SECRET_KEY
  YOUJU_OFFSITE_AGE_RECIPIENT"
# 装的时候随机生成、不用 env-set 改的（密钥另用 secret、rotate-sync）
PROTECTED_NAMES="MYSQL_ROOT_PASSWORD MYSQL_PASSWORD YOUJU_DATABASE_URL YOUJU_SESSION_SECRET YOUJU_ADMIN_TOKEN
  YOUJU_SYNC_SECRET YOUJU_WX_PUSH_TOKEN YOUJU_WX_PUSH_AES_KEY YOUJU_IMAGE"
# 正在跑的这份脚本的绝对路径：提示里的回退命令照抄就能用（停桌面服务时它还在 releases 目录里）
SELF=$(readlink -f "$0")
# 按摘要锁定（v5.8.0，2026-09-01 发布）；升级时查 Docker Hub 上该标签的摘要一起改
CERTBOT_IMAGE=certbot/certbot:v5.8.0@sha256:f70ad0adbb7e117f0fe42a63c553f28ea451edabc0148757b6efcd9735acaa20

# 停的服务（D-126）。先停桌面（腾出最多），再停会重新拉起服务的 socket、path、timer，再停服务本身。
# 远程桌面三个：创始人 11/9 说"不用了"。NetworkManager 不在里面：网卡靠它，停了会断网。
DESKTOP_UNITS="gdm.service gnome-remote-desktop.service xrdp.service xrdp-sesman.service
  cups.path cups.socket cups.service cups-browsed.service avahi-daemon.socket avahi-daemon.service
  fwupd-refresh.timer fwupd.service ModemManager.service kerneloops.service power-profiles-daemon.service
  switcheroo-control.service colord.service accounts-daemon.service upower.service udisks2.service
  rtkit-daemon.service"

die() { echo "错误：$*" >&2; exit 1; }

# 要用域名的子命令先调这个：检查 YOUJU_DOMAIN，定下站点文件的位置
need_domain() {
  [ -n "$DOMAIN" ] || die "先设 YOUJU_DOMAIN（接口域名），例如 YOUJU_DOMAIN=<接口域名> ${SELF} $1"
  [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || die "域名不对：${DOMAIN}"
  SITE=$VHOST_DIR/$DOMAIN.conf
}
say() { echo "== $*"; }

compose() {
  docker compose --project-directory "$SERVER" -f "$SERVER/docker-compose.yml" --env-file "$SERVER/.env" "$@"
}

public_ip() { curl -fsS -m 5 http://metadata.tencentyun.com/latest/meta-data/public-ipv4 || true; }

mem_available_mb() { awk '/^MemAvailable:/{print int($2 / 1024)}' /proc/meminfo; }

# 别的站点文件里已经写了这个域名的（宝塔的站点目录只认 *.conf）
other_sites() {
  local files=("$VHOST_DIR"/*.conf)
  [ -e "${files[0]}" ] || return 0
  awk -v d="$DOMAIN" -v s="$SITE" '
    $1 == "server_name" { for (i = 2; i <= NF; i++) { n = $i; sub(/;$/, "", n); if (n == d && FILENAME != s) print FILENAME } }
  ' "${files[@]}" | sort -u
}

check() {
  say "内存（MB）"
  free -m
  local avail; avail=$(mem_available_mb)
  [ "$avail" -ge 1700 ] || echo "注意：可用内存 ${avail}MB，不到 1.7G（有据三个容器上限合计 1.5G）"
  say "硬盘"
  df -h /
  local free_g; free_g=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
  [ "$free_g" -ge 5 ] || die "根分区只剩 ${free_g}G，不到 5G"
  say "端口"
  local port ours
  ours=$(docker ps --filter label=com.docker.compose.project=youju-server --format '{{.Ports}}')
  for port in 18100 18101; do
    if [ -n "$(ss -ltnH "sport = :${port}")" ]; then
      if [[ "$ours" == *"127.0.0.1:${port}->"* ]]; then
        echo "${port}：有据自己在用"
      else
        die "端口 ${port} 被别的程序占用"
      fi
    else
      echo "${port}：空闲"
    fi
  done
  say "Docker"
  docker version --format 'Docker {{.Server.Version}}'
  docker compose version
  say "nginx"
  [ -x "$NGINX" ] || die "没找到宝塔的 nginx（${NGINX}）"
  "$NGINX" -t
  say "sftp"
  [ -x "$SFTP_SERVER" ] || die "没有 ${SFTP_SERVER}"
  echo "有 ${SFTP_SERVER}"
  say "域名"
  local others; others=$(other_sites)
  [ -z "$others" ] || die "这些站点文件已经用了 ${DOMAIN}：${others}"
  echo "本机公网 IP $(public_ip)；${DOMAIN} 解析到 $({ getent ahostsv4 "$DOMAIN" || true; } | awk '{print $1}' | sort -u | tr '\n' ' ')"
  echo "检查完毕（只读）"
}

sites() {
  [ "$#" -gt 0 ] || die "用法：sites <域名>..."
  local h https http
  for h in "$@"; do
    [[ "$h" =~ ^[a-z0-9.-]+$ ]] || die "域名不对：${h}"
    https=$(curl -s -o /dev/null -m 8 -w '%{http_code}' --resolve "$h:443:127.0.0.1" "https://$h/" || true)
    http=$(curl -s -o /dev/null -m 8 -w '%{http_code}' --resolve "$h:80:127.0.0.1" "http://$h/" || true)
    echo "$h https=${https} http=${http}"
  done
}

# 只挂在桌面模式（graphical.target）下、无桌面开机（multi-user.target）不会拉起的服务，去掉要停的这些：
# 别的项目的服务要是写的 WantedBy=graphical.target，改开机默认以后重启就不会再起来
# 写 WantedBy=default.target 的不算：default.target 跟着开机默认走，改了照样起。
# 在 $(...) 里调用时 bash 不带 set -e，出错要自己返回非零，调用处再停下。
graphical_only() {
  local m g d
  m=$(systemctl list-dependencies --plain --no-pager multi-user.target | sed -E 's/^[^A-Za-z0-9]+//' | LC_ALL=C sort -u) \
    && [ -n "$m" ] || return 1
  g=$(systemctl list-dependencies --plain --no-pager graphical.target | sed -E 's/^[^A-Za-z0-9]+//' | LC_ALL=C sort -u) \
    && [ -n "$g" ] || return 1
  d=$(find /etc/systemd/system/default.target.wants /usr/lib/systemd/system/default.target.wants \
    -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null || true)
  LC_ALL=C comm -13 <(printf '%s\n' "$m") <(printf '%s\n' "$g") \
    | { grep -v -x -F -f <(printf '%s\n' graphical.target display-manager.service $DESKTOP_UNITS $d) || true; }
}

unit_state() {
  local enabled active
  enabled=$(systemctl is-enabled "$1" 2>/dev/null) || true
  active=$(systemctl is-active "$1" 2>/dev/null) || true
  echo "$1 ${enabled:-unknown} ${active:-unknown}"
}

desktop() {
  local u
  case "${1:-status}" in
    status)
      echo "开机默认：$(systemctl get-default)"
      for u in $DESKTOP_UNITS; do unit_state "$u"; done
      echo "只挂在桌面模式下的别的服务（改无桌面开机后重启不会起来）：$({ graphical_only || echo "（查不到）"; } | tr '\n' ' ')"
      free -m
      ;;
    off)
      local extra
      extra=$(graphical_only) || die "查不到开机依赖（systemctl list-dependencies 出错），先别动（什么都还没停）"
      if [ -n "$extra" ] && [ "${2:-}" != "--force" ]; then
        echo "这些服务只挂在桌面模式（graphical.target）下，改成无桌面开机后重启就不会再起来："
        echo "$extra"
        die "先查清它们是谁的、要不要留；没问题再加 --force（什么都还没停）"
      fi
      # 只在第一次停之前记原来的状态：重跑 off 不会把"已经停了"记成原状
      if [ ! -f "$STATE_FILE" ]; then
        install -d -m 755 "$BASE"
        { echo "default $(systemctl get-default)"; for u in $DESKTOP_UNITS; do unit_state "$u"; done; } \
          > "$STATE_FILE.tmp"
        mv -f "$STATE_FILE.tmp" "$STATE_FILE"
      fi
      say "停之前"
      free -m
      systemctl set-default multi-user.target
      for u in $DESKTOP_UNITS; do
        systemctl stop "$u" 2>/dev/null || true
        # 静态服务（没有开机自启的设置，如 gdm、colord）disable 会报错，不影响：开机默认已不进桌面
        systemctl disable "$u" >/dev/null 2>&1 || true
      done
      sleep 5
      say "停之后"
      free -m
      for u in $DESKTOP_UNITS; do unit_state "$u"; done
      say "还在的登录会话（别的项目挂在 SSH 会话下的进程不受影响）"
      loginctl list-sessions --no-legend || true
      say "3389（远程桌面）还有没有程序在听"
      ss -ltnH "sport = :3389" || true
      echo "原来的状态记在 ${STATE_FILE}；恢复：${SELF} desktop on"
      ;;
    on)
      [ -f "$STATE_FILE" ] || die "没有 ${STATE_FILE}：不知道原来的状态"
      local default enabled active
      default=$(awk '$1 == "default" {print $2}' "$STATE_FILE")
      [ -n "$default" ] || die "${STATE_FILE} 里没有开机默认"
      # 倒着恢复：先服务、再 socket，最后 gdm（它要 accounts-daemon 等先起来）
      while read -r u enabled active; do
        if [ "$u" = default ]; then continue; fi
        # alias：有的系统上 gdm 以 display-manager.service 的别名启用，disable 会删掉别名，这里要加回来
        if [ "$enabled" = enabled ] || [ "$enabled" = alias ]; then
          systemctl enable "$u" >/dev/null 2>&1 || echo "恢复开机自启失败：${u}"
        fi
        if [ "$active" = active ]; then
          systemctl start "$u" || echo "启动失败：${u}"
        fi
      done < <(tac "$STATE_FILE")
      systemctl set-default "$default"
      mv -f "$STATE_FILE" "$STATE_FILE.restored-$(date +%Y%m%d%H%M%S)"
      for u in $DESKTOP_UNITS; do unit_state "$u"; done
      ;;
    *) die "用法：desktop status|off [--force]|on" ;;
  esac
}

key() {
  local pub=${1:-}
  [[ "$pub" =~ ^ssh-ed25519\ [A-Za-z0-9+/]+={0,3}(\ [A-Za-z0-9._@-]+)?$ ]] \
    || die "公钥格式不对：要 ssh-ed25519 开头的一整行"
  [ -x "$SFTP_SERVER" ] || die "没有 ${SFTP_SERVER}"
  id youju >/dev/null 2>&1 || useradd --no-create-home --home-dir /home/youju --shell /bin/sh youju
  # 没有密码（不能用密码登录），只认下面这把公钥
  usermod -p '*' youju
  install -d -m 755 "$BASE"
  install -d -m 750 -o youju -g youju "$BASE/incoming"
  # 家目录、.ssh、authorized_keys 都归 root：youju 用 sftp 改不了自己这把钥匙的限制（sshd 认 root 属主）
  install -d -m 755 -o root -g root /home/youju /home/youju/.ssh
  # restrict：不给命令行、端口转发、代理转发；command：这把钥匙只能跑 sftp，起始目录 incoming，只能上传
  printf 'restrict,command="%s -d %s -P %s" %s\n' "$SFTP_SERVER" "$BASE/incoming" "$SFTP_DENY" "$pub" \
    > /home/youju/.ssh/authorized_keys.tmp
  chown root:root /home/youju/.ssh/authorized_keys.tmp
  chmod 644 /home/youju/.ssh/authorized_keys.tmp
  mv -f /home/youju/.ssh/authorized_keys.tmp /home/youju/.ssh/authorized_keys
  echo "用户 youju 已就绪。本机核对服务器指纹："
  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
  echo "回退：${SELF} unkey"
}

unkey() {
  if id youju >/dev/null 2>&1; then
    pkill -u youju || true
    sleep 1
    userdel youju
  fi
  # 家目录归 root（userdel -r 不删别人的目录），自己删
  rm -rf /home/youju "$BASE/incoming"
  echo "已删除用户 youju、/home/youju 和 ${BASE}/incoming"
}

unpack() {
  local ver=${1:-} sum=${2:-}
  [[ "$ver" =~ ^[0-9a-f]{8}$ ]] || die "版本要写 8 位提交号"
  [[ "$sum" =~ ^[0-9a-f]{64}$ ]] || die "哈希要写打包时打印的 64 位 SHA-256"
  local name=youju-server-$ver.tar.gz
  # 先拷到只有 root 能进的目录再核对、解包：incoming 归 youju，核对完到解包之间文件可能被换掉
  install -d -m 700 "$BASE/staging"
  install -m 600 "$BASE/incoming/$name" "$BASE/staging/$name"
  echo "$sum  $BASE/staging/$name" | sha256sum -c -
  install -d -m 755 "$BASE/releases"
  rm -rf "$BASE/releases/$ver"
  mkdir -m 755 "$BASE/releases/$ver"
  tar -xzf "$BASE/staging/$name" -C "$BASE/releases/$ver" --no-same-owner
  rm -f "$BASE/staging/$name"
  echo "已解包到 ${BASE}/releases/${ver}；接着运行 ${BASE}/releases/${ver}/shared/youju-shared.sh install ${ver}"
}

check_version() {
  [[ "${1:-}" =~ ^[0-9a-f]{8}$ ]] || die "版本要写 8 位提交号"
  [ "$(cat "$BASE/releases/$1/source/VERSION" 2>/dev/null)" = "$1" ] \
    || die "${BASE}/releases/$1 里没有这个版本（先核对哈希、解包，见手册第十节）"
}

install_release() {
  local ver=${1:-}
  check_version "$ver"
  local rel=$BASE/releases/$ver
  # 这一版要按最小权限分开库账号（它的 compose 认这几个口令文件）：先核对分得了，分不了就停下，一个文件都不换
  if [ -f "$SERVER/.env" ] && compose_splits_db "$rel/shared/docker-compose.yml"; then
    db_split_check || die "install 停下了，一个文件都没换；核对改好以后重跑 install"
  fi
  install -d -m 755 "$BASE" "$BASE/bin" "$BASE/nginx" "$WEBROOT"
  install -d -m 700 "$SERVER" "$LE_DIR"
  install -m 644 "$rel/shared/docker-compose.yml" "$rel/shared/mysql.cnf" \
    "$rel/source/backend/deploy/mysql_accounts.sql" "$SERVER/"
  install -m 644 "$rel/shared/nginx-http.conf" "$rel/shared/nginx-https.conf" "$BASE/nginx/"
  install -m 755 "$rel/shared/youju-shared.sh" "$BASE/bin/youju-shared.sh"
  # 新的 compose 一装上就把库账号分开放好：不然构建镜像这段时间里 status、logs、紧急下架这些要读 compose 的
  # 子命令都会因为缺口令文件用不了。装的是还不认这几个文件的旧版本（回退）就不动
  if [ -f "$SERVER/.env" ] && compose_splits_db "$SERVER/docker-compose.yml"; then
    split_db_env
  fi
  say "构建镜像 youju-selfhost:${ver}（第一次 5–15 分钟）"
  docker build -t "youju-base:$ver" -f "$rel/source/Dockerfile" "$rel/source"
  docker build -t "youju-selfhost:$ver" --build-arg "BASE=youju-base:$ver" \
    -f "$rel/source/backend/deploy/selfhost/Dockerfile" "$rel/source/backend/deploy/selfhost"
  if [ -f "$SERVER/.env" ]; then
    sed -i "s#^YOUJU_IMAGE=.*#YOUJU_IMAGE=youju-selfhost:${ver}#" "$SERVER/.env"
    echo "已把 .env 里的镜像换成 youju-selfhost:${ver}，运行 ${BASE}/bin/youju-shared.sh up 生效"
  fi
  echo "回退：.env 里 YOUJU_IMAGE 改回上一版再 up；不要的镜像 docker image rm youju-selfhost:${ver} youju-base:${ver}"
}

make_env() {
  local ver=${1:-}
  check_version "$ver"
  [ -f "$SERVER/docker-compose.yml" ] || die "先 install ${ver}"
  if [ -f "$SERVER/.env" ]; then
    echo "已有 ${SERVER}/.env，不动（要重新生成先自己挪走）"
    return
  fi
  local f
  for f in $DB_FILES; do
    [ ! -f "$SERVER/$f" ] \
      || die "已有 ${SERVER}/${f}、没有 .env：数据库可能已经用里面的口令建好了，先人工核对（要重新生成先把它也挪走）"
  done
  local cloud_env
  cloud_env=$(sed -n 's/^YOUJU_CLOUD_ENV=//p' "$BASE/releases/$ver/source/backend/deploy/selfhost/server/env.example")
  [ -n "$cloud_env" ] || die "env.example 里没有 YOUJU_CLOUD_ENV"
  local db_pw root_pw api_pw worker_pw aes
  db_pw=$(openssl rand -hex 24)
  root_pw=$(openssl rand -hex 24)
  api_pw=$(openssl rand -hex 24)
  worker_pw=$(openssl rand -hex 24)
  for f in "$db_pw" "$root_pw" "$api_pw" "$worker_pw"; do
    [ "${#f}" -eq 48 ] || die "生成数据库口令失败"
  done
  aes=$(openssl rand -base64 96 | tr -dc 'A-Za-z0-9')
  aes=${aes:0:43}
  [ "${#aes}" -eq 43 ] || die "生成消息推送密钥失败"
  (
    umask 077
    cat > "$SERVER/.env.tmp" <<EOF
# 有据 2.0（共用服务器，D-126）的配置：youju-shared.sh env 生成于 $(date '+%F %T')，只有 root 能读。
# 不进代码库、不发到聊天里。
# 测试期（没有用户数据、不切流量）：
# - 微信接口指向本机一个不存在的地址（YOUJU_WX_API_BASE），AppSecret 是占位：程序不会拿它去调微信；
# - worker 只写心跳、不跑定时任务（docker-compose.yml 里 YOUJU_CRONTAB 的默认值）。
# 上线前按手册第十节"上线前"一项项改：真的 AppSecret、删掉 YOUJU_WX_API_BASE、YOUJU_CRONTAB、异地备份、
# 换掉同步密钥……
# 连库的口令不在这里：root 和 youju（建表迁移）在 mysql-root.env，接口、worker 各在 db-api.env、db-worker.env。
YOUJU_IMAGE=youju-selfhost:${ver}
YOUJU_HOSTING=self
YOUJU_SESSION_LOGIN=true
YOUJU_SESSION_SECRET=$(openssl rand -hex 32)
YOUJU_WX_OPEN_API=token
YOUJU_WX_APPID=${APPID}
YOUJU_WX_APPSECRET=test-placeholder-not-a-secret
YOUJU_WX_API_BASE=http://127.0.0.1:9
YOUJU_CLIENT_IP_HEADER=x-forwarded-for
YOUJU_LOG_MASK_IP=true
YOUJU_PUBLIC_REPLICA=true
YOUJU_ADMIN_TOKEN=$(openssl rand -hex 32)
YOUJU_WX_PUSH_MODE=signed
YOUJU_WX_PUSH_TOKEN=$(openssl rand -hex 16)
YOUJU_WX_PUSH_AES_KEY=${aes}
YOUJU_STORAGE_BACKEND=disk
YOUJU_STORAGE_DIR=/data/storage
YOUJU_CLOUD_ENV=${cloud_env}
YOUJU_STORAGE_BUCKET=youju-self
YOUJU_SYNC_SECRET=$(openssl rand -hex 32)
EOF
    printf 'MYSQL_ROOT_PASSWORD=%s\nMYSQL_PASSWORD=%s\n' "$root_pw" "$db_pw" > "$SERVER/mysql-root.env.tmp"
    printf 'YOUJU_DATABASE_URL=%s\n' "$(db_url_for youju_api "$api_pw")" > "$SERVER/db-api.env.tmp"
    printf 'YOUJU_DATABASE_URL=%s\n' "$(db_url_for youju_worker "$worker_pw")" > "$SERVER/db-worker.env.tmp"
    for f in $DB_FILES; do mv -f "$SERVER/$f.tmp" "$SERVER/$f"; done
    mv -f "$SERVER/.env.tmp" "$SERVER/.env"
  )
  echo "已生成 ${SERVER}/.env、mysql-root.env、db-api.env、db-worker.env（连库口令按账号分开放，都只有 root 能读）"
}

# 数据库账号按最小权限分开（共用服务器）：
# - root 和 youju（建表、迁移用，对 youju 库有全部权限）的口令只在 mysql-root.env：只给 MySQL 容器和这个脚本；
# - 接口用 youju_api（授权照 mysql_accounts.sql 第 ② 节，和云托管同一份），worker 用 youju_worker（增删改查，
#   加上恢复备份要的 LOCK TABLES），连接串各在 db-api.env、db-worker.env，compose 只给各自的容器；
# - 接口、worker 共用的 .env 里不留任何连库口令。这几个文件都只有 root 能读。
DB_FILES="mysql-root.env db-api.env db-worker.env"
DB_WORKER_GRANTS="SELECT, INSERT, UPDATE, DELETE, LOCK TABLES"

# 这几个文件里的一项（文件没有时回空）
kv_get() { { sed -n "s/^$2=//p" "$SERVER/$1" 2>/dev/null || true; } | head -n 1; }
root_password() { kv_get mysql-root.env MYSQL_ROOT_PASSWORD; }
migrate_password() { kv_get mysql-root.env MYSQL_PASSWORD; }
db_url() { kv_get "db-$1.env" YOUJU_DATABASE_URL; }
db_url_for() { printf 'mysql+pymysql://%s:%s@mysql:3306/youju?charset=utf8mb4' "$1" "$2"; }
# 连接串里的口令：只认 db_url_for 写的那种（账号对、连本机 MySQL 的 youju 库、口令只有字母数字），不是就返回 1
db_url_password() {
  local rest
  case "$1" in "mysql+pymysql://$2:"*"@mysql:3306/youju?charset=utf8mb4") ;; *) return 1 ;; esac
  rest=${1#"mysql+pymysql://$2:"}
  rest=${rest%"@mysql:3306/youju?charset=utf8mb4"}
  [[ "$rest" =~ ^[A-Za-z0-9]+$ ]] || return 1
  printf '%s' "$rest"
}
# 这份 compose 认不认这几个口令文件
compose_splits_db() {
  grep -qx '    env_file: mysql-root.env' "$1" \
    && grep -qx '    env_file: \[\.env, db-api\.env\]' "$1" \
    && grep -qx '    env_file: \[\.env, db-worker\.env\]' "$1"
}

db_env_ok() {
  local name
  for name in MYSQL_ROOT_PASSWORD MYSQL_PASSWORD YOUJU_DATABASE_URL; do
    [ -z "$(env_get "$name")" ] \
      || die "${SERVER}/.env 里还有 ${name}：数据库账号要按最小权限分开放，先运行 ${SELF} split-db-env"
  done
  { [ -n "$(root_password)" ] && [ -n "$(migrate_password)" ] \
    && db_url_password "$(db_url api)" youju_api >/dev/null \
    && db_url_password "$(db_url worker)" youju_worker >/dev/null; } \
    || die "${SERVER} 下的 mysql-root.env、db-api.env、db-worker.env 缺了或写法不对：先运行 ${SELF} split-db-env"
}

# 能不能照最小权限分开放：.env 里的口令和 mysql-root.env 里已有的不能对不上，.env 里的连接串要就是 youju 连本机
# 的那一个，已有的 db-*.env 写法要对。不能放时说清原因、返回 1；只打指纹（不打口令）：停下找团队核对，别把文件
# 内容打出来（"执行命令"有记录）
db_split_check() {
  local name in_env in_file url pw svc
  for name in MYSQL_ROOT_PASSWORD MYSQL_PASSWORD; do
    in_env=$(env_get "$name")
    in_file=$(kv_get mysql-root.env "$name")
    if [ -z "$in_env" ] && [ -z "$in_file" ]; then
      echo "错误：${SERVER}/.env 和 mysql-root.env 里都没有 ${name}" >&2
      return 1
    fi
    if [ -n "$in_env" ] && [ -n "$in_file" ] && [ "$in_env" != "$in_file" ]; then
      echo "错误：mysql-root.env 里的 ${name} 和 .env 里的不一样（.env 那个指纹 $(fingerprint "$in_env")，mysql-root.env 那个指纹 $(fingerprint "$in_file")）：停下找团队核对，别把两个文件的内容打出来" >&2
      return 1
    fi
  done
  url=$(env_get YOUJU_DATABASE_URL)
  pw=$(env_get MYSQL_PASSWORD)
  [ -n "$pw" ] || pw=$(migrate_password)
  if [ -n "$url" ] && [ "$url" != "$(db_url_for youju "$pw")" ]; then
    echo "错误：.env 里的 YOUJU_DATABASE_URL 不是 youju 账号连本机 MySQL 的那一个（指纹 $(fingerprint "$url")）：停下找团队核对" >&2
    return 1
  fi
  for svc in api worker; do
    url=$(db_url "$svc")
    if [ -n "$url" ] && ! db_url_password "$url" "youju_$svc" >/dev/null; then
      echo "错误：${SERVER}/db-${svc}.env 写法不对（指纹 $(fingerprint "$url")）：停下找团队核对" >&2
      return 1
    fi
  done
}

# 用解包出来的新脚本 install 这一版时自动跑（也可以单独补跑）：root、youju 的口令放进 mysql-root.env；接口、
# worker 还没有自己的账号的，各生成一个（口令随机）；.env 里删掉这三项。值不显示；已经分好就不动。
# 新文件都写好了才改 .env，中途断了重跑就行
split_db_env() {
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  compose_splits_db "$SERVER/docker-compose.yml" \
    || die "先 install 这一版（${SERVER}/docker-compose.yml 还不认这几个口令文件），什么都没动"
  db_split_check || die "什么都没动"
  local root_pw pw svc new
  root_pw=$(env_get MYSQL_ROOT_PASSWORD)
  [ -n "$root_pw" ] || root_pw=$(root_password)
  pw=$(env_get MYSQL_PASSWORD)
  [ -n "$pw" ] || pw=$(migrate_password)
  (
    umask 077
    printf 'MYSQL_ROOT_PASSWORD=%s\nMYSQL_PASSWORD=%s\n' "$root_pw" "$pw" > "$SERVER/mysql-root.env.tmp"
    mv -f "$SERVER/mysql-root.env.tmp" "$SERVER/mysql-root.env"
    for svc in api worker; do
      [ -z "$(db_url "$svc")" ] || continue
      new=$(openssl rand -hex 24)
      [ "${#new}" -eq 48 ] || exit 1
      printf 'YOUJU_DATABASE_URL=%s\n' "$(db_url_for "youju_$svc" "$new")" > "$SERVER/db-$svc.env.tmp"
      mv -f "$SERVER/db-$svc.env.tmp" "$SERVER/db-$svc.env"
    done
    awk 'index($0, "MYSQL_ROOT_PASSWORD=") != 1 && index($0, "MYSQL_PASSWORD=") != 1 \
      && index($0, "YOUJU_DATABASE_URL=") != 1' "$SERVER/.env" > "$SERVER/.env.tmp"
    mv -f "$SERVER/.env.tmp" "$SERVER/.env"
  ) || die "没分好（生成口令或写文件出错），查 ${SERVER} 下的文件"
  db_env_ok
  echo "数据库账号已按最小权限分开放（都只有 root 能读）：root 和 youju（建表迁移）的口令在 mysql-root.env，接口、worker 各用 db-api.env、db-worker.env 里自己的账号，.env 里不再有连库口令。运行 ${SELF} up 生效：它会照 mysql_accounts.sql 设好这两个账号和授权"
}

# 回退到还不认这几个口令文件的旧版本之前跑（用现在这份脚本）：把 root、youju 的口令和 youju 的连接串写回 .env
# （旧版本的 compose 从 .env 读）。口令文件留着；以后再升级回来，install 会照样分开
unsplit_db_env() {
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  local root_pw pw
  root_pw=$(root_password)
  pw=$(migrate_password)
  { [ -n "$root_pw" ] && [ -n "$pw" ]; } || die "${SERVER}/mysql-root.env 里缺 root 或 youju 的口令，什么都没动"
  set_env MYSQL_ROOT_PASSWORD "$root_pw"
  set_env MYSQL_PASSWORD "$pw"
  set_env YOUJU_DATABASE_URL "$(db_url_for youju "$pw")"
  echo "已把 root、youju 的口令和 youju 的连接串写回 .env（值不显示）；接着用旧版本解包出来的脚本 install 旧版本，再 up"
}

# 接口的授权：mysql_accounts.sql 第 ② 节里 TO 'youju_api'@'%' 的那些行（install 时拷到这里；和
# tests/conftest.py 建受限账号用的是同一个取法）
api_grants() { grep -E "^GRANT [^;]+ TO 'youju_api'@'%';$" "$SERVER/mysql_accounts.sql" 2>/dev/null || true; }
# 改权限之前先逐条查授权用到的表和列都在（SELECT … WHERE 1 = 0：不读数据，表或列不在就报错），不在就停在这里，
# 一项权限都不改
api_grant_checks() {
  api_grants | awk '
    match($0, / ON youju\.[a-z_]+ TO /) {
      table = substr($0, RSTART + 10, RLENGTH - 14)
      privs = substr($0, 1, RSTART - 1)
      cols = "1"
      if (match(privs, /\([a-z_, ]+\)/)) cols = substr(privs, RSTART + 1, RLENGTH - 2)
      print "SELECT " cols " FROM youju." table " WHERE 1 = 0;"
    }'
}
# 接口和 worker 的库账号：没有就建、口令和 db-*.env 对齐；查过表和列都在以后，权限先全部收回再照清单授（不多不少）
db_accounts_sql() {
  local api_pw worker_pw grants
  api_pw=$(db_url_password "$(db_url api)" youju_api)
  worker_pw=$(db_url_password "$(db_url worker)" youju_worker)
  grants=$(api_grants)
  [ -n "$grants" ] || die "${SERVER}/mysql_accounts.sql 里没找到接口的授权：先用解包出来的新脚本 install 这一版"
  printf '%s\n' \
    "CREATE USER IF NOT EXISTS 'youju_api'@'%' IDENTIFIED BY '${api_pw}';" \
    "ALTER USER 'youju_api'@'%' IDENTIFIED BY '${api_pw}';" \
    "CREATE USER IF NOT EXISTS 'youju_worker'@'%' IDENTIFIED BY '${worker_pw}';" \
    "ALTER USER 'youju_worker'@'%' IDENTIFIED BY '${worker_pw}';"
  api_grant_checks
  printf '%s\n' \
    "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'youju_api'@'%', 'youju_worker'@'%';" \
    "GRANT ${DB_WORKER_GRANTS} ON youju.* TO 'youju_worker'@'%';" \
    "$grants"
}
# 在 MySQL 容器里用 root 跑（口令是容器里的环境变量）；SQL 从标准输入给，口令不进命令行参数
db_accounts() {
  local sql
  sql=$(db_accounts_sql)
  # shellcheck disable=SC2016  # 要在容器里展开
  printf '%s\n' "$sql" | compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B' \
    || die "接口、worker 的库账号和授权没设好（原因见上面）：接口和 worker 还停着，改好以后再 ${SELF} up"
  echo "接口（youju_api）、worker（youju_worker）的库账号和授权已照清单设好"
}

up() {
  [ -f "$SERVER/.env" ] || die "先 env"
  db_env_ok
  compose config -q
  compose up -d --wait --wait-timeout 300 mysql
  # 升级时先停 worker、接口，迁移完再起（和本机 setup-wsl.sh 一样）
  compose stop worker api >/dev/null 2>&1 || true
  # 迁移用 youju 账号（建表、改表要它）；连接串经环境变量传进临时容器，不进命令行参数。收同步的进度表、
  # 编号下限也要建表、改表权限，同样用 youju 先做好（worker 平时只有增删改查）
  YOUJU_DATABASE_URL="$(db_url_for youju "$(migrate_password)")" \
    compose run --rm --no-deps -e YOUJU_DATABASE_URL worker youju db-upgrade
  YOUJU_DATABASE_URL="$(db_url_for youju "$(migrate_password)")" \
    compose run --rm --no-deps -e YOUJU_DATABASE_URL worker youju sync-server-setup
  db_accounts
  compose up -d --wait --wait-timeout 900 worker
  compose up -d --wait --wait-timeout 300 api
  status
  echo "回退：${SELF} uninstall（停掉、撤站点，数据留着）"
}

site() {
  local mode=${1:-}
  case "$mode" in http | https) ;; *) die "用法：site http|https" ;; esac
  local tpl=$BASE/nginx/nginx-$mode.conf
  [ -f "$tpl" ] || die "先 install"
  [ -x "$NGINX" ] || die "没找到宝塔的 nginx（${NGINX}）"
  "$NGINX" -t >/dev/null 2>&1 || die "nginx 现在的配置就有错，先别动（运行 ${NGINX} -t 看详情）"
  local others; others=$(other_sites)
  [ -z "$others" ] || die "这些站点文件已经用了 ${DOMAIN}：${others}"
  if [ "$mode" = https ]; then
    [ -s "$LE_DIR/live/$DOMAIN/fullchain.pem" ] || die "还没有证书，先 cert"
  fi
  install -d -m 755 "$WEBROOT"
  install -d -m 755 "$BASE/nginx"
  local backup
  backup=$BASE/nginx/vhost-before-$(date +%Y%m%d%H%M%S).tar.gz
  (umask 077; tar -czf "$backup" -C / "${VHOST_DIR#/}")
  echo "已备份站点目录：${backup}"
  local prev=""
  if [ -f "$SITE" ]; then
    prev=$BASE/nginx/site-before-$(date +%Y%m%d%H%M%S)
    cp -p "$SITE" "$prev"
  fi
  # 临时文件不以 .conf 结尾：宝塔只加载 *.conf，写到一半不会被 nginx 读到
  sed "s/__DOMAIN__/${DOMAIN}/g" "$tpl" > "$SITE.youju-tmp"
  chmod 644 "$SITE.youju-tmp"
  mv -f "$SITE.youju-tmp" "$SITE"
  if ! "$NGINX" -t >/dev/null 2>&1; then
    "$NGINX" -t || true
    if [ -n "$prev" ]; then cp -p "$prev" "$SITE"; else rm -f "$SITE"; fi
    die "新站点文件没通过 nginx 检查，已撤回，nginx 没有重载"
  fi
  "$NGINX" -s reload
  echo "已写 ${SITE} 并重载 nginx"
  if [ "$mode" = https ]; then mark_loaded; fi
  if [ "$mode" = https ]; then
    cat > "$CRON_FILE.tmp" <<EOF
# 有据（D-126）：每周一 4:23 续期 ${DOMAIN} 的证书（快到期才真的换，换了才重载 nginx）。删掉这个文件就停。
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
23 4 * * 1 root YOUJU_DOMAIN=${DOMAIN} ${BASE}/bin/youju-shared.sh renew >> /var/log/youju-cert.log 2>&1
EOF
    chmod 644 "$CRON_FILE.tmp"
    mv -f "$CRON_FILE.tmp" "$CRON_FILE"
    echo "已加续期定时任务 ${CRON_FILE}"
  fi
  echo "回退：rm ${SITE} ${CRON_FILE} && ${NGINX} -t && ${NGINX} -s reload"
}

mark_loaded() { sha256sum < "$LE_DIR/live/$DOMAIN/fullchain.pem" > "$LOADED_FILE"; }

cert() {
  [ -f "$SITE" ] || die "先 site http（Let's Encrypt 要从 80 端口取验证文件）"
  local ip resolved
  ip=$(public_ip)
  # 还没解析时 getent 退出码是 2：不让它在这里中断脚本，下面给出能看懂的提示
  resolved=$({ getent ahostsv4 "$DOMAIN" || true; } | awk '{print $1}' | sort -u | tr '\n' ' ')
  [ -n "$resolved" ] || die "${DOMAIN} 还没解析：先在 DNSPod 加 A 记录"
  if [ -n "$ip" ]; then
    [ "$resolved" = "$ip " ] || die "${DOMAIN} 解析到「${resolved}」，不是本机 ${ip}"
  else
    echo "注意：取不到本机公网 IP，没法核对解析（${DOMAIN} 解析到 ${resolved}），照样申请"
  fi
  install -d -m 755 "$WEBROOT"
  install -d -m 700 "$LE_DIR"
  # 上次被打断留下的同名容器会让 docker run 直接失败
  docker rm -f youju-certbot >/dev/null 2>&1 || true
  docker run --rm --name youju-certbot \
    -v "$LE_DIR:/etc/letsencrypt" -v "$WEBROOT:/webroot" \
    "$CERTBOT_IMAGE" certonly --webroot -w /webroot -d "$DOMAIN" \
    --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring
  [ -s "$LE_DIR/live/$DOMAIN/fullchain.pem" ] || die "没拿到证书"
  openssl x509 -in "$LE_DIR/live/$DOMAIN/fullchain.pem" -noout -subject -enddate
  echo "回退：rm -rf ${LE_DIR}（证书和 Let's Encrypt 账号一起删）"
}

renew() {
  local pem=$LE_DIR/live/$DOMAIN/fullchain.pem
  if [ ! -s "$pem" ]; then
    echo "$(date '+%F %T') 没有 ${DOMAIN} 的证书，跳过"
    return
  fi
  docker rm -f youju-certbot-renew >/dev/null 2>&1 || true
  if ! docker run --rm --name youju-certbot-renew \
    -v "$LE_DIR:/etc/letsencrypt" -v "$WEBROOT:/webroot" \
    "$CERTBOT_IMAGE" renew --non-interactive; then
    echo "$(date '+%F %T') certbot 续期请求失败（网络或 Let's Encrypt 限流？），下周再试" >&2
    return 1
  fi
  # 和 nginx 已加载的比（不是和续期前比）：哪次换了证书但重载失败，下周还会再试
  if [ "$(sha256sum < "$pem")" != "$(cat "$LOADED_FILE" 2>/dev/null || true)" ]; then
    if "$NGINX" -t; then
      "$NGINX" -s reload
      mark_loaded
      echo "$(date '+%F %T') 证书已更新，nginx 已重载"
    else
      echo "$(date '+%F %T') 证书已更新，但 nginx 检查没过（别的站点的问题？），没有重载，下周再试" >&2
      return 1
    fi
  else
    echo "$(date '+%F %T') 证书还不用换"
  fi
}

status() {
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  compose ps
  local ids; ids=$(compose ps -q)
  if [ -n "$ids" ]; then
    # shellcheck disable=SC2086
    docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' $ids
  fi
  local port
  for port in 18100 18101; do
    echo "127.0.0.1:${port}/health → $(curl -fsS -m 5 "http://127.0.0.1:${port}/health" || echo 不通)"
  done
  # 接口、worker 各用自己的库账号，里面不该有 root 和 youju 的口令：只打账号名和"有没有"，不显示口令
  local svc got
  for svc in api worker; do
    # shellcheck disable=SC2016  # 要在容器里展开
    got=$(compose exec -T "$svc" sh -c 'u=${YOUJU_DATABASE_URL#*://}; u=${u%%:*}
if [ -n "${MYSQL_ROOT_PASSWORD+x}${MYSQL_PASSWORD+x}" ]; then s=有; else s=没有; fi
echo "${u:-无}" "$s"' </dev/null 2>/dev/null) || got=""
    if [ -n "$got" ]; then
      echo "${svc}：连库账号 ${got% *}；root、youju 的口令：${got##* }"
    else
      echo "${svc}：查不了"
    fi
  done
  free -m
}

logs() {
  case "${1:-}" in mysql | api | worker) ;; *) die "用法：logs mysql|api|worker [行数]" ;; esac
  [[ "${2:-100}" =~ ^[0-9]+$ ]] || die "行数要是数字"
  compose logs --tail "${2:-100}" "$1"
}

# ---- 首发前搬家（D-127） ----

# 导出窗口里临时起的容器（下载、试恢复、加密）用 worker 的内存上限（640M）：先停接口和 worker，
# 有据合计不超过 1.5G；结束时（成功、失败都一样）再起来。停着的这段时间本机推同步会失败、下一轮再推
pause_services() {
  # 先确认起得来再停：口令文件没放好时 compose 加载不了项目，停了就起不来
  db_env_ok
  compose stop api worker
  trap on_exit EXIT
}

# 退出时（成功、失败、被打断都一样）：试恢复开始了还没清 binlog 的先清，再把接口和 worker 起来。
# 关掉网页终端、断线时 compose 客户端死了，它起的一次性容器（下载、试恢复）还在跑：先停掉——不然
# 试恢复接着往 binlog 写，又和接口、worker 叠在一起超内存。试恢复收到停止信号会自己删临时库；
# 被强杀（内存不够被系统杀掉）的删不了，恢复演练（restore-drill）只跑这一次，等不到"下次开头删"，
# 所以它退出时在这里把留下的临时库删掉（DROP_DRILL_DBS）。
# 这里每一步失败都接着做下一步（不让 set -e 半途退出，服务至少要起来）
PURGE_BINLOG=""
DROP_DRILL_DBS=""
on_exit() {
  set +e
  local stray
  stray=$(docker ps -q --filter label=com.docker.compose.project=youju-server \
    --filter label=com.docker.compose.oneoff=True)
  # shellcheck disable=SC2086  # 可能有几个容器编号
  [ -z "$stray" ] || docker stop -t 60 $stray >/dev/null
  if [ -n "$DROP_DRILL_DBS" ]; then
    echo "恢复演练没收完尾：正式库不受影响；留下的临时库 youju_drill_…（有的话）现在删掉，再清 binlog" >&2
    drop_drill_dbs || echo "注意：临时库 youju_drill_… 没删掉，照手册第八节\"恢复演练\"查" >&2
  elif [ -n "$PURGE_BINLOG" ]; then
    echo "试恢复被打断：正式库不受影响；临时库 youju_drill_… 它自己会删，没删掉的下次试恢复开头会删" >&2
  fi
  if [ -n "$PURGE_BINLOG" ]; then
    purge_binlog || echo "注意：binlog 没清掉，再跑之前先查（手册第六节第 7 步、第八节\"恢复演练\"）" >&2
  fi
  resume_services
}

# 在 mysql 容器里跑（sh -s）：轮换 binlog，删掉当前这个以前的全部，再查只剩一个。不用
# PURGE … BEFORE NOW()：刚轮换出来的文件修改时间就是这一秒，同一秒里删不掉（本机 MySQL 8.0 实测）。
# SHOW MASTER STATUS 是 MySQL 8.0 的写法（compose 锁定的是 8.0）；升到 8.2 以后要改成
# SHOW BINARY LOG STATUS（8.4 删掉了旧的）
binlog_purge_sql() {
  cat <<'EOF'
set -eu
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
on=$(mysql -uroot -N -B -e 'SELECT @@log_bin')
if [ "$on" != 1 ]; then echo "binlog 没开，不用清"; exit 0; fi
current=$(mysql -uroot -N -B -e 'FLUSH BINARY LOGS; SHOW MASTER STATUS' | awk 'NR == 1 {print $1}')
case "$current" in "" | *[!A-Za-z0-9._-]*) echo "取不到当前的 binlog 文件名" >&2; exit 1 ;; esac
mysql -uroot -e "PURGE BINARY LOGS TO '$current'"
left=$(mysql -uroot -N -B -e 'SHOW BINARY LOGS' | wc -l)
[ "$left" -eq 1 ] || { echo "binlog 还剩 $left 个文件，没清干净" >&2; exit 1; }
echo "binlog 已清，只剩当前的 $current"
EOF
}

# 密码用容器里的环境变量给 mysql（MYSQL_PWD），不写成 -p 参数；脚本从标准输入给
purge_binlog() { binlog_purge_sql | compose exec -T mysql sh -s; }

# 在 mysql 容器里跑（sh -s）：打出 binlog 开没开（1 开、0 关）
binlog_state_sql() {
  cat <<'EOF'
set -eu
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
mysql -uroot -N -B -e 'SELECT @@GLOBAL.log_bin'
EOF
}

# 在 mysql 容器里跑（sh -s）：binlog 确实关着时，删掉数据目录里以前留下的 binlog 文件
# （binlog.<6 位编号> 和 binlog.index）；还开着、读不出就按失败退出，什么都不删
binlog_files_cleanup_sql() {
  cat <<'EOF'
set -eu
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
on=$(mysql -uroot -N -B -e 'SELECT @@GLOBAL.log_bin')
[ "$on" = 0 ] || { echo "binlog 还开着（${on:-读不出}），不删文件" >&2; exit 1; }
dir=$(mysql -uroot -N -B -e 'SELECT @@GLOBAL.datadir')
case "$dir" in /*/) ;; *) echo "读不出数据目录" >&2; exit 1 ;; esac
n=0
for f in "${dir}"binlog.[0-9][0-9][0-9][0-9][0-9][0-9] "${dir}binlog.index"; do
  [ -e "$f" ] || continue
  rm -f -- "$f"
  n=$((n + 1))
done
echo "删掉了以前留下的 binlog 文件 ${n} 个"
EOF
}

# 关掉 MySQL 的二进制日志（binlog）：没在用——不做主从，也没有按时刻恢复，恢复只用每天的备份。
# install 已经把带 disable_log_bin 的 mysql.cnf 拷到 ${SERVER}；MySQL 要重建一次容器才用上
# （mysql.cnf 是按文件挂进去的，up 看不出它变了）。先停接口和 worker（不让新的写入落进还开着的
# binlog），清掉现有的 binlog，再重建 MySQL、查 @@log_bin 是 0，删掉数据目录里留下的 binlog 文件
# （MySQL 之前已经用上新配置的，清 binlog 那步会打"binlog 没开，不用清"，旧文件就靠这一步删）；
# 最后把 worker、接口起来，起来了才打 BINLOG OFF。中途失败、被打断，on_exit 照样把它们起来。
# 再跑一遍也没事
binlog_off() {
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  grep -q '^disable_log_bin' "$SERVER/mysql.cnf" 2>/dev/null \
    || die "${SERVER}/mysql.cnf 里还没有 disable_log_bin：先 install、up 含这一改的版本"
  say "关掉 MySQL 的 binlog：停接口和 worker → 清掉现有的 binlog → 用新配置重建 MySQL → 查 @@log_bin 是 0 → 删留下的 binlog 文件 → 起 worker、接口"
  pause_services
  purge_binlog
  compose up -d --force-recreate --wait --wait-timeout 300 mysql
  local on
  on=$(binlog_state_sql | compose exec -T mysql sh -s | tr -d '[:space:]') \
    || die "查不出 MySQL 的 binlog 开没开（MySQL 连不上？）"
  [ "$on" = 0 ] || die "MySQL 重建以后 binlog 还开着（@@GLOBAL.log_bin=${on:-读不出}）"
  binlog_files_cleanup_sql | compose exec -T mysql sh -s \
    || die "binlog 关了，但数据目录里留下的 binlog 文件没删掉（原因见上面）"
  resume_services
  trap - EXIT
  echo "BINLOG OFF"
}
# 在 mysql 容器里跑（sh -s）：删掉恢复演练留下的临时库（名字和 youju/restore_drill.py 的 NAME 一样：
# youju_drill_<14 位时间>_<4 位十六进制>），别的库一概不碰
drill_drop_sql() {
  cat <<'EOF'
set -eu
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
# 先取名单（查不到就按失败退出，不当作没有），再逐个删
dbs=$(mysql -uroot -N -B -e "SELECT schema_name FROM information_schema.schemata WHERE schema_name REGEXP '^youju_drill_[0-9]{14}_[0-9a-f]{4}\$'")
for db in $dbs; do
  case "$db" in youju_drill_[0-9]*) ;; *) continue ;; esac
  mysql -uroot -e "DROP DATABASE IF EXISTS \`$db\`"
  echo "删掉了留下的临时库 $db"
done
EOF
}
drop_drill_dbs() { drill_drop_sql | compose exec -T mysql sh -s; }
resume_services() {
  if ! { compose up -d --wait --wait-timeout 900 worker && compose up -d --wait --wait-timeout 300 api; }; then
    echo "注意：接口、worker 没起来，运行 ${SELF} up" >&2
    exit 1
  fi
}

# 清单链接的格式。在 C 语言环境里比：bash 的 [[ =~ ]] 按当前语言环境的排序规则解释 [!-~] 这种范围，
# en_US.UTF-8 下字母不在 '!' 到 '~' 之间，合法链接也会判成"不对"（11/12 导出窗口实测：OrcaTerm 的
# root 就是 LANG=en_US.UTF-8）。整个函数在子 shell 里跑，LC_ALL 不影响外面
manifest_url_ok() (
  LC_ALL=C
  [[ "$1" =~ ^https://[A-Za-z0-9.-]+\.(tcb\.qcloud\.la|myqcloud\.com)/[!-~]*$ ]]
)

# 网页终端复制时常带上首尾的空格、制表符、回车：只去首尾的（sed 的 [[:space:]] 含回车），
# 中间带空白或回车的原样留着，manifest_url_ok 会判不对，不会悄悄拼成另一条链接
trim_pasted() { printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }

fetch_export() {
  local sum=${1:-} url=""
  [[ "$sum" =~ ^[0-9a-f]{64}$ ]] && [ "$#" -eq 1 ] \
    || die "用法：${SELF} fetch-export <哈希>，再按提示粘贴清单链接"
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  # 存档过了就不再下（不再下一份明文备份，也不白停一次服务）；真要重来先删这个标记
  [ ! -f "$EXPORT_DIR/imported" ] || die "这批已经存档过（IMPORT OK），不再下载；真要重来先删 ${EXPORT_DIR}/imported"
  # 链接只在交互终端里按提示粘贴（不显示）：不进命令行参数（别的用户 ps 看得到）、不进 shell 历史，
  # 也不能用"执行命令"跑（它会把输入存进控制台的执行记录）；经环境变量传进容器
  need_tty
  printf '粘贴清单链接（不显示），回车：'
  IFS= read -rs url || true
  echo
  url=$(trim_pasted "$url")
  manifest_url_ok "$url" \
    || die "清单链接不对（要是云托管导出脚本打印的那一行里的链接）"
  local free_g
  free_g=$(df -BG --output=avail "$BASE" | tail -1 | tr -dc '0-9')
  [ "$free_g" -ge 10 ] || die "硬盘只剩 ${free_g}G，不到 10G，先别下"
  install -d -m 700 "$EXPORT_DIR"
  pause_services
  YOUJU_EXPORT_URL="$url" compose run --rm --no-deps -e YOUJU_EXPORT_URL -v "$EXPORT_DIR:/export" worker \
    youju export-fetch --sha256 "$sum" --dir /export
  echo "下一步：${SELF} export-show 看汇总；${SELF} import-export <旧版本> 试恢复、加密存档"
}

export_show() {
  [ -f "$EXPORT_DIR/summary.json" ] || die "还没有导出汇总：先 fetch-export"
  cat "$EXPORT_DIR/summary.json"
  ls -l "$EXPORT_DIR" "$EXPORT_DIR/backups"
}

# 试恢复之前按备份解压后的大小核一次硬盘：临时库加 binlog 大约是它的 3 倍，再留 2G。
# 解压后的大小整个解压数一遍（gzip -l 读文件尾的记录，超过 4G 会绕回去）；按字节比，
# ${BASE}（加密副本）和 docker 的数据目录（MySQL 数据卷，临时库写在这里）各核一次
drill_space_ok() {
  local raw need free dir dirs n
  raw=$(gzip -dc "$1" | wc -c | tr -d ' ') || die "整库备份解压不了"
  [[ "$raw" =~ ^[0-9]+$ ]] || die "读不出整库备份解压后的大小"
  need=$(( raw * 3 + 2 * 1073741824 ))
  dirs=("$BASE")
  dir=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
  { [ -n "$dir" ] && [ -d "$dir" ]; } \
    || die "读不出 docker 的数据目录（docker info），没法核对 MySQL 数据卷那块盘的空间"
  dirs+=("$dir")
  free=""
  for dir in "${dirs[@]}"; do
    n=$(df -B1 --output=avail "$dir" | tail -1 | tr -dc '0-9')
    [ -n "$n" ] || die "读不出 ${dir} 所在硬盘的剩余空间"
    if [ -z "$free" ] || [ "$n" -lt "$free" ]; then free=$n; fi
  done
  [ "$free" -ge "$need" ] || die "硬盘不够试恢复：要 $(( (need + 1073741823) / 1073741824 ))G，现在只剩 $(( free / 1073741824 ))G（先腾空间，别动别的项目的文件）"
}

import_export() {
  local old=${1:-}
  [[ "$old" =~ ^[0-9a-f]{8}$ ]] || die "用法：import-export <旧版本号>（迁移版本和备份一样的那一版，如 a79a0042）"
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  [ ! -f "$EXPORT_DIR/imported" ] || die "这批已经存档过（IMPORT OK），不再重做"
  docker image inspect "youju-selfhost:$old" >/dev/null 2>&1 \
    || die "没有镜像 youju-selfhost:${old}（${BASE}/releases/${old} 还在的话，照手册重新构建这一版的镜像）"
  [ -f "$EXPORT_DIR/fetch-complete" ] \
    || die "下载还没全部完成（fetch-export 要看到 FETCH OK）：没下好的先重跑 fetch-export"
  local files=("$EXPORT_DIR"/backups/youju-*.jsonl.gz)
  [ -e "${files[0]}" ] || die "${EXPORT_DIR}/backups 里没有整库备份：先 fetch-export"
  [ -f "$EXPORT_DIR/pay-records.jsonl.gz" ] || die "没有交易记录：先 fetch-export"
  [ "${#files[@]}" -eq 1 ] || die "${EXPORT_DIR}/backups 里不止一份备份，只留要归档的那一份"
  drill_space_ok "${files[0]}"
  local name root_pw
  name=$(basename "${files[0]}")
  root_pw=$(root_password)
  [ -n "$root_pw" ] || die "${SERVER}/mysql-root.env 里没有 MYSQL_ROOT_PASSWORD（先 ${SELF} split-db-env）"
  pause_services
  say "1/3 试恢复：${old} 的镜像在临时库 youju_drill_… 里恢复一遍（核对条数、外键、每张表的 SHA-256），结束删掉；不碰正式库"
  # 建临时库要 root；连接串经环境变量传进容器（不进命令行参数）。临时库和正式库在同一个 MySQL 里
  local drill_ok=1
  PURGE_BINLOG=1
  YOUJU_IMAGE="youju-selfhost:$old" \
    YOUJU_DATABASE_URL="mysql+pymysql://root:${root_pw}@mysql:3306/youju?charset=utf8mb4" \
    compose run --rm --no-deps -e YOUJU_DATABASE_URL -v "$EXPORT_DIR:/export:ro" worker \
    youju restore-drill --file "/export/backups/$name" || drill_ok=0
  # 试恢复写进 binlog 的是旧库的明文（binlog 开着时）：成不成都清掉（中途被打断的，退出时清）。
  # 不做主从，binlog 没人用。清不干净就停下：不存档、不删明文
  say "    清掉 MySQL 的 binlog（试恢复写进去的是旧库的明文；binlog 关了以后这步打\"binlog 没开，不用清\"）"
  purge_binlog
  PURGE_BINLOG=""
  [ "$drill_ok" = 1 ] || die "试恢复没通过（原因见上面）：没存档、没删明文"
  say "2/3 加密存档：整库备份、交易记录、批量工具结果分别用 .env 里的 age 公钥加密，存到服务器存储和异地的"
  say "    archive/，到期日写在文件名里（整库自备份起 30 天；交易记录自最早一笔下单或赠送起 3650 天；"
  say "    批量工具结果自生成起 30 天），每天 archive-prune 到期删"
  compose run --rm --no-deps -v "$EXPORT_DIR:/export:ro" worker youju archive-export --dir /export
  say "3/3 删掉明文的整库备份、交易记录、批量工具结果和带链接的清单"
  rm -f "$EXPORT_DIR/backups/$name" "$EXPORT_DIR/pay-records.jsonl.gz" "$EXPORT_DIR/manifest.json" \
    "$EXPORT_DIR/advisor-out.json"
  rm -rf "$EXPORT_DIR/advisor-out"
  # 存档过了：以后再跑 fetch-export 会拒绝（不再下一份明文备份）
  date '+%F %T' > "$EXPORT_DIR/imported"
  echo "IMPORT OK"
}

# ---- 恢复演练（D-118 ②：提审前用服务器自己的备份做一次） ----

# 北京时间的时、分（HHMM）；测试里换掉
beijing_hhmm() { TZ=Asia/Shanghai date +%H%M; }

# 拿存储卷里最新的一份用户数据备份（每天 4:00 做的，D-129 ⑥），在临时库 youju_drill_… 里恢复一遍、
# 核对条数、外键和每张表的 SHA-256，结束删掉，不碰正式库。不在容器里直接跑 youju restore-drill：有据的
# 库账号建不了临时库，也不会先停 worker。这里和 import-export 一样用 root，口令从 .env 经环境变量传进
# 临时容器（不进命令行参数）。临时容器用 worker 那份内存（有据三个容器合计 1.5G）：先停 worker，接口
# 照常；结束（成功、失败、被打断都一样）再起来（on_exit）。worker 停着的这几分钟里定时任务不跑、本机推
# 同步会失败（下一轮补上），所以只在北京时间 10:00–23:30 跑，避开凌晨的备份、早上的提醒、日报和对账。
# 试恢复把一整份用户数据写进了 binlog，跑完清掉；演练进程被系统强杀留下的临时库也删掉（见 on_exit）。
# 两件都做完才撤掉收尾标记：哪一步失败，退出时 on_exit 再删一遍、再清一遍
restore_drill() {
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  local now
  now=$(beijing_hhmm)
  [[ "$now" =~ ^[0-9]{4}$ ]] || die "读不出现在的时间"
  if [ "$((10#$now))" -lt 1000 ] || [ "$((10#$now))" -gt 2330 ]; then
    die "现在是北京时间 ${now:0:2}:${now:2:2}：只在 10:00–23:30 之间跑（别的时段有每天的备份、提醒、日报和对账，停 worker 会让它们跳过）"
  fi
  local dir
  dir=$(docker volume inspect youju-server_storage --format '{{.Mountpoint}}' 2>/dev/null || true)
  { [ -n "$dir" ] && [ -d "$dir" ]; } || die "读不出存储卷的位置（docker volume inspect youju-server_storage）"
  # 文件名里是备份那天的日期，按名字排就是按日期排
  local files=("$dir"/backups/youju-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].jsonl.gz)
  [ -e "${files[0]}" ] || die "存储里还没有用户数据备份（每天 4:00 做，上线打开正式定时任务的第二天才有）"
  local name root_pw
  name=$(basename "${files[${#files[@]} - 1]}")
  drill_space_ok "$dir/backups/$name"
  root_pw=$(sed -n 's/^MYSQL_ROOT_PASSWORD=//p' "$SERVER/.env")
  [ -n "$root_pw" ] || die ".env 里没有 MYSQL_ROOT_PASSWORD"
  say "恢复演练：用户数据备份 ${name} 在临时库 youju_drill_… 里恢复一遍（核对条数、外键、每张表的 SHA-256），结束删掉；不碰正式库"
  say "先停 worker（定时任务、本机推同步停几分钟），接口照常；结束再起来"
  compose stop worker
  trap on_exit EXIT
  DROP_DRILL_DBS=1
  PURGE_BINLOG=1
  YOUJU_DATABASE_URL="mysql+pymysql://root:${root_pw}@mysql:3306/youju?charset=utf8mb4" \
    compose run --rm --no-deps -e YOUJU_DATABASE_URL worker \
    youju restore-drill --file "/data/storage/backups/$name" \
    || die "恢复演练没通过（原因见上面）"
  say "演练通过；删掉以前被打断留下的临时库（一般没有），再清 MySQL 的 binlog（试恢复写进了一份用户数据）"
  drop_drill_dbs || die "演练通过了，但留下的临时库没删掉（照手册第八节\"恢复演练\"做）"
  purge_binlog || die "演练通过了，但 binlog 没清掉（照手册第八节\"恢复演练\"做）"
  DROP_DRILL_DBS=""
  PURGE_BINLOG=""
  echo "DRILL OK ${name}"
}

takedowns() {
  local file=$EXPORT_DIR/takedowns.jsonl part=${1:-1}
  [ -f "$file" ] || die "还没有下架清单：先 fetch-export"
  [[ "$part" =~ ^[1-9][0-9]*$ ]] || die "段号要是正整数"
  local blob lines
  blob=$(gzip -9nc < "$file" | base64 -w 76)
  lines=$(printf '%s\n' "$blob" | wc -l)
  echo "下架清单 $(wc -l < "$file") 个网址；SHA-256 $(sha256sum < "$file" | cut -c1-64)；gzip 后 base64，共 $(( (lines + 199) / 200 )) 段，第 ${part} 段："
  printf '%s\n' "$blob" | sed -n "$(( (part - 1) * 200 + 1 )),$(( part * 200 ))p"
}

takedowns_check() {
  [ -f "$EXPORT_DIR/takedowns.jsonl" ] || die "还没有下架清单：先 fetch-export"
  # 在正在跑的 worker 里查（不另起容器）；清单从标准输入给
  compose exec -T worker youju takedown-carry check --file /dev/stdin < "$EXPORT_DIR/takedowns.jsonl"
}

# 紧急下架：都在正在跑的 worker 里执行（不另起容器；worker 没在跑就先 up）
emergency() {
  compose exec -T worker youju emergency-takedown "$@"
}

# 说明在提示里问（read 读进来的不进命令历史；经管道给容器里的命令，也不出现在进程列表里）。
# 没有终端（如控制台"执行命令"）时不问、不带说明：说明可能写到投诉人，不进执行记录。
# 先读完说明再启动容器里的命令：边问边启动的话，在提示处 Ctrl-C 只停了这边，容器里的命令
# 读到空说明照样执行（docker exec 的客户端退出不结束容器里的进程）
ask_note() {
  local note=""
  if [ -t 0 ]; then
    read -r -p "说明（可以不写，直接回车；别写投诉人的姓名、电话；Ctrl-C 取消）：" note || note=""
  fi
  printf '%s' "$note"
}

emergency_takedown() {
  [ "$#" -eq 2 ] || die "用法：emergency-takedown <文件编号> <原因：complaint|authority|error|other>（说明在提示里输入）"
  local note
  note=$(ask_note)
  printf '%s' "$note" | emergency add "$1" --reason "$2" --note-stdin --by "${YOUJU_BY:-}"
}

emergency_lift() {
  [ "$#" -eq 1 ] || die "用法：emergency-lift <文件编号>（说明在提示里输入）"
  local note
  note=$(ask_note)
  printf '%s' "$note" | emergency lift "$1" --note-stdin --by "${YOUJU_BY:-}"
}

# 和 takedowns 一样打印成 gzip 后 base64（中文原因贴进本机终端不会乱码），本机 carry-takedowns.sh
# <SHA-256> emergency 核对后应用。每次都重新导出：分几段抄的中间又有新的紧急下架，SHA-256 会对不上
emergency_carry() {
  local part=${1:-1} data blob lines
  [[ "$part" =~ ^[1-9][0-9]*$ ]] || die "段号要是正整数"
  data=$(emergency export) || die "导出紧急下架没成功"
  [ -n "$data" ] || { echo "没有紧急下架着的，不用带回"; return 0; }
  blob=$(printf '%s\n' "$data" | gzip -9nc | base64 -w 76)
  lines=$(printf '%s\n' "$blob" | wc -l)
  echo "紧急下架清单 $(printf '%s\n' "$data" | wc -l) 个网址；SHA-256 $(printf '%s\n' "$data" | sha256sum | cut -c1-64)；gzip 后 base64，共 $(( (lines + 199) / 200 )) 段，第 ${part} 段："
  printf '%s\n' "$blob" | sed -n "$(( (part - 1) * 200 + 1 )),$(( part * 200 ))p"
}

# .env 里的一项改成新值、没有就加上（先写临时文件再改名）。值经环境变量给 awk，不进命令行参数
set_env() {
  (
    umask 077
    KEY="$1" VALUE="$2" awk 'BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; done = 0 }
      index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
      { print }
      END { if (!done) print k "=" v }' "$SERVER/.env" > "$SERVER/.env.tmp"
  )
  mv -f "$SERVER/.env.tmp" "$SERVER/.env"
}

env_get() { sed -n "s/^$1=//p" "$SERVER/.env" | head -n 1; }

fingerprint() { printf '%s' "$1" | sha256sum | cut -c1-4; }

# 密钥的格式：对不上多半是复制少了、多了
secret_ok() {
  local re
  case "$1" in
    YOUJU_WX_APPSECRET) re='^[0-9a-f]{32}$' ;;
    YOUJU_OFFSITE_AGE_RECIPIENT) re='^age1[0-9a-z]{58}$' ;;
    YOUJU_ALERT_WEBHOOK) re='^https://[A-Za-z0-9.-]+/[A-Za-z0-9._~:/?=&%+-]*$' ;;
    *) re='^[A-Za-z0-9._+/=:-]{8,200}$' ;;
  esac
  [[ "$2" =~ $re ]]
}

need_tty() {
  [ -t 0 ] && [ -t 1 ] \
    || die "只能在交互终端里用（轻量控制台的'登录'网页终端）：'执行命令'会把输入输出存进控制台的执行记录"
}

secret() {
  local name=${1:-} value=""
  [[ " $(echo $SECRET_NAMES) " == *" $name "* ]] && [ -n "$name" ] \
    || die "用法：secret <名称>；名称是 $(echo $SECRET_NAMES)"
  need_tty
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  printf '粘贴 %s 的值（不显示），回车：' "$name"
  IFS= read -rs value || true
  echo
  value=${value%$'\r'}
  secret_ok "$name" "$value" || die "格式不对（${#value} 位），没有写"
  set_env "$name" "$value"
  echo "已写入 ${name}：${#value} 位，指纹 $(fingerprint "$value")。都填完运行 ${SELF} up 生效"
}

# show ai 打出来之前逐项核一遍：这几项只该是线路、模型名、附加参数。看着像夹带了密钥的不打：地址里有
# ?、@ 之类（只认 https://主机[:端口][/路径]），附加参数里有 authorization、api_key、token、authToken 之类的键，
# 或者 sk-、Bearer 开头的值。附加参数用 python 按 JSON 查（值从标准输入给，不进命令行参数）
ai_show_ok() {
  local name=$1 value=$2 port
  local key_like='(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}' long_run='[A-Za-z0-9]{32,}'
  [[ "$value" != *'$'* && "$value" != *' #'* && "$value" != \"* && "$value" != \'* ]] || return 1
  # 哪一项都不该像密钥：sk- 开头的一长串、32 个以上连着的字母数字（比如模型名误填了密钥）
  [[ ! "$value" =~ $key_like && ! "$value" =~ $long_run ]] || return 1
  case "$name" in
    YOUJU_AI_BASE_URL)
      [[ "$value" =~ ^https://[A-Za-z0-9.-]+(:([0-9]{1,5}))?(/[A-Za-z0-9._~/-]*)?$ ]] || return 1
      port=${BASH_REMATCH[2]}
      [ -z "$port" ] || { [ $((10#$port)) -ge 1 ] && [ $((10#$port)) -le 65535 ]; }
      ;;
    YOUJU_AI_MODEL | YOUJU_AI_BACKGROUND_MODEL) [[ "$value" =~ ^[A-Za-z0-9._:/-]{1,100}$ ]] ;;
    YOUJU_AI_REQUEST_EXTRA)
      [ "${#value}" -le 2000 ] || return 1
      local check='
import json, re, sys
# 键名：去掉分隔符以后含这些的都算（apisecret、APISecret、client_secret 都拦）；再拆成词（驼峰、缩写
# 也拆：authToken -> auth token），词是 token、key、auth、pwd 或以 token 结尾的也算；max_tokens 不算
STRONG = ("secret", "password", "passwd", "credential", "authorization", "apikey", "accesskey", "privatekey", "cookie", "bearer")
WORDS = {"token", "key", "auth", "pwd"}
def bad_key(name):
    name = str(name)
    if any(w in re.sub(r"[^a-z0-9]", "", name.lower()) for w in STRONG):
        return True
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    return any(w in WORDS or w.endswith("token") for w in re.split(r"[^a-z0-9]+", spaced))
bad_value = re.compile(r"(?i)^(sk-|bearer\s)")
def ok(x):
    if isinstance(x, dict):
        return all(not bad_key(k) and ok(v) for k, v in x.items())
    if isinstance(x, list):
        return all(ok(v) for v in x)
    if isinstance(x, str):
        return not bad_value.search(x)
    return True
value = json.load(sys.stdin)
sys.exit(0 if isinstance(value, dict) and ok(value) else 1)
'
      if command -v python3 >/dev/null 2>&1; then
        printf '%s' "$value" | python3 -I -c "$check" >/dev/null 2>&1
      else
        printf '%s' "$value" | compose exec -T worker python -I -c "$check" >/dev/null 2>&1
      fi
      ;;
    *) return 1 ;;
  esac
}

# 附加参数（已经核过是 JSON 对象）按纯 ASCII、紧凑写法重写：值从标准输入给，不进命令行参数
ai_extra_ascii() {
  local code='import json, sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=True, separators=(",", ":")))'
  if command -v python3 >/dev/null 2>&1; then
    python3 -I -c "$code"
  else
    compose exec -T worker python -I -c "$code"
  fi
}

show() {
  need_tty
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  case "${1:-}" in
    push)
      echo "消息推送 Token：$(env_get YOUJU_WX_PUSH_TOKEN)"
      echo "消息推送 EncodingAESKey：$(env_get YOUJU_WX_PUSH_AES_KEY)"
      echo "看完关掉这个终端窗口"
      ;;
    sync)
      echo "同步密钥：$(env_get YOUJU_SYNC_SECRET)"
      echo "看完关掉这个终端窗口"
      ;;
    ai)
      # 本机的后台 AI 要和服务器同一条线路、同一个模型：全部几行照抄到本机 set-config.sh（它把这几项在
      # 本机改成和这里一样：没打出来的，本机也去掉、用程序默认的）。密钥不打，只给长度和指纹。
      # 最后一行以 "#END <项数>" 开头（纯 ASCII，网页终端复制中文出乱码也认得出）：set-config.sh 读到它、
      # 核对项数，才算复制全了（漏了开头几行，本机会被改回默认线路）
      local name value count=0 lines=""
      # 每项只读一次：核过、转写过、再按本机的规矩核一遍，全部没问题才一次打出来；有一项不对一行都不打
      for name in YOUJU_AI_BASE_URL YOUJU_AI_MODEL YOUJU_AI_BACKGROUND_MODEL YOUJU_AI_REQUEST_EXTRA; do
        value=$(env_get "$name")
        [ -n "$value" ] || continue
        ai_show_ok "$name" "$value" \
          || die "${name} 看着不像正常的线路、模型名或附加参数（可能夹带了密钥），一行都没打：先把它改对（${SELF} env-set ${name}）再来"
        if [ "$name" = YOUJU_AI_REQUEST_EXTRA ]; then
          # 附加参数按纯 ASCII 重写（中文写成 \uXXXX，意思不变）：网页终端复制中文到 UU 可能变成乱码，
          # 乱码也是合法 JSON，会被悄悄写进本机；本机 set-config.sh 只收纯 ASCII。重写以后可能变长，
          # \u0024 这类转义也会还原成 $，所以按本机的规矩再核一遍（不超过 2000 字、没有 $ 和 " #"）
          value=$(printf '%s' "$value" | ai_extra_ascii) \
            || die "${name} 转不成纯 ASCII，一行都没打：要手动看"
          # 转写以后整套再核一遍：转义藏着的 $、长串、像密钥的值这时都现出来了
          ai_show_ok "$name" "$value" \
            || die "${name} 转成纯 ASCII 以后本机收不了（超过 2000 字、有 \$ 或 \" #\"，或者看着像密钥），一行都没打：要手动看"
        fi
        lines="${lines}${name}=${value}"$'\n'
        count=$((count + 1))
      done
      printf '%s' "$lines"
      value=$(env_get YOUJU_AI_API_KEY)
      if [ -n "$value" ]; then
        echo "#END ${count} （以上 ${count} 项；YOUJU_AI_API_KEY 不打印：${#value} 位，指纹 $(fingerprint "$value")；本机用 set-secret.sh 录同一个，指纹要一样）"
      else
        echo "#END ${count} （以上 ${count} 项；服务器上还没录 YOUJU_AI_API_KEY：先录，再给本机录同一个）"
      fi
      ;;
    *) die "用法：show push|sync|ai" ;;
  esac
}

rotate_sync() {
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  local value
  value=$(openssl rand -hex 32)
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || die "生成同步密钥失败"
  set_env YOUJU_SYNC_SECRET "$value"
  echo "已换同步密钥：64 位，指纹 $(fingerprint "$value")（旧的作废）。"
  echo "接着：${SELF} up；在交互终端里 ${SELF} show sync，填到本机（set-secret.sh YOUJU_SYNC_SECRET）"
}

# 不涉密的一项能不能这样写进 .env：名称是 YOUJU_ 开头、不是密钥；值写成不加引号的一行要能原样读回来
# （不能有换行、$、" #"，不能以引号开头）
plain_ok() {
  local name=$1 value=$2
  [[ "$name" =~ ^YOUJU_[A-Z0-9_]{1,60}$ ]] || return 1
  [[ " $(echo $SECRET_NAMES $PROTECTED_NAMES) " != *" $name "* ]] || return 1
  [[ "$value" != *'$'* && "$value" != *' #'* && "$value" != \"* && "$value" != \'* ]] || return 1
  [ "${#value}" -le 4000 ]
}

env_set() {
  local name=${1:-} value=""
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  IFS= read -r value || true
  value=${value%$'\r'}
  plain_ok "$name" "$value" \
    || die "${name:-（没写名称）} 不能这样改：密钥用 secret；值里不能有换行、\$、' #'，不能以引号开头"
  set_env "$name" "$value"
  echo "已写入 ${name}=${value}；运行 ${SELF} up 生效"
}

env_unset() {
  local name=${1:-}
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  plain_ok "$name" "" || die "${name:-（没写名称）} 不能这样删"
  (
    umask 077
    KEY="$name" awk 'index($0, ENVIRON["KEY"] "=") != 1' "$SERVER/.env" > "$SERVER/.env.tmp"
  )
  mv -f "$SERVER/.env.tmp" "$SERVER/.env"
  echo "已删掉 ${name}；运行 ${SELF} up 生效"
}

config_from_export() {
  [ -f "$EXPORT_DIR/summary.json" ] || die "还没有导出汇总：先 fetch-export"
  [ -f "$SERVER/.env" ] || die "还没装（没有 ${SERVER}/.env）"
  # 在正在跑的 worker 里把配置转成 .env 的写法（字典、列表写 JSON，真假写 true/false），每项一行"名称<TAB>值"
  local lines name value n=0
  lines=$(compose exec -T worker python -I -c '
import json, sys
for name, value in sorted((json.load(sys.stdin).get("config") or {}).items()):
    if value is None:
        continue
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    print(name + "\t" + text.replace("\n", "$").replace("\t", "$"))
' < "$EXPORT_DIR/summary.json")
  while IFS=$'\t' read -r name value; do
    [ -n "$name" ] || continue
    if plain_ok "$name" "$value"; then
      set_env "$name" "$value"
      n=$((n + 1))
      echo "  ${name}=${value}"
    else
      echo "  跳过 ${name}：这样写不进 .env，要手动看" >&2
    fi
  done <<< "$lines"
  echo "已按云托管的值写入 ${n} 项；运行 ${SELF} up 生效"
}

cloudrun_gone() {
  case "${1:-}" in deregistered | deleted) ;; *) die "用法：cloudrun-gone deregistered|deleted" ;; esac
  compose exec -T worker youju cloudrun-gone --how "$1"
}

alert_probe() {
  compose exec -T worker youju alert-probe --send
}

# 第六节第 9 步以后退回过云托管、再切回服务器时（手册第七节）：上一批已经存档，清掉它的标记，
# 才能重走第 4–9 步导出新的一批。明文的备份、交易记录应该在存档时就删了，还在就停下查明
export_reset() {
  [ -f "$EXPORT_DIR/imported" ] || die "${EXPORT_DIR} 里没有存档过的一批（没有 imported 标记），不用重置"
  local left=("$EXPORT_DIR"/backups/youju-*.jsonl.gz)
  if [ -e "${left[0]}" ] || [ -e "$EXPORT_DIR/pay-records.jsonl.gz" ]; then
    die "${EXPORT_DIR} 里还有明文的整库备份或交易记录（存档时应该已经删了）：先查明再重置"
  fi
  rm -f "$EXPORT_DIR/imported" "$EXPORT_DIR/fetch-complete" "$EXPORT_DIR/summary.json"
  echo "已清掉上一批的标记（下载过、没变的日志和附件照样按 ETag 跳过）；照手册第六节第 4–9 步重走一遍"
}

uninstall() {
  local purge=0
  [ "${1:-}" = "--purge" ] && purge=1
  if [ -f "$SERVER/.env" ]; then
    if [ "$purge" = 1 ]; then compose down -v --remove-orphans; else compose down --remove-orphans; fi
  fi
  if [ -f "$SITE" ]; then
    rm -f "$SITE"
    if "$NGINX" -t; then
      "$NGINX" -s reload
    else
      echo "注意：nginx 检查没过（别的站点的问题？），没有重载；有据的站点文件已删，下次重载时生效" >&2
    fi
  fi
  rm -f "$CRON_FILE"
  if [ "$purge" = 1 ]; then
    local mysql_image=""
    [ -f "$SERVER/docker-compose.yml" ] \
      && mysql_image=$(grep -o -m1 'mysql:[^ ]*@sha256:[0-9a-f]*' "$SERVER/docker-compose.yml" || true)
    docker image ls --format '{{.Repository}}:{{.Tag}}' | { grep -E '^youju-(selfhost|base):' || true; } \
      | xargs -r docker image rm
    # 有容器在用的镜像 docker 会拒绝删；构建缓存是全机共用的，不动
    # shellcheck disable=SC2086
    docker image rm $mysql_image "$CERTBOT_IMAGE" >/dev/null 2>&1 || true
    rm -rf "$SERVER" "$LE_DIR" "$WEBROOT" "$BASE/releases" "$BASE/nginx" "$BASE/staging" "$LOADED_FILE" \
      "$EXPORT_DIR"
    echo "已删除有据的容器、数据卷、镜像、证书和 ${BASE} 下的文件"
  else
    echo "已停掉有据、撤掉站点和定时任务；数据卷和 ${SERVER} 下的配置、口令文件留着（${SELF} up 重新启动）"
  fi
  echo "用户 youju 另用 unkey 删；桌面服务另用 desktop on 恢复"
}

[ "$(id -u)" -eq 0 ] || die "要用 root 运行"
cmd=${1:-}
[ "$#" -gt 0 ] && shift
case "$cmd" in
  check) need_domain check; check ;;
  sites) sites "$@" ;;
  desktop) desktop "$@" ;;
  key) key "$@" ;;
  unkey) unkey ;;
  unpack) unpack "$@" ;;
  install) install_release "$@" ;;
  env) make_env "$@" ;;
  split-db-env) split_db_env ;;
  unsplit-db-env) unsplit_db_env ;;
  up) up ;;
  site) need_domain site; site "$@" ;;
  cert) need_domain cert; cert ;;
  renew) need_domain renew; renew ;;
  status) status ;;
  logs) logs "$@" ;;
  uninstall) need_domain uninstall; uninstall "$@" ;;
  fetch-export) fetch_export "$@" ;;
  export-show) export_show ;;
  import-export) import_export "$@" ;;
  takedowns) takedowns "$@" ;;
  takedowns-check) takedowns_check ;;
  emergency-find) emergency find "$*" ;;
  emergency-takedown) emergency_takedown "$@" ;;
  emergency-lift) emergency_lift "$@" ;;
  emergency-list) emergency list "$@" ;;
  emergency-carry) emergency_carry "$@" ;;
  emergency-settle) emergency settle ;;
  secret) secret "$@" ;;
  show) show "$@" ;;
  rotate-sync) rotate_sync ;;
  env-set) env_set "$@" ;;
  env-unset) env_unset "$@" ;;
  config-from-export) config_from_export ;;
  cloudrun-gone) cloudrun_gone "$@" ;;
  alert-probe) alert_probe ;;
  export-reset) export_reset ;;
  binlog-off) binlog_off ;;
  restore-drill) restore_drill ;;
  *) awk 'NR > 1 && /^#/ {print; next} NR > 1 {exit}' "$0"; exit 1 ;;
esac
