#!/bin/sh
# 生成贴进云托管 admin "登录实例"终端的一段命令（首发前搬家 D-127，手册第六节）：把 cloudrun-export.py
# 压缩后按 base64 写进容器的 /tmp，核对 SHA-256 再运行。整段只有英文和数字：容器终端贴中文会乱码；
# 每行不长（终端一行太长会被截断）。不用 tee 存输出：输出里有清单链接（2 小时内能下整库备份），
# 不留在容器的文件里。
# 用法：sh export-paste.sh [--links-only] > 某个文件；把文件内容整段复制、贴进终端。
set -eu
here=$(cd "$(dirname "$0")" && pwd)
src=$here/cloudrun-export.py
case "${1:-}" in "" | --links-only) ;; *) echo "用法：sh export-paste.sh [--links-only]" >&2; exit 1 ;; esac
sum=$(openssl dgst -sha256 -r "$src" | cut -c1-64)
echo "cat > /tmp/youju-export.b64 <<'YOUJU_EOF'"
gzip -9nc < "$src" | openssl base64
echo "YOUJU_EOF"
echo "python3 -I -c \"import base64,gzip;open('/tmp/youju-export.py','wb').write(gzip.decompress(base64.b64decode(open('/tmp/youju-export.b64').read())))\" && echo '$sum  /tmp/youju-export.py' | sha256sum -c - && python3 -I /tmp/youju-export.py ${1:-}"
