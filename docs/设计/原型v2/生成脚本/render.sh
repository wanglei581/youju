#!/bin/bash
# 并行渲染效果图：参数为 名称:高度 ...
# 无界面 Chrome 截完图后常常不退出，所以截图文件写完、大小稳定后就结束它。
C="$(cd "$(dirname "$0")" && pwd)"
CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
render_one() {
  n=${1%%:*}; h=${1##*:}
  out="$C/static/$n.png"; rm -f "$out"
  "$CH" --headless=new --disable-gpu --hide-scrollbars --no-first-run --user-data-dir="$C/chrome-profile-$n" \
    --force-device-scale-factor=2 --window-size=390,$h --virtual-time-budget=8000 \
    --screenshot="$out" "file://$C/static/$n.html" >/dev/null 2>&1 &
  pid=$!
  for _ in $(seq 1 150); do
    if [ -s "$out" ]; then
      a=$(wc -c < "$out"); sleep 1; b=$(wc -c < "$out")
      [ "$a" = "$b" ] && break
    else
      sleep 1
    fi
  done
  kill "$pid" 2>/dev/null; pkill -f "chrome-profile-$n" 2>/dev/null; wait "$pid" 2>/dev/null
  echo "$n $(wc -c < "$out" 2>/dev/null || echo 失败)"
}
export -f render_one; export C CH
printf '%s\n' "$@" | xargs -P 4 -I{} bash -c 'render_one {}'
