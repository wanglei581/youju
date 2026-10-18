"""出一张"手机上的样子"示意图：订阅消息列表里的大图（2.35:1）和转发卡片里的小图（1:1），按手机实际比例排。

python preview.py --font400 <Noto Sans SC 400> --font500 <Noto Sans SC 500> --logo <有据-logo-v1.png> --out <png>
界面只是示意（灰色块、圆角卡片），不是微信的真实界面；标题里的"××省"等是占位，不是真实数据。
"""

import argparse
import base64
import html
import io
import tempfile
from pathlib import Path

from PIL import Image

from render import render

COVERS = Path(__file__).resolve().parents[1] / "封面"
ACCOUNT = "有据申报与行业助手"
# 标题按方案第四节"栏目名｜日期范围或地区"的写法，日期、地区是示意
ITEMS = [
    ("申报周报", "申报周报｜10月13日—19日（全国）"),
    ("临期提醒", "临期提醒｜10月23日—29日截止的申报项目（已核对）"),
    ("名单速报", "名单速报｜××省 2026 年第一批高新技术企业拟认定名单"),
    ("使用说明", "使用说明｜收录范围、截止日期怎么看、怎么设提醒"),
]
DIGEST = "第三方整理，非政府平台，以原文为准。"


def data_uri(path, mime):
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"


def small_logo(path):
    img = Image.open(path).convert("RGB").resize((96, 96), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def page(args):
    logo = small_logo(args.logo)
    feed, chat = [], []
    for name, title in ITEMS:
        wide = data_uri(COVERS / f"{name}_2.35比1.png", "image/png")
        square = data_uri(COVERS / f"{name}_1比1.png", "image/png")
        t = html.escape(title)
        feed.append(
            f'<div class="msg"><div class="who"><img class="av" src="{logo}"><b>{ACCOUNT}</b><span>上午 8:00</span></div>'
            f'<img class="wide" src="{wide}"><div class="t">{t}</div></div>'
        )
        chat.append(
            f'<div class="row"><div class="card"><div class="ct">{t}</div><div class="cb"><span>{DIGEST}</span>'
            f'<img class="sq" src="{square}"></div><div class="cf"><img class="av2" src="{logo}">{ACCOUNT}</div></div></div>'
        )
    css = f"""
@font-face {{ font-family: N; font-weight: 400; src: url("{data_uri(args.font400, 'font/woff2')}"); }}
@font-face {{ font-family: N; font-weight: 500; src: url("{data_uri(args.font500, 'font/woff2')}"); }}
* {{ box-sizing: border-box; margin: 0; }}
body {{ width: 750px; font-family: N, sans-serif; background: #EDEDED; color: #111; }}
.cap {{ padding: 28px 32px 8px; font-size: 24px; color: #777; }}
.cols {{ display: flex; gap: 24px; padding: 8px 24px 32px; }}
.col {{ flex: 1; }}
.msg {{ background: #FFF; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
.who {{ display: flex; align-items: center; gap: 12px; font-size: 22px; margin-bottom: 16px; }}
.who span {{ margin-left: auto; color: #999; font-size: 20px; }}
.av {{ width: 44px; height: 44px; border-radius: 50%; }}
.wide {{ display: block; width: 100%; border-radius: 8px; }}
.t {{ margin-top: 14px; font-size: 26px; font-weight: 500; line-height: 1.45; }}
.row {{ display: flex; justify-content: flex-end; margin-bottom: 24px; }}
.card {{ width: 100%; background: #FFF; border-radius: 10px; padding: 20px 22px 14px; }}
.ct {{ font-size: 26px; line-height: 1.4; font-weight: 500; }}
.cb {{ display: flex; gap: 16px; margin-top: 10px; font-size: 22px; color: #888; line-height: 1.45; }}
.cb span {{ flex: 1; }}
.sq {{ width: 96px; height: 96px; flex-shrink: 0; }}
.cf {{ display: flex; align-items: center; gap: 8px; margin-top: 14px; padding-top: 12px; border-top: 1px solid #EEE; font-size: 20px; color: #999; }}
.av2 {{ width: 28px; height: 28px; border-radius: 50%; }}
"""
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>'
        '<div class="cap">示意，按手机实际比例（375pt 宽、2 倍图）。上：订阅消息列表；下：转发给好友的卡片（右侧是 1:1）</div>'
        f'<div class="cols"><div class="col">{"".join(feed)}</div></div>'
        f'<div class="cols"><div class="col">{"".join(chat)}</div></div></body></html>'
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--font400", required=True)
    ap.add_argument("--font500", required=True)
    ap.add_argument("--logo", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="cover-preview-"))
    src = tmp / "preview.html"
    src.write_text(page(args), encoding="utf-8")
    print(render(src, size=(750, 3600), out=Path(args.out), trim_to=(237, 237, 237)))


if __name__ == "__main__":
    main()
