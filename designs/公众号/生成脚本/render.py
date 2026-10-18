"""用本机 Chrome（无界面）把封面 SVG 渲染成同尺寸的 PNG：python render.py <svg>...

无界面 Chrome 截完图后常常不退出，所以等 PNG 写完、大小不再变化就结束它（同 docs/设计/原型v2/生成脚本/render.sh）。
截出来的图再用 Pillow 存成不带透明通道的 PNG，体积小一些。
"""

import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def svg_size(svg):
    head = svg.read_text(encoding="utf-8")[:300]
    width = int(head.split('width="', 1)[1].split('"', 1)[0])
    height = int(head.split('height="', 1)[1].split('"', 1)[0])
    return width, height


def render(src, size=None, out=None, trim_to=None):
    """src 是 SVG 或 HTML；size 不给时取 SVG 的宽高。trim_to 给一个背景色时，把底部多出的空白裁掉（长页面用）。"""
    src = Path(src).resolve()
    width, height = size or svg_size(src)
    out = Path(out).resolve() if out else src.with_suffix(".png")
    out.unlink(missing_ok=True)
    profile = tempfile.mkdtemp(prefix="cover-chrome-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
         f"--user-data-dir={profile}", "--force-device-scale-factor=1", f"--window-size={width},{height}",
         "--virtual-time-budget=5000", f"--screenshot={out}", src.as_uri()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    last = -1
    for _ in range(120):
        time.sleep(0.5)
        if out.exists():
            size_now = out.stat().st_size
            if size_now and size_now == last:
                break
            last = size_now
    proc.kill()
    proc.wait()
    shutil.rmtree(profile, ignore_errors=True)
    if not out.exists():
        return f"{src.name}：渲染失败"
    img = Image.open(out).convert("RGB")
    assert img.size == (width, height), (src.name, img.size)
    if trim_to:
        bottom = height
        while bottom > 1 and all(img.getpixel((x, bottom - 1)) == trim_to for x in range(0, width, 5)):
            bottom -= 1
        img = img.crop((0, 0, width, min(height, bottom + 24)))
    img.save(out, "PNG", optimize=True)
    return f"{out.name} {img.size[0]}×{img.size[1]} {out.stat().st_size // 1024} KB"


if __name__ == "__main__":
    with ThreadPoolExecutor(4) as pool:
        for line in pool.map(render, sys.argv[1:]):
            print(line)
