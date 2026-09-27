"""「有据」C 精修版设计规范：颜色、字号、图标和公共组件。所有页面由 pages.py 用这里的组件拼出来。"""

import json
import re
from pathlib import Path

# 颜色
BLUE = "#2A5CE0"          # 品牌蓝：搜索、选中、链接、主按钮
BLUE_G1 = "#2F66F0"       # 顶部渐变
BLUE_G2 = "#2553D6"
BLUE_TINT = "#EAF0FF"
BG = "#F2F4F8"            # 页面底
CARD = "#FFFFFF"
T1 = "#1D2129"            # 正文
T2 = "#4E5969"            # 次级
T3 = "#86909C"            # 弱信息（不用于来源和日期）
LINE = "#EEF0F3"          # 卡片内分隔线
LINE2 = "#E5E6EB"         # 控件描边
FILL = "#F2F3F5"          # 灰色填充（未选中筛选项、搜索框）
ORANGE = "#F57B21"        # 名单
O_TXT = "#D9640F"
O_TINT = "#FFF3E8"
GREEN = "#13A677"         # 政策
G_TXT = "#0E8A63"
G_TINT = "#E8F7F1"
RED = "#E5484D"           # 截止 7 天内
R_TINT = "#FFECEC"

FONT = "'Noto Sans SC', 'PingFang SC', 'Hiragino Sans GB', sans-serif"
FONTS_LINK = ('<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;600;700'
              '&amp;display=swap" rel="stylesheet">')

# 图标（24×24 线性，描边由调用处决定）
ICON = {
    "search": '<circle cx="11" cy="11" r="6.5"></circle><path d="M20 20l-4.2-4.2"></path>',
    "back": '<path d="M15 5l-7 7 7 7"></path>',
    "chev": '<path d="M9 6l6 6-6 6"></path>',
    "down": '<path d="M6 9l6 6 6-6"></path>',
    "close": '<path d="M6 6l12 12M18 6L6 18"></path>',
    "apply": '<path d="M6 3.5h9l3 3v14H6z"></path><path d="M9 11l2 2 4-4"></path><path d="M9 16.5h6"></path>',
    "list": ('<path d="M9.5 6.5h10M9.5 12h10M9.5 17.5h10"></path>'
             '<path d="M4.5 6.5l1 1 1.7-2M4.5 12l1 1 1.7-2M4.5 17.5l1 1 1.7-2"></path>'),
    "policy": ('<path d="M5 5.5a2 2 0 0 1 2-2h11v17H7a2 2 0 0 1-2-2z"></path>'
               '<path d="M5 18.5a2 2 0 0 1 2-2h11"></path><path d="M9 8h5"></path>'),
    "calendar": ('<rect x="4" y="5.5" width="16" height="14.5" rx="2"></rect>'
                 '<path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"></path><path d="M12 13v3l2 1"></path>'),
    "grid": ('<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"></rect><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"></rect>'
             '<rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"></rect><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"></rect>'),
    "bell": '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"></path><path d="M10 20.5a2.2 2.2 0 0 0 4 0"></path>',
    "doc": '<path d="M7 3.5h7l4 4v13H6v-16.5z"></path><path d="M14 3.5V8h4"></path><path d="M9 12.5h6M9 16h6"></path>',
    "user": '<circle cx="12" cy="8.5" r="3.5"></circle><path d="M5 20c1.2-3.6 4-5.5 7-5.5s5.8 1.9 7 5.5"></path>',
    "star": '<path d="M12 4l2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 16.4l-4.8 2.5.9-5.4-3.9-3.8 5.4-.8z"></path>',
    "download": '<path d="M12 4v11"></path><path d="M7.5 10.5L12 15l4.5-4.5"></path><path d="M5 19.5h14"></path>',
    "flag": '<path d="M6 20.5V4"></path><path d="M6 4.5h11l-2.2 3.8L17 12H6"></path>',
    "plus": '<path d="M12 5v14M5 12h14"></path>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"></path>',
    "filter": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"></path><circle cx="15" cy="7" r="2"></circle><circle cx="9" cy="17" r="2"></circle>',
    "ext": '<path d="M14 4h6v6"></path><path d="M20 4l-9 9"></path><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"></path>',
    "clip": '<path d="M20 11.5l-7.8 7.8a4.5 4.5 0 0 1-6.4-6.4l8.1-8.1a3 3 0 0 1 4.2 4.2l-8 8a1.5 1.5 0 0 1-2.1-2.1l7.3-7.3"></path>',
    "shield": '<path d="M12 3.5l7 3v5c0 4.5-3 7.8-7 9-4-1.2-7-4.5-7-9v-5z"></path><path d="M9 12l2 2 4-4"></path>',
    "info": '<circle cx="12" cy="12" r="8"></circle><path d="M12 11v5"></path><path d="M12 8h.01"></path>',
    "gear": '<circle cx="12" cy="12" r="3"></circle><path d="M12 3.5v2.5M12 18v2.5M3.5 12H6M18 12h2.5M6 6l1.8 1.8M16.2 16.2L18 18M6 18l1.8-1.8M16.2 7.8L18 6"></path>',
    "copy": '<rect x="8.5" y="8.5" width="11" height="11" rx="2"></rect><path d="M5.5 15.5V6a1 1 0 0 1 1-1h9.5"></path>',
    "home_fill": '<path d="M4 10.5L12 4l8 6.5V20a1 1 0 0 1-1 1h-4.5v-5.5h-5V21H5a1 1 0 0 1-1-1z"></path>',
}


# 图标用 TDesign（腾讯，MIT 许可，文件和许可在 icons/tdesign/），决策记录 D-015。
# 我们的名字 → TDesign 图标名；上面手画的 ICON 只在对照表里没有时使用。
LIB_ICONS = {"search": "search", "back": "chevron-left", "chev": "chevron-right", "down": "chevron-down", "close": "close",
             "apply": "task-checked", "list": "bulletpoint", "policy": "book", "calendar": "calendar", "grid": "app",
             "bell": "notification", "doc": "file", "user": "user", "home": "home", "star": "star", "download": "download",
             "flag": "flag", "plus": "add", "check": "check", "filter": "filter", "ext": "jump", "clip": "attach",
             "shield": "secured", "info": "info-circle", "gear": "setting-1", "copy": "copy"}
# 底栏选中时用面性图标
LIB_FILLED = {"home": "home-filled", "bell": "notification-filled", "doc": "file-filled", "user": "user-filled"}
ICON_DIR = Path(__file__).resolve().parent / "icons" / "tdesign"


def icon(name, size=22, color=T1, width=1.8, fill="none", filled=False):
    """TDesign 图标沿用原始线宽（2px / 24px），width 只对手画图标有效。"""
    if name in LIB_ICONS:
        file = LIB_FILLED[name] if filled else LIB_ICONS[name]
        svg = (ICON_DIR / f"{file}.svg").read_text(encoding="utf-8")
        inner = re.sub(r"^.*?<svg[^>]*>|</svg>\s*$", "", svg, flags=re.S)
        return (f'<svg aria-hidden="true" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
                f'style="flex-shrink: 0; color: {color}">{inner}</svg>')
    return (f'<svg aria-hidden="true" width="{size}" height="{size}" viewBox="0 0 24 24" style="flex-shrink: 0" fill="{fill}" stroke="{color}" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">{ICON[name]}</svg>')


def capsule(on_blue=False):
    """微信小程序右上角的系统胶囊（占位，表示这块区域不能放内容）。"""
    if on_blue:
        border, bg, fg, div = "rgba(255,255,255,0.35)", "rgba(255,255,255,0.12)", "#FFFFFF", "rgba(255,255,255,0.35)"
    else:
        border, bg, fg, div = LINE2, CARD, T1, LINE2
    return (f'<div aria-hidden="true" style="position: absolute; right: 10px; top: 8px; width: 87px; height: 32px; box-sizing: border-box; '
            f'border: 1px solid {border}; border-radius: 16px; background: {bg}; display: flex; align-items: center">'
            f'<div style="flex-grow: 1; display: flex; justify-content: center"><svg width="20" height="20" viewBox="0 0 24 24" fill="{fg}">'
            f'<circle cx="5" cy="12" r="1.7"></circle><circle cx="12" cy="12" r="2.3"></circle><circle cx="19" cy="12" r="1.7"></circle></svg></div>'
            f'<div style="width: 1px; height: 18px; background: {div}"></div>'
            f'<div style="flex-grow: 1; display: flex; justify-content: center"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="{fg}" stroke-width="1.8">'
            f'<circle cx="12" cy="12" r="7.5"></circle><circle cx="12" cy="12" r="2.6" fill="{fg}" stroke="none"></circle></svg></div></div>')


def navbar(title, back="Main.dc.html", border=True):
    """子页面的白色导航栏：返回、居中标题、胶囊。back=None 表示底栏页面（没有返回）。"""
    back_html = ""
    if back:
        back_html = (f'<a href="{back}" aria-label="返回" style="position: absolute; left: 6px; top: 4px; width: 40px; height: 40px; '
                     f'display: flex; align-items: center; justify-content: center">{icon("back", 22, T1, 2)}</a>')
    b = f"border-bottom: 1px solid {LINE};" if border else ""
    return (f'<header style="height: 48px; display: flex; align-items: center; justify-content: center; position: relative; background: {CARD}; {b}">'
            f'{back_html}<span style="font-size: 17px; font-weight: 600">{title}</span>{capsule()}</header>')


def tabbar(active):
    items = [("首页", "Main.dc.html", "home"), ("关注", "Follow.dc.html", "bell"),
             ("文件", "Files.dc.html", "doc"), ("我的", "Me.dc.html", "user")]
    out = []
    for label, href, key in items:
        on = label == active
        color = BLUE if on else T3
        svg = icon(key, 24, color, filled=on)
        cur = ' aria-current="page"' if on else ""
        weight = "font-weight: 600;" if on else ""
        out.append(f'<a href="{href}"{cur} style="display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; '
                   f'color: {color}; font-size: 11px; {weight}">{svg}<span>{label}</span></a>')
    return (f'<nav aria-label="主导航" style="position: absolute; left: 0; right: 0; bottom: 0; height: 84px; box-sizing: border-box; '
            f'padding-bottom: 30px; background: {CARD}; border-top: 1px solid {LINE}; display: grid; '
            f'grid-template-columns: repeat(4, minmax(0, 1fr))">{"".join(out)}</nav>')


def tag_type(kind):
    c = {"申报通知": (BLUE_TINT, BLUE), "名单": (O_TINT, O_TXT), "政策": (G_TINT, G_TXT),
         "资金支持": (FILL, T2)}[kind]
    return (f'<span style="padding: 0 6px; line-height: 20px; border-radius: 4px; background: {c[0]}; color: {c[1]}; '
            f'font-size: 12px; font-weight: 500; white-space: nowrap">{kind}</span>')


def tag_status(s):
    if s == "公示":
        return (f'<span style="padding: 0 6px; line-height: 18px; border-radius: 4px; border: 1px dashed {O_TXT}; color: {O_TXT}; '
                f'font-size: 12px; white-space: nowrap">公示</span>')
    return (f'<span style="padding: 0 6px; line-height: 18px; border-radius: 4px; border: 1px solid {T2}; color: {T1}; '
            f'font-size: 12px; white-space: nowrap">正式公布</span>')


def deadline(text, days):
    """截止提示：14 天内标红，7 天内再加红色底。"""
    if days <= 7:
        return (f'<span style="margin-left: auto; padding: 0 6px; line-height: 20px; border-radius: 4px; background: {R_TINT}; '
                f'font-size: 12px; font-weight: 600; color: {RED}; white-space: nowrap">{text} · 剩{days}天</span>')
    color = RED if days <= 14 else T2
    return f'<span style="margin-left: auto; font-size: 12px; color: {color}; white-space: nowrap">{text} · 剩{days}天</span>'


def doc_row(d, last=False, href=None, title_html=None):
    """列表行：类型/状态/地区/截止 → 标题 → 发文机关和日期 + 复制原文链接。"""
    head = [tag_type(d["kind"])]
    if d.get("status"):
        head.append(tag_status(d["status"]))
    head.append(f'<span style="font-size: 12px; color: {T2}; white-space: nowrap">{d["region"]}</span>')
    if d.get("deadline"):
        head.append(deadline(d["deadline"], d["days"]))
    border = "" if last else f"border-bottom: 1px solid {LINE};"
    title = title_html or d["title"]
    tag_open, tag_close = (f'<a href="{href}" style="display: block; color: {T1}">', "</a>") if href else ("<div>", "</div>")
    return (f'<div style="padding: 14px 0 13px; {border}">{tag_open}'
            f'<div style="display: flex; align-items: center; gap: 6px">{"".join(head)}</div>'
            f'<div style="margin-top: 7px; font-size: 16px; font-weight: 600; line-height: 24px">{title}</div>{tag_close}'
            + meta_line(d))


# 复制原文链接：每个按钮一个编号，生成页面时据此写交互逻辑（点了真的复制，并在按钮上方提示去浏览器打开）
COPY_LINKS = {}


def copy_id(url):
    cid = f"c{len(COPY_LINKS) + 1}"
    COPY_LINKS[cid] = url
    return cid


def copy_bubble(cid, pos="right: 0; bottom: calc(100% + 6px)"):
    return (f'<sc-if value="{{{{copied_{cid}}}}}" hint-placeholder-val="{{{{ false }}}}"><span role="status" style="position: absolute; {pos}; z-index: 5; '
            f'padding: 6px 10px; border-radius: 6px; background: rgba(29, 33, 41, 0.92); color: #FFFFFF; font-size: 12px; font-weight: 400; line-height: 1.4; '
            f'white-space: nowrap; pointer-events: none">已复制，请粘贴到浏览器打开</span></sc-if>')


def meta_line(d):
    """发文机关不截断：名称长时单独一行，日期和“复制原文链接”放下一行。"""
    cid = copy_id(d.get("url", ""))
    link = (f'<button type="button" onClick="{{{{copy_{cid}}}}}" style="margin-left: auto; flex-shrink: 0; display: inline-flex; align-items: center; gap: 3px; '
            f'padding: 0; background: none; border: 0; font-family: inherit; font-size: 12.5px; color: {BLUE}; white-space: nowrap; cursor: pointer">{icon("copy", 14, BLUE, 1.9)}复制原文链接</button>'
            + copy_bubble(cid))
    if len(d["issuer"]) > 11:
        return (f'<div style="margin-top: 7px; font-size: 12.5px; line-height: 1.6; color: {T2}">{d["issuer"]}</div>'
                f'<div style="margin-top: 2px; position: relative; display: flex; align-items: center; font-size: 12.5px; color: {T2}"><span>{d["date"]}</span>{link}</div></div>')
    return (f'<div style="margin-top: 7px; position: relative; display: flex; align-items: center; gap: 12px; font-size: 12.5px; color: {T2}">'
            f'<span>{d["issuer"]} · {d["date"]}</span>{link}</div></div>')


def with_copy(js_body, body, defaults):
    """页面里有“复制原文链接”按钮时，把复制逻辑并进页面原有的交互逻辑。"""
    cids = sorted(set(re.findall(r'\{\{copy_(c\d+)\}\}', body)), key=lambda c: int(c[1:]))
    if not cids:
        return js_body, defaults
    defaults = {**defaults, **{f"copied_{c}": False for c in cids}}
    links = {c: COPY_LINKS[c] for c in cids}
    js_out = ("const __page = (() => {\n    " + js_body + "\n    })();\n"
              f"    const __links = {js(links)};\n"
              "    const __copied = (this.state || {}).copied;\n"
              "    const __out = { ...__page };\n"
              "    Object.keys(__links).forEach((id) => {\n"
              "      __out['copied_' + id] = __copied === id;\n"
              "      __out['copy_' + id] = () => {\n"
              "        try { navigator.clipboard.writeText(__links[id]).catch(() => {}); } catch (e) { /* 画布里可能没有剪贴板权限 */ }\n"
              "        this.setState({ copied: id });\n"
              "        setTimeout(() => { if ((this.state || {}).copied === id) this.setState({ copied: null }); }, 2200);\n"
              "      };\n"
              "    });\n"
              "    return __out;")
    return js_out, defaults


def notice_bar(on_blue=False):
    """全站统一的出处说明：放在导航栏下方（蓝色顶部的页面放在顶部区域里）。"""
    color = "rgba(255,255,255,0.82)" if on_blue else T3
    bg = "transparent" if on_blue else BG
    return (f'<div style="height: 24px; display: flex; align-items: center; justify-content: center; gap: 4px; background: {bg}; '
            f'font-size: 11.5px; color: {color}">{icon("shield", 12, color, 2)}第三方整理 · 非政府平台 · 每条附原文链接</div>')


def load_more(shown, total, href="Search.dc.html"):
    return (f'<div style="height: 48px; display: flex; align-items: center; justify-content: center; gap: 6px; border-top: 1px solid {LINE}; '
            f'font-size: 13px; color: {T3}">已显示 {shown}/{total} 份 · <a href="{href}" style="color: {BLUE}">查看全部 {total} 份</a></div>')


def card(inner, pad="0 16px", mt=10, extra=""):
    return (f'<div style="margin: {mt}px 12px 0; padding: {pad}; background: {CARD}; border-radius: 12px; {extra}">{inner}</div>')


def section_title(text, right_html=""):
    return (f'<div style="display: flex; align-items: center; justify-content: space-between; padding: 14px 0 4px">'
            f'<span style="font-size: 16px; font-weight: 600">{text}</span>{right_html}</div>')


def pill_button(text, href=None, primary=True, extra=""):
    st = (f"background: {BLUE}; color: #FFFFFF; border: 0;" if primary
          else f"background: {CARD}; color: {BLUE}; border: 1px solid {BLUE};")
    base = (f'height: 44px; display: flex; align-items: center; justify-content: center; gap: 6px; border-radius: 22px; '
            f'font-size: 15px; font-weight: 500; {st} {extra}')
    if href:
        return f'<a href="{href}" style="{base}">{text}</a>'
    return f'<button type="button" style="{base}; font-family: inherit; cursor: pointer; width: 100%">{text}</button>'


def root(height, bg=BG):
    return (f'<div style="width: 390px; height: {height}px; position: relative; overflow: hidden; background: {bg}; color: {T1}; '
            f'font-family: {FONT}; font-size: 14px; line-height: 1.5; font-variant-numeric: tabular-nums">')


def dc_page(title, height, body, js_body="return {};", bg=BG):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
{FONTS_LINK}
<style>
body{{margin:0;background:{bg}}}
a{{color:{BLUE};text-decoration:none}}a:hover{{color:#1F47B8}}
</style>
</helmet>
{root(height, bg)}
{body}
</div>
</x-dc>
<script type="text/x-dc" data-dc-script data-props='{{"$preview":{{"width":390,"height":{height}}}}}'>
class Component extends DCLogic {{
  renderVals() {{
    {js_body}
  }}
}}
</script>
</body>
</html>
"""


def static_preview(height, body, defaults, bg=BG):
    """把 DC 模板按默认状态展开成普通 HTML，用来渲染效果图给团队评审。"""
    s = body

    def keep_if(m):
        flag = m.group(1).strip()
        return m.group(2) if defaults.get(flag, True) else ""

    # 由内向外展开，支持嵌套的 sc-if（如列表页签里的复制提示）
    pat = re.compile(r'<sc-if value="\{\{\s*([\w.]+)\s*\}\}"[^>]*>((?:(?!<sc-if\b).)*?)</sc-if>', re.S)
    while True:
        s2 = pat.sub(keep_if, s)
        if s2 == s:
            break
        s = s2
    s = re.sub(r'\s(onClick|onChange)="\{\{[^}]+\}\}"', "", s)
    s = re.sub(r'\{\{\s*([\w.]+)\s*\}\}', lambda m: str(defaults.get(m.group(1), "")), s)
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{FONTS_LINK}'
            f'<style>body{{margin:0;background:{bg}}} a{{color:{BLUE};text-decoration:none}}</style></head>'
            f'<body>{root(height, bg)}{s}</div></body></html>')


def js(v):
    return json.dumps(v, ensure_ascii=False)
