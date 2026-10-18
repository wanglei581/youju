"""有据公众号的栏目封面：申报周报、临期提醒、名单速报、使用说明，各出 2.35:1 和 1:1 两版 SVG。

微信一篇文章只有一张封面图，2.35:1（消息列表大图）和 1:1（转发卡片、次条小图）是从这一张图里各裁一块
（草稿箱接口的 cover_info.crop_percent_list，旧版文档叫 pic_crop_235_1、pic_crop_1_1）。所以封面做成 2.35:1，正中的方块单独看就是一张完整的 1:1：
上传 2.35:1 那张，1:1 的裁剪框放在正中。1:1 的 SVG 只是同一画面换个 viewBox，内容保证一致。

样式沿用小程序"风格 C 精修版"（D-014）：首页顶部的品牌蓝渐变、首页入口的渐变图标块（同一套 TDesign 图标）、
白字。文字全部转成路径、logo 以 PNG 内嵌，SVG 不依赖本机字体。

用法（在仓库根目录）：
  python covers.py --font <Noto Sans SC 500 字重> --logo <designs/品牌/有据-logo-v1.png> --out designs/公众号/封面
依赖 fonttools、brotli（读 woff2）、Pillow；字体和 logo 的来源见 designs/公众号/README.md。
"""

import argparse
import base64
import io
import re
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[3]
ICON_CSS = REPO / "apps/miniapp/components/icon/index.wxss"

# 画布按 2 倍图出：1880×800 是 2.35:1（微信建议 900×383）；正中 800×800 是 1:1
W, H = 1880, 800
SQ_X = (W - H) // 2

# 颜色取自小程序 app.wxss 和首页（pages/home/index.wxss）
BG_TOP, BG_BOTTOM = "#2F66F0", "#2553D6"  # 首页顶部渐变 --brand-g1 → --brand-g2
WHITE = "#FFFFFF"
BRAND = "#2A5CE0"
TILES = {  # 首页入口图标块：145° 渐变，白色图标
    "blue": ("#5B8CFF", "#2A5CE0"),
    "green": ("#3FD0A6", "#13A677"),
    "orange": ("#FFAB5C", "#F57B21"),
}
LIGHT_TILE = "#F2F4F8"  # 浅色块（小程序页面底色）配品牌蓝图标

# 栏目：名称、图标（小程序图标字体里的 TDesign 图标名）、图标块颜色。
# 临期提醒、名单速报和小程序首页的"截止""查名单"入口用同一个图标和颜色，读者在两边看到的是同一个东西。
# 使用说明用浅色块：和申报周报的蓝色块区分开，缩成转发卡片的小图也认得出（Codex 审阅）。
COLUMNS = [
    {"name": "申报周报", "icon": "calendar", "tile": "blue"},
    {"name": "临期提醒", "icon": "hourglass", "tile": "green"},
    {"name": "名单速报", "icon": "bulletpoint", "tile": "orange"},
    {"name": "使用说明", "icon": "info-circle", "tile": "light"},
]

# 版式（2 倍图像素）
BRAND_X, BRAND_Y, LOGO_SIZE = 72, 64, 104  # 左上角 logo 和"有据"，同小程序首页顶部的品牌位置
BRAND_TEXT = 64
TILE = 236
TILE_RADIUS = TILE * 26 / 84  # 小程序图标块 84rpx 宽、圆角 26rpx
# 图标、栏目名按转发卡片里 48pt 的小图定：栏目名约 9pt、图标约 8pt 才能一眼认出（Codex 审阅）
ICON = TILE * 0.58
TITLE = 148
TITLE_TRACKING = 0.04  # 字距 0.04 em
GAP = 48  # 图标块和栏目名之间


def num(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


class Font:
    """把文字转成 SVG 路径（字形坐标 y 轴向上，这里翻转并缩放到字号）。"""

    def __init__(self, path):
        self.font = TTFont(path)
        self.glyphs = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap()
        self.upm = self.font["head"].unitsPerEm
        self.hmtx = self.font["hmtx"]

    def width(self, text, size, tracking=0.0):
        adv = sum(self.hmtx[self.cmap[ord(c)]][0] for c in text) * size / self.upm
        return adv + tracking * size * (len(text) - 1)

    def path(self, text, size, x, center_y, tracking=0.0):
        # 思源黑体的汉字字面框是 -0.12…0.88 em，视觉中心在基线上方 0.38 em
        baseline = center_y + 0.38 * size
        s = size / self.upm
        parts = []
        for c in text:
            name = self.cmap[ord(c)]
            pen = SVGPathPen(self.glyphs, ntos=num)
            self.glyphs[name].draw(TransformPen(pen, (s, 0, 0, -s, x, baseline)))
            parts.append(pen.getCommands())
            x += self.hmtx[name][0] * s + tracking * size
        return "".join(parts)


class Icons:
    """小程序图标组件内嵌的图标字体（TDesign 图标，MIT），按图标名取字形。"""

    def __init__(self, css_path):
        css = css_path.read_text(encoding="utf-8")
        data = re.search(r"base64,([A-Za-z0-9+/=]+)", css).group(1)
        self.font = TTFont(io.BytesIO(base64.b64decode(data)))
        self.glyphs = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap()
        self.upm = self.font["head"].unitsPerEm
        self.codes = {
            name: int(code, 16)
            for name, code in re.findall(r"\.yj-icon-([a-z0-9-]+):before \{ content: '\\([0-9a-fA-F]+)'; \}", css)
        }

    def path(self, name, cx, cy, size):
        """字形按外框居中放在 (cx, cy)，外框边长 size 对应字体的 1 em。"""
        glyph = self.cmap[self.codes[name]]
        bounds = BoundsPen(self.glyphs)
        self.glyphs[glyph].draw(bounds)
        x0, y0, x1, y1 = bounds.bounds
        s = size / self.upm
        pen = SVGPathPen(self.glyphs, ntos=num)
        transform = (s, 0, 0, -s, cx - (x0 + x1) / 2 * s, cy + (y0 + y1) / 2 * s)
        self.glyphs[glyph].draw(TransformPen(pen, transform))
        return pen.getCommands()


def logo_png(path, size):
    """logo 原图是满版的米色圆角方块（RGB，四角是白色）。按原图圆角（半径 240/1254）抠掉四角，
    再向里收 0.5% 去掉白边，缩到放置尺寸的 2 倍，PNG 以 base64 内嵌。"""
    big = size * 8
    img = Image.open(path).convert("RGB").resize((big, big), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (big, big), 0)
    inset = round(big * 0.005)
    radius = round(big * 240 / 1254) - inset
    ImageDraw.Draw(mask).rounded_rectangle([inset, inset, big - 1 - inset, big - 1 - inset], radius=radius, fill=255)
    img.putalpha(mask)
    img = img.resize((size * 2, size * 2), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def cover(column, font, icons, logo):
    light = column["tile"] == "light"
    top_color, bottom_color = (LIGHT_TILE, LIGHT_TILE) if light else TILES[column["tile"]]
    defs = (
        "<defs>"
        f'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{BG_TOP}"/><stop offset="1" stop-color="{BG_BOTTOM}"/></linearGradient>'
        # 145°：和小程序的 linear-gradient(145deg, …) 一致（正方形上渐变线的起止点按 CSS 的算法换算）
        f'<linearGradient id="tile" x1="0.1" y1="-0.07" x2="0.9" y2="1.07">'
        f'<stop offset="0" stop-color="{top_color}"/><stop offset="1" stop-color="{bottom_color}"/></linearGradient>'
        '<filter id="shadow" x="-30%" y="-30%" width="160%" height="170%">'
        '<feDropShadow dx="0" dy="6" stdDeviation="10" flood-color="#0A1E5A" flood-opacity="0.15"/></filter>'
        "</defs>"
    )
    parts = [defs, f'<rect width="{W}" height="{H}" fill="url(#bg)"/>']

    # 左上角：logo + "有据"（只在 2.35:1 里出现；1:1 转发卡片下方本来就带公众号头像和名称）
    parts.append(
        f'<image x="{BRAND_X}" y="{BRAND_Y}" width="{LOGO_SIZE}" height="{LOGO_SIZE}" '
        f'href="data:image/png;base64,{logo}"/>'
    )
    text_x = BRAND_X + LOGO_SIZE + 28
    parts.append(
        f'<path fill="{WHITE}" d="{font.path("有据", BRAND_TEXT, text_x, BRAND_Y + LOGO_SIZE / 2, 0.06)}"/>'
    )

    # 正中（就是 1:1 的画面）：图标块 + 栏目名，整体上下居中
    stack = TILE + GAP + TITLE
    tile_y = (H - stack) / 2
    tile_x = W / 2 - TILE / 2
    parts.append(
        f'<rect filter="url(#shadow)" x="{num(tile_x)}" y="{num(tile_y)}" width="{TILE}" height="{TILE}" '
        f'rx="{num(TILE_RADIUS)}" fill="url(#tile)"/>'
    )
    if not light:
        # 图标块内侧一圈淡淡的白边：蓝色图标块放在蓝底上时边缘才看得清
        parts.append(
            f'<rect x="{num(tile_x + 1)}" y="{num(tile_y + 1)}" width="{TILE - 2}" height="{TILE - 2}" '
            f'rx="{num(TILE_RADIUS - 1)}" fill="none" stroke="{WHITE}" stroke-opacity="0.2" stroke-width="2"/>'
        )
    icon_color = BRAND if light else WHITE
    parts.append(f'<path fill="{icon_color}" d="{icons.path(column["icon"], W / 2, tile_y + TILE / 2, ICON)}"/>')
    title_w = font.width(column["name"], TITLE, TITLE_TRACKING)
    title_cy = tile_y + TILE + GAP + TITLE / 2
    parts.append(
        f'<path fill="{WHITE}" d="{font.path(column["name"], TITLE, W / 2 - title_w / 2, title_cy, TITLE_TRACKING)}"/>'
    )
    # 保证栏目名完整落在正中方块里（1:1 裁出来不会切字）
    assert SQ_X + 60 <= W / 2 - title_w / 2 and W / 2 + title_w / 2 <= SQ_X + H - 60, column["name"]
    return "".join(parts)


def svg(inner, title, view_x, width):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{H}" '
        f'viewBox="{view_x} 0 {width} {H}" role="img" aria-label="{title}"><title>{title}</title>{inner}</svg>\n'
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--font", required=True, help="Noto Sans SC（思源黑体）500 字重，woff2/otf/ttf")
    ap.add_argument("--logo", required=True, help="designs/品牌/有据-logo-v1.png")
    ap.add_argument("--out", required=True, help="输出目录")
    args = ap.parse_args()

    font = Font(args.font)
    icons = Icons(ICON_CSS)
    logo = logo_png(args.logo, LOGO_SIZE)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for column in COLUMNS:
        inner = cover(column, font, icons, logo)
        name = column["name"]
        (out / f"{name}_2.35比1.svg").write_text(svg(inner, f"有据 · {name}", 0, W), encoding="utf-8")
        (out / f"{name}_1比1.svg").write_text(svg(inner, f"有据 · {name}", SQ_X, H), encoding="utf-8")
        print(name)


if __name__ == "__main__":
    main()
