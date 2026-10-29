#!/bin/bash
# 渲染效果图：先按 390×3200 截一张量出画面高度（单屏页面底色是品红，画面以外都是品红），再按实际高度重截。
# 用法：render.sh <单屏 HTML 目录> <效果图目录> 屏号...
# 无界面 Chrome 截完图后常常不退出，所以截图文件写完、大小稳定后就结束它（同原型 v2 的 render.sh）。
SRC="$(cd "$1" && pwd)"; OUT="$(mkdir -p "$2" && cd "$2" && pwd)"; shift 2
CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
export SRC OUT CH
shot() {
  n=$1; h=$2; out=$3
  rm -f "$out"
  prof=$(mktemp -d)  # 每次截图用一次性的 Chrome 配置目录，截完删掉（每个几 MB，留着占硬盘）
  "$CH" --headless=new --disable-gpu --hide-scrollbars --no-first-run --user-data-dir="$prof" \
    --force-device-scale-factor=2 --window-size=390,$h --virtual-time-budget=8000 \
    --screenshot="$out" "file://$SRC/$n.html" >/dev/null 2>&1 &
  pid=$!
  for _ in $(seq 1 150); do
    if [ -s "$out" ]; then
      a=$(wc -c < "$out"); sleep 1; b=$(wc -c < "$out")
      [ "$a" = "$b" ] && break
    else
      sleep 1
    fi
  done
  kill "$pid" 2>/dev/null; pkill -f "$prof" 2>/dev/null; wait "$pid" 2>/dev/null
  rm -rf "$prof"
}
one() {
  n=$1
  shot "$n" 3200 "$SRC/$n.measure.png"
  h=$(python3 -c "
from PIL import Image
im = Image.open('$SRC/$n.measure.png').convert('RGB'); w, H = im.size; px = im.load()
y = H - 1
while y > 0 and all(px[x, y] == (255, 0, 255) for x in range(0, w, 7)): y -= 1
print((y + 1) // 2)")
  rm -f "$SRC/$n.measure.png"
  shot "$n" "$h" "$OUT/$n.png"
  echo "$n ${h}px $(wc -c < "$OUT/$n.png" 2>/dev/null || echo 失败)"
}
export -f shot one
printf '%s\n' "$@" | xargs -P 4 -I{} bash -c 'one {}'
