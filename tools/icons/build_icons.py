#!/usr/bin/env python3
"""生成小程序图标组件的样式：apps/miniapp/components/icon/index.wxss。

TDesign 的 t-icon 从 https://tdesign.gtimg.com 在线加载图标字体，外部网址不可用时图标会消失。
这里把 TDesign 图标字体（tdesign-icons，MIT 许可）只保留 icons.txt 里列出的字形，压成 woff，
以 base64 内嵌进组件样式，小程序不再从网络加载字体。

字形和编码的对应关系取自 TDesign 小程序组件自带的 icon.wxss
（.t-icon-xxx:before{content:'\\E00D'}），并和字体里的字形名逐个核对
（TDesign 字体的字形名就是图标名）。

用法（仓库根目录；fonttools 装在虚拟环境里，不要装到系统 Python）：
    python3 -m venv /tmp/icons-venv && /tmp/icons-venv/bin/pip install fonttools
    /tmp/icons-venv/bin/python tools/icons/build_icons.py --font <t.ttf>
不给 --font 时从 TDesign 官方地址下载 t.ttf（版本见 FONT_VERSION）。
--wxss 默认是 apps/miniapp/miniprogram_npm/tdesign-miniprogram/icon/icon.wxss
（微信开发者工具"构建 npm"后才有）。步骤见 docs/设计/素材来源与许可.md。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import re
import sys
import urllib.request
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[2]
MINIAPP = ROOT / "apps" / "miniapp"
ICON_LIST = Path(__file__).with_name("icons.txt")
OUTPUT = MINIAPP / "components" / "icon" / "index.wxss"
DEFAULT_WXSS = MINIAPP / "miniprogram_npm" / "tdesign-miniprogram" / "icon" / "icon.wxss"
FONT_VERSION = "0.4.5"
FONT_URL = f"https://tdesign.gtimg.com/icon/{FONT_VERSION}/fonts/t.ttf"
FAMILY = "youju-icon"
PREFIX = "yj-icon"

MIT = """MIT License

Copyright (c) 2021-present TDesign

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


def read_names(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        if not re.fullmatch(r"[a-z0-9-]+", name):
            sys.exit(f"图标名不合法：{name!r}")
        if name in names:
            sys.exit(f"图标重复：{name}")
        names.append(name)
    return names


def read_codepoints(path: Path) -> dict[str, int]:
    """TDesign icon.wxss：.t-icon-add:before{content:'\\E00D';} → {'add': 0xE00D}"""
    text = path.read_text(encoding="utf-8")
    found = re.findall(r"\.t-icon-([a-z0-9-]+):before\{content:'\\([0-9A-Fa-f]+)';?\}", text)
    if not found:
        sys.exit(f"没在 {path} 里找到图标编码")
    return {name: int(code, 16) for name, code in found}


def load_font(path: str | None) -> bytes:
    if path:
        return Path(path).read_bytes()
    print(f"下载 {FONT_URL}", file=sys.stderr)
    with urllib.request.urlopen(FONT_URL, timeout=60) as res:  # noqa: S310 - 固定的 TDesign 官方地址
        return res.read()


def build_woff(font_bytes: bytes, codepoints: list[int]) -> bytes:
    options = subset.Options()
    options.flavor = "woff"
    options.layout_features = []  # 图标不需要连字等排版特性
    options.hinting = False
    options.desubroutinize = True
    options.name_IDs = [0, 1, 2, 3, 4, 5, 6]
    options.notdef_outline = False
    options.glyph_names = False
    # 不改写 head 里的修改时间：同样的输入每次生成同样的文件
    font = TTFont(io.BytesIO(font_bytes), recalcTimestamp=False)
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=codepoints)
    subsetter.subset(font)
    out = io.BytesIO()
    subset.save_font(font, out, options)
    return out.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--font", help="TDesign 图标字体 t.ttf；不给就从官方地址下载")
    parser.add_argument(
        "--wxss", default=str(DEFAULT_WXSS), help="TDesign 小程序组件的 icon/icon.wxss"
    )
    parser.add_argument("--out", default=str(OUTPUT), help="输出的组件样式文件")
    args = parser.parse_args()

    names = read_names(ICON_LIST)
    mapping = read_codepoints(Path(args.wxss))
    missing = [n for n in names if n not in mapping]
    if missing:
        sys.exit(f"TDesign 没有这些图标：{', '.join(missing)}")

    font_bytes = load_font(args.font)
    source = TTFont(io.BytesIO(font_bytes))
    cmap = source.getBestCmap()
    for name in names:
        glyph = cmap.get(mapping[name])
        if glyph != name:
            sys.exit(f"编码对不上：{name} 的 U+{mapping[name]:04X} 在字体里是 {glyph!r}")

    woff = build_woff(font_bytes, [mapping[n] for n in names])
    check = TTFont(io.BytesIO(woff))
    kept = check.getBestCmap()
    lost = [n for n in names if mapping[n] not in kept]
    if lost:
        sys.exit(f"子集里少了：{', '.join(lost)}")

    digest = hashlib.sha256(font_bytes).hexdigest()
    data = base64.b64encode(woff).decode("ascii")
    rules = "\n".join(
        f".{PREFIX}-{n}:before {{ content: '\\{mapping[n]:x}'; }}" for n in sorted(names)
    )
    license_block = "\n".join(f" * {line}".rstrip() for line in MIT.splitlines())
    css = f"""/* 有据图标组件的样式和图标字体。由 tools/icons/build_icons.py 生成，不要手改；
 * 加图标：在 tools/icons/icons.txt 里加一行后重跑（步骤见 docs/设计/素材来源与许可.md）。
 *
 * 字体：TDesign 图标字体 tdesign-icons {FONT_VERSION} 的 t.ttf（sha256 {digest}），
 * 只保留下面 {len(names)} 个字形，压成 woff（{len(woff)} 字节），以 base64 内嵌，不从网络加载。
 * 来源、许可和生成方法见 docs/设计/素材来源与许可.md。
 *
 * TDesign 图标字体的许可：
 *
{license_block}
 */
@font-face {{
  font-family: '{FAMILY}';
  src: url('data:font/woff;base64,{data}') format('woff');
  font-weight: normal;
  font-style: normal;
}}

/* 结构和 TDesign t-icon 一致：块级、行高 1，图标宽高都等于 size；不写 color 时继承外层文字颜色 */
.{PREFIX} {{
  display: block;
  font-family: '{FAMILY}' !important;
  font-style: normal;
  font-weight: 400;
  font-variant: normal;
  text-transform: none;
  line-height: 1;
  text-align: center;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}

{rules}
"""
    Path(args.out).write_text(css, encoding="utf-8")
    print(f"已生成 {args.out}：{len(names)} 个图标，woff {len(woff)} 字节，base64 {len(data)} 字节")


if __name__ == "__main__":
    main()
