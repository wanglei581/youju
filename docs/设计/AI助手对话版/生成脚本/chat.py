"""「有据」AI 助手对话版设计稿（2026-10-17）：10 个手机画面（9 屏，第 1 屏分第一次打开和再次打开），和给创始人看的评审稿。

沿用原型 v2 的规范（../../原型v2/生成脚本/ds.py：颜色、字号、标签、胶囊）；
图标只用小程序本地图标集里有的（tools/icons/icons.txt），用了表外的图标直接报错。
通知标题、文号、日期、原句、网页摘录都取自政府网站原文（2026-10-17 用本机网络核对，链接见 SOURCES）；
企业情况、对话时间是示例，模型名称、备案号、联网次数等用 [ ] 占位，等定了再填。

用法：python3 chat.py <评审稿 HTML> <单屏 HTML 目录>
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "原型v2" / "生成脚本"))

import ds  # noqa: E402
from ds import (BG, BLUE, BLUE_TINT, CARD, FILL, FONT, FONTS_LINK, G_TINT, GREEN, LINE, LINE2, O_TINT, O_TXT,  # noqa: E402
                ORANGE, RED, T1, T2, T3, capsule, tag_type)

# ---------- 图标：只许用小程序本地图标集里有的 ----------
ICONS_TXT = HERE.parents[3] / "tools" / "icons" / "icons.txt"
ALLOWED = {line.split("#")[0].strip() for line in ICONS_TXT.read_text(encoding="utf-8").splitlines()} - {""}


def icon(name, size=18, color=T1, filled=False):
    file = ds.LIB_FILLED[name] if filled else ds.LIB_ICONS[name]
    if file not in ALLOWED:
        raise KeyError(f"图标 {file} 不在小程序本地图标集（tools/icons/icons.txt）里")
    return ds.icon(name, size, color, filled=filled)


QUOTE_BG = "#F7F8FA"   # 原文原句的底色：全稿统一，看到灰底就是原文
MINE_BG = "#F0F5FF"    # 你填的企业情况

# ---------- 真实数据（政府网站原文，2026-10-17 核对） ----------
SC5G = dict(region="四川 · 省级", title="关于组织开展2026年5G工厂名录遴选推荐工作的通知", issuer="四川省经济和信息化厅",
            date="2026-10-16", no="川经信办函〔2026〕356号", atts=1)
GD8 = dict(region="广东 · 省级", title="广东省工业和信息化厅关于开展第八批产业技术基础公共服务平台申报工作的通知",
           issuer="广东省工业和信息化厅", date="2026-10-16", no="粤工信创新函[2026]54号", atts=3)
HN = dict(region="海南 · 省级", title="关于组织开展2026年第二批专精特新中小企业申报和复核工作的通知", issuer="海南省工业和信息化厅",
          date="2026-09-21", no="琼工信企业〔2026〕190号", atts=4)

S_OBJ = "项目申报主体为制造业、采矿业、电力、港口等重点领域企业，在四川省内注册，具备独立法人资格。"
S_GUIDE = "项目建设内容参照《5G全连接工厂建设指南》（工信厅信管〔2022〕23号）。"
S_PLATFORM = ("请各市（州）经济和信息化局组织本地区企业，通过“5G+工业互联网发展管理平台”（https://5gii.aii-alliance.org/）"
              "于10月30日前填报项目并上传相关材料。")
S_REVIEW = ("请各市（州）经济和信息化局严格审核企业填报的材料（推荐项目需为已完成项目），确保项目材料的真实性、合规性和完整性，"
            "于11月4日前将盖章版推荐项目汇总表发送到指定邮箱。")
S_ATT = "附件：5G工厂入库项目推荐汇总表"

# 右栏：原句提到的每一项，列出你填的或“没有填”；只摆事实，不写符不符合
HN_ROWS = [
    ("在海南省生产经营、具有独立法人资格。", "一、企业申报要求（一）",
     [("注册地", "海南省 海口市", "营业执照", False), ("独立法人资格", "没有对应的填写项", "", True)]),
    ("已获得科技型或创新型中小企业称号（有效期内），可提出省级专精特新中小企业申请。", "一、企业申报要求（一）",
     [("科技型中小企业", "你没有选", "", True), ("创新型中小企业", "你选了，有效期至 [日期]", "", False)]),
    ("2023年已认定（2026年内达到三年有效期限）的省级专精特新中小企业，可提出省级专精特新中小企业复核申请。", "一、企业申报要求（一）",
     [("省级专精特新中小企业", "你没有选", "", True)]),
    ("企业未被列入经营异常名录或严重失信主体名单，提供的产品（服务）不属于国家禁止、限制或淘汰类，同时近三年未发生重大安全"
     "（含网络安全、数据安全）、质量、环境污染等事故以及偷漏税等违法违规行为。", "一、企业申报要求（二）", []),
    ("申请和复核企业均须满足《办法》规定的专精特新中小企业认定标准。", "一、企业申报要求（三）", []),
]

WEB = [
    dict(kind="政策文件", site="四川省发展和改革委员会", domain="fgw.sc.gov.cn", date="2026-01-13",
         title="关于印发《关于支持数字经济高质量发展的若干政策措施》的通知（川发改数据〔2026〕4号）",
         quote="……推动人工智能、大数据、云计算、5G、物联网等信息技术在制造全过程、全要素深度融合运用……"
               "单个项目安排不超过设备（含软件）投资额20%的资金支持。"),
    dict(kind="新闻转载", site="四川省政府国有资产监督管理委员会", domain="gzw.sc.gov.cn", date="2025-09-19",
         title="川观新闻丨四川29个工厂入选国家5G工厂名录",
         quote="强化政策支持，制定“5G+工业互联网”政策措施，联动市（州）共同支持5G工厂建设应用。"),
]

SOURCES = [
    ("四川省经济和信息化厅 · 关于组织开展2026年5G工厂名录遴选推荐工作的通知（2026-10-16）",
     "https://jxt.sc.gov.cn/scjxt/wjfb/2026/10/16/cc3f737fb244460dbd63fc2c36d85393.shtml"),
    ("广东省工业和信息化厅 · 关于开展第八批产业技术基础公共服务平台申报工作的通知（2026-10-16）",
     "https://gdii.gd.gov.cn/zwgk/tzgg1011/content/post_4967575.html"),
    ("海南省工业和信息化厅 · 关于组织开展2026年第二批专精特新中小企业申报和复核工作的通知（2026-09-21）",
     "https://iitb.hainan.gov.cn/iitb/tzgg/202609/5e951788f1024a86a684fb8aa528ab77.shtml"),
    ("四川省发展和改革委员会 · 川发改数据〔2026〕4号（2026-01-13）",
     "https://fgw.sc.gov.cn/sfgw/qtwj/2026/1/13/737c5b423df6427b88a54dd9dddf0424.shtml"),
    ("四川省国资委网站 · 川观新闻丨四川29个工厂入选国家5G工厂名录（2025-09-19）",
     "http://gzw.sc.gov.cn/scsgzw/CU230206/2025/9/19/89b404b0ca0a418482b3ecb5eac2dee4.shtml"),
]

# 说明面板：《生成式人工智能服务管理暂行办法》第十条要公开的适用人群、场合、用途，加上次数、图片、保存期限和模型公示
SCOPE_ROWS = [
    ("适用人群", "企业里负责项目申报的经办人员。"),
    ("适用场合", "阅读政府部门发布的申报通知、准备申报材料时。"),
    ("用途", "从通知原文里摘出申报对象、条件、时间、材料等原句；回答通知里的问题并注明出处；把原文条件和你填的企业情况并排列出，供你自己对照。"),
    ("不适用", "判断企业能不能申报；代写申报材料或代办申报；代替阅读通知原文。不面向未成年人。"),
    ("次数", "每人每天 5 次：提问、划重点、对照、识别图片各算 1 次；联网查政府网站另算，每天 [次数] 次。处理失败、没有回答的不扣次数。"),
    ("图片", "先做内容安全检测，只用来识别文字；识别完不保存图片。"),
    ("保存", "你填的企业情况保存 7 天，对话和整理结果保存 30 天，可以随时删除。"),
    ("模型", "[模型名称] · 备案号 [备案号]"),
]


# ---------- 组件 ----------
def ai_tag(solid=False):
    st = f"background: {BLUE}; color: #FFFFFF" if solid else f"background: {BLUE_TINT}; color: {BLUE}"
    return (f'<span style="flex-shrink: 0; padding: 0 6px; line-height: 20px; border-radius: 4px; {st}; font-size: 12px; '
            f'font-weight: 600; white-space: nowrap">AI 生成</span>')


def nav(title="AI 助手"):
    return (f'<header style="height: 48px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; position: relative; background: {CARD}">'
            f'<span style="position: absolute; left: 6px; top: 4px; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center">{icon("back", 22, T1)}</span>'
            f'<span style="font-size: 17px; font-weight: 600">{title}</span>{capsule()}</header>')


def strip(expanded=False):
    """导航栏下方固定的一条：显式“AI 生成”标识，不能关闭；右边“说明”展开第十条要公开的内容。"""
    arrow = f'<span style="display: inline-flex; {"transform: rotate(180deg)" if expanded else ""}">{icon("down", 14, BLUE)}</span>'
    head = (f'<div style="height: 36px; display: flex; align-items: center; gap: 8px; padding: 0 12px 0 16px">'
            f'{ai_tag(True)}<span style="flex-grow: 1; min-width: 0; font-size: 12px; color: {T2}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">第三方整理 · 非政府平台 · 以原文为准</span>'
            f'<span style="flex-shrink: 0; display: inline-flex; align-items: center; gap: 2px; font-size: 12.5px; color: {BLUE}">{"收起" if expanded else "说明"}{arrow}</span></div>')
    panel = ""
    if expanded:
        rows = "".join(f'<div style="display: flex; gap: 10px; padding: 4px 0"><span style="width: 52px; flex-shrink: 0; color: {T1}; font-weight: 500">{k}</span>'
                       f'<span style="min-width: 0">{v}</span></div>' for k, v in SCOPE_ROWS)
        panel = (f'<div style="margin: 0 12px 12px; padding: 8px 12px 10px; background: {QUOTE_BG}; border-radius: 10px; font-size: 12.5px; line-height: 1.65; color: {T2}">{rows}'
                 f'<div style="margin-top: 6px; padding-top: 9px; border-top: 1px solid {LINE2}; display: flex; gap: 18px; color: {BLUE}">'
                 f'<span>《AI 助手使用说明》</span><span>投诉举报（15 个工作日内答复）</span></div></div>')
    return f'<div style="flex-shrink: 0; background: {CARD}; border-bottom: 1px solid {LINE}">{head}{panel}</div>'


def ctx_bar(d):
    """带着通知进来以后，对话顶部收起成一行；点开看通知、换一份。"""
    return (f'<div style="margin: 10px 12px 0; height: 42px; padding: 0 10px 0 12px; box-sizing: border-box; display: flex; align-items: center; gap: 8px; background: {CARD}; border-radius: 10px">'
            f'{icon("doc", 16, BLUE)}<span style="flex-grow: 1; min-width: 0; font-size: 13.5px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">{d["title"]}</span>'
            f'{icon("down", 15, T3)}</div>')


def sys_note(text):
    return f'<div style="padding: 14px 24px 0; text-align: center; font-size: 12px; line-height: 1.6; color: {T3}">{text}</div>'


def user_msg(text):
    return (f'<div style="display: flex; justify-content: flex-end; padding: 16px 12px 0 64px">'
            f'<div style="padding: 9px 14px; background: {BLUE}; color: #FFFFFF; border-radius: 18px 18px 4px 18px; font-size: 15px; line-height: 1.6">{text}</div></div>')


def user_img(inner, note=""):
    n = f'<div style="margin-top: 5px; text-align: right; font-size: 11.5px; color: {T3}">{note}</div>' if note else ""
    return (f'<div style="display: flex; justify-content: flex-end; padding: 16px 12px 0"><div>'
            f'<div style="border-radius: 14px 14px 4px 14px; overflow: hidden">{inner}</div>{n}</div></div>')


FOOT = {
    "quote": "原句逐字取自原文，归类由 AI 判断、可能有误；不判断能否申报，以原文和主管部门解释为准",
    "answer": "回答依据所附原文，不判断能否申报，以通知原文和主管部门解释为准",
    "clause": "只并排列出，不判断能否申报，以通知原文和主管部门解释为准",
    "web": "网页内容未经人工核对，不判断能否申报，以原文和主管部门解释为准",
    "image": "识别的文字只用来找原文，回答以政府网站上的原文为准",
}


def footer(kind):
    if kind is None:
        return '<div style="height: 14px"></div>'
    return (f'<div style="margin-top: 12px; padding: 10px 14px 12px; border-top: 1px solid {LINE}; font-size: 11.5px; line-height: 1.6; color: {T3}">'
            f'<div>AI 生成 · {FOOT[kind]}</div>'
            f'<div style="margin-top: 3px; display: flex; align-items: center; gap: 12px"><span>模型 [模型名称] · 备案号 [备案号]</span>'
            f'<span style="margin-left: auto; display: inline-flex; align-items: center; gap: 3px; color: {T2}">{icon("copy", 13, T2)}复制</span>'
            f'<span style="color: {T2}">投诉举报</span></div></div>')


def ai_card(title, body, right="", foot=None):
    head = (f'<div style="display: flex; align-items: center; gap: 8px; padding: 12px 14px 0">{ai_tag()}'
            f'<span style="font-size: 15px; font-weight: 600">{title}</span>'
            + (f'<span style="margin-left: auto; font-size: 12px; color: {T3}; white-space: nowrap">{right}</span>' if right else "")
            + '</div>')
    return (f'<div style="margin: 14px 12px 0; background: {CARD}; border-radius: 12px">{head}'
            f'<div style="padding: 10px 14px 0">{body}</div>{footer(foot)}</div>')


def para(text, size=15, color=T1, mt=0):
    return f'<p style="margin: {mt}px 0 0; font-size: {size}px; line-height: 1.7; color: {color}">{text}</p>'


def caution(text, link=None, mt=0):
    """提醒（附件、未经人工核对、个人信息）：浅橙底，放在卡片最前面。"""
    lk = f'<div style="margin-top: 4px; color: {O_TXT}; font-weight: 500">{link} ›</div>' if link else ""
    return (f'<div style="margin-top: {mt}px; display: flex; gap: 8px; padding: 10px 12px; background: {O_TINT}; border-radius: 8px; font-size: 13px; line-height: 1.6; color: {T1}">'
            f'<span style="flex-shrink: 0; padding-top: 2px">{icon("info", 15, O_TXT)}</span><div style="min-width: 0">{text}{lk}</div></div>')


def quote_block(text, size=14.5, mt=8):
    return (f'<div style="margin-top: {mt}px; padding: 10px 12px; background: {QUOTE_BG}; border-radius: 8px; font-size: {size}px; line-height: 1.75; '
            f'color: {T1}; overflow-wrap: anywhere">{text}</div>')


def tag_cat(t):
    return (f'<span style="padding: 0 6px; line-height: 20px; border-radius: 4px; border: 1px solid {LINE2}; color: {T2}; font-size: 12px; '
            f'white-space: nowrap">{t}</span>')


def link_row(left="看原文", right_flag=True, right_text="这句摘错了", copy=False):
    r = (f'<span style="margin-left: auto; display: inline-flex; align-items: center; gap: 3px; color: {T2}">{icon("flag", 13, T2)}{right_text}</span>'
         if right_flag else "")
    c = (f'<span style="margin-left: 16px; display: inline-flex; align-items: center; gap: 3px; color: {BLUE}">{icon("copy", 13, BLUE)}复制原文链接</span>'
         if copy else "")
    return f'<div style="margin-top: 8px; display: flex; align-items: center; font-size: 12.5px"><span style="color: {BLUE}">{left} ›</span>{c}{r}</div>'


def quote_item(cats, text, src, last=False):
    border = "" if last else f"border-bottom: 1px solid {LINE};"
    return (f'<div style="padding: 12px 0; {border}">'
            f'<div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap">{"".join(tag_cat(c) for c in cats)}'
            f'<span style="margin-left: auto; font-size: 12px; color: {T3}; white-space: nowrap">{src}</span></div>'
            f'{quote_block(text)}{link_row()}</div>')


def cat_chip(label, n, on=False):
    st = (f"background: {BLUE_TINT}; color: {BLUE}; border: 1px solid {BLUE}; font-weight: 500" if on
          else f"background: {CARD}; color: {T1}; border: 1px solid {LINE2}")
    return (f'<span style="height: 28px; padding: 0 10px; display: inline-flex; align-items: center; gap: 3px; border-radius: 14px; '
            f'font-size: 12.5px; white-space: nowrap; {st}">{label}<span style="color: {BLUE if on else T3}">{n}</span></span>')


def src_head(n, title, sub):
    return (f'<div style="display: flex; gap: 8px; align-items: flex-start">{num(n)}<div style="min-width: 0">'
            f'<div style="font-size: 13.5px; font-weight: 500; line-height: 1.55">{title}</div>'
            f'<div style="margin-top: 1px; font-size: 12px; color: {T3}">{sub}</div></div></div>')


def num(n, mt=1):
    """回答里的出处编号，和下面的原文一一对应。"""
    return (f'<span style="flex-shrink: 0; margin-top: {mt}px; width: 18px; height: 18px; border-radius: 4px; background: {BLUE_TINT}; color: {BLUE}; '
            f'font-size: 11.5px; font-weight: 600; display: inline-flex; align-items: center; justify-content: center">{n}</span>')


def inline_num(n):
    return (f'<span style="display: inline-flex; vertical-align: 2px; margin: 0 2px; width: 16px; height: 16px; border-radius: 4px; background: {BLUE_TINT}; '
            f'color: {BLUE}; font-size: 11px; font-weight: 600; align-items: center; justify-content: center">{n}</span>')


def btn(text, primary=False, ic=None, grow=False):
    st = (f"background: {BLUE}; color: #FFFFFF; border: 1px solid {BLUE}" if primary
          else f"background: {CARD}; color: {BLUE}; border: 1px solid {BLUE}")
    i = icon(ic, 15, "#FFFFFF" if primary else BLUE) if ic else ""
    g = "flex-grow: 1; justify-content: center;" if grow else ""
    return (f'<span style="height: 34px; padding: 0 14px; box-sizing: border-box; display: inline-flex; align-items: center; gap: 5px; {g} '
            f'border-radius: 17px; font-size: 13.5px; font-weight: 500; white-space: nowrap; {st}">{i}{text}</span>')


def quick_chips(items):
    chips = "".join(f'<span style="flex-shrink: 0; height: 32px; padding: 0 14px; display: inline-flex; align-items: center; border-radius: 16px; '
                    f'background: {CARD}; border: 1px solid {LINE2}; font-size: 13px; color: {T1}; white-space: nowrap">{t}</span>' for t in items)
    return f'<div style="flex-shrink: 0; display: flex; gap: 8px; padding: 10px 12px 8px; overflow: hidden">{chips}</div>'


def input_bar(placeholder, note, can_send=False, disabled=False):
    edge = LINE2 if disabled else T1
    send = BLUE if can_send else "#BCCDF7"
    return (f'<div style="flex-shrink: 0; background: {CARD}; border-top: 1px solid {LINE}; padding: 8px 12px 28px">'
            f'<div style="display: flex; align-items: center; gap: 8px">'
            f'<span aria-label="发图片" style="width: 38px; height: 38px; flex-shrink: 0; box-sizing: border-box; border-radius: 19px; border: 1.5px solid {edge}; '
            f'display: flex; align-items: center; justify-content: center">{icon("plus", 20, T3 if disabled else T1)}</span>'
            f'<div style="flex-grow: 1; min-width: 0; height: 40px; padding: 0 14px; box-sizing: border-box; border-radius: 20px; background: {FILL}; display: flex; '
            f'align-items: center; font-size: 14.5px; color: {T3}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">{placeholder}</div>'
            f'<span style="flex-shrink: 0; height: 36px; padding: 0 15px; display: inline-flex; align-items: center; border-radius: 18px; background: {send}; '
            f'color: #FFFFFF; font-size: 14.5px; font-weight: 500">发送</span></div>'
            f'<div style="margin-top: 7px; text-align: center; font-size: 11.5px; color: {T3}">{note}</div></div>')


def notice_meta(d, show_no=True):
    no = f'<div>{d["no"]}</div>' if show_no else ""
    return f'<div style="margin-top: 6px; font-size: 12.5px; line-height: 1.6; color: {T2}"><div>{d["issuer"]} · {d["date"]}</div>{no}</div>'


def phone(top, bottom, min_h=844):
    return (f'<div class="yj-phone" style="width: 390px; min-height: {min_h}px; display: flex; flex-direction: column; background: {BG}; color: {T1}; '
            f'font-family: {FONT}; font-size: 14px; line-height: 1.5; font-variant-numeric: tabular-nums; position: relative; overflow: hidden; text-align: left">'
            f'{top}<div style="flex-grow: 1; min-height: 18px"></div>{bottom}</div>')


# ---------- 照片缩略图（示意：纸质通知、营业执照；执照上的字段一律画成色块，不放任何真实信息） ----------
DESK = "radial-gradient(120% 90% at 30% 20%, #A9AEB6, #6A6F78)"


def photo_notice():
    lines = "".join(f'<div style="height: 3px; margin-top: 5px; width: {w}%; background: #CDCDCD; border-radius: 1px"></div>'
                    for w in (94, 100, 97, 100, 86, 100, 100, 64))
    return (f'<div style="zoom: 0.76"><div style="width: 156px; height: 206px; position: relative; background: {DESK}">'
            f'<div style="position: absolute; left: 18px; top: 16px; width: 122px; height: 172px; box-sizing: border-box; padding: 12px 11px; background: #FBFAF6; '
            f'transform: rotate(-3.5deg); box-shadow: 0 8px 18px rgba(0,0,0,0.28)">'
            f'<div style="text-align: center; font-size: 10px; font-weight: 700; letter-spacing: 1px; color: #C8102E; white-space: nowrap">广东省工业和信息化厅</div>'
            f'<div style="margin: 5px 0 8px; height: 1.5px; background: #C8102E"></div>'
            f'<div style="height: 4px; background: #3A3A3A; border-radius: 1px"></div><div style="height: 4px; margin: 4px auto 0; width: 72%; background: #3A3A3A; border-radius: 1px"></div>'
            f'<div style="height: 3px; margin: 7px auto 4px; width: 46%; background: #9A9A9A"></div>{lines}</div></div></div>')


def photo_license():
    rows = "".join(f'<div style="display: flex; gap: 4px; margin-top: 5px"><span style="width: 16px; height: 3px; background: #8B6B4B"></span>'
                   f'<span style="flex-grow: 1; height: 3px; background: #C2C2C2"></span></div>' for _ in range(5))
    return (f'<div style="zoom: 0.8"><div style="width: 200px; height: 142px; position: relative; background: {DESK}">'
            f'<div style="position: absolute; left: 16px; top: 13px; width: 168px; height: 114px; box-sizing: border-box; padding: 8px 11px; background: #FFFCF3; '
            f'border: 3px solid #C9A063; transform: rotate(2deg); box-shadow: 0 8px 18px rgba(0,0,0,0.28)">'
            f'<div style="display: flex; align-items: center; justify-content: center; gap: 4px"><span style="width: 11px; height: 11px; border-radius: 6px; border: 1.5px solid #C8102E"></span>'
            f'<span style="font-size: 10px; font-weight: 700; letter-spacing: 2px; color: #C8102E">营业执照</span></div>'
            f'<div style="display: flex; gap: 8px; margin-top: 3px"><div style="flex-grow: 1">{rows}</div>'
            f'<div style="width: 26px; height: 26px; flex-shrink: 0; margin-top: 4px; opacity: 0.5; background: repeating-linear-gradient(90deg, #333 0 2px, transparent 2px 4px), '
            f'repeating-linear-gradient(0deg, #333 0 2px, transparent 2px 4px)"></div></div></div></div></div>')


# ---------- 各屏 ----------
EXAMPLES = ["四川 5G 工厂名录，企业在哪个平台填报？", "广东第八批产业技术基础平台要交哪些材料？",
            "海南专精特新申报，线上填报到哪天？"]


def rows_card(rows, mt=10):
    return f'<div style="margin: {mt}px 12px 0; padding: 0 14px; background: {CARD}; border-radius: 12px">{"".join(rows)}</div>'


def ex_row(text, last=False, ic="search", color=T1):
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    return (f'<div style="display: flex; align-items: center; gap: 10px; padding: 13px 0; {b}">{icon(ic, 16, T3 if color == T1 else color)}'
            f'<span style="flex-grow: 1; min-width: 0; font-size: 14px; line-height: 1.5; color: {color}">{text}</span>{icon("chev", 15, T3)}</div>')


def section_label(text, right=""):
    r = f'<span style="font-size: 12.5px; color: {BLUE}">{right}</span>' if right else ""
    return (f'<div style="display: flex; align-items: baseline; justify-content: space-between; padding: 18px 16px 0">'
            f'<span style="font-size: 14px; font-weight: 600">{text}</span>{r}</div>')


def ability(ic, bg, fg, title, desc):
    return (f'<div style="display: flex; gap: 12px; padding: 10px 0">'
            f'<span style="width: 34px; height: 34px; flex-shrink: 0; border-radius: 10px; background: {bg}; display: flex; align-items: center; justify-content: center">{icon(ic, 18, fg)}</span>'
            f'<div style="min-width: 0"><div style="font-size: 14.5px; font-weight: 600">{title}</div>'
            f'<div style="margin-top: 1px; font-size: 12.5px; line-height: 1.6; color: {T2}">{desc}</div></div></div>')


def intro(big=True):
    if not big:
        return (f'<div style="padding: 18px 20px 0"><div style="font-size: 17px; font-weight: 700">问申报通知里的事，每句回答都标出处</div></div>')
    return (f'<div style="padding: 20px 20px 0"><div style="font-size: 21px; font-weight: 700; line-height: 1.45">问申报通知里的事<br>每句回答都标出处</div>'
            f'<div style="margin-top: 8px; font-size: 13.5px; line-height: 1.7; color: {T2}">先从有据收录的政府通知原文里找；收录里没有的，直接告诉你没找到，不编。</div></div>'
            f'<div style="margin: 14px 12px 0; padding: 4px 14px; background: {CARD}; border-radius: 12px">'
            + ability("search", BLUE_TINT, BLUE, "查通知", "从收录的政府通知里找答案，出处标到原句")
            + ability("doc", G_TINT, GREEN, "拍图片", "拍纸质通知或截图，找到官方原文；拍营业执照，自动填企业情况")
            + ability("shield", O_TINT, ORANGE, "联网查", "收录里没有时，可以查 gov.cn 等政府网站，结果标明未经人工核对")
            + '</div>')


def home_foot():
    return (f'<div style="padding: 16px 16px 0; text-align: center; font-size: 11.5px; color: {T3}">模型 [模型名称] · 备案号 [备案号] · '
            f'<span style="color: {T2}">投诉举报</span></div>')


def s1_first():
    top = (nav() + strip() + intro(True) + section_label("可以这样问")
           + rows_card([ex_row(t) for t in EXAMPLES] + [ex_row("拍一张纸质通知，找到官方原文", last=True, ic="plus", color=BLUE)], mt=8)
           + home_foot())
    return phone(top, input_bar("输入问题，或点左边拍图片", "今天还能用 5 次"))


def s1_scope():
    top = (nav() + strip(expanded=True) + intro(False) + section_label("可以这样问")
           + rows_card([ex_row(t, last=(i == 1)) for i, t in enumerate(EXAMPLES[:2])], mt=8))
    return phone(top, input_bar("输入问题，或点左边拍图片", "今天还能用 5 次"))


def recent_row(d, what, when, last=False):
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    return (f'<div style="padding: 13px 0; {b}"><div style="display: flex; align-items: center; gap: 6px">{tag_type("申报通知")}'
            f'<span style="font-size: 12px; color: {T2}">{d["region"]}</span><span style="margin-left: auto; font-size: 12px; color: {T3}">{when}</span></div>'
            f'<div style="margin-top: 6px; display: flex; align-items: center; gap: 10px"><div style="flex-grow: 1; min-width: 0">'
            f'<div style="font-size: 14.5px; font-weight: 600; line-height: 1.55">{d["title"]}</div>'
            f'<div style="margin-top: 3px; font-size: 12.5px; color: {T2}">{what}</div></div>{icon("chev", 16, T3)}</div></div>')


def s1_again():
    top = (nav() + strip() + intro(False) + section_label("最近问过", "全部")
           + rows_card([recent_row(SC5G, "划重点 · 问了 2 个问题", "今天"),
                        recent_row(HN, "和我的企业对一下", "昨天", last=True)], mt=8)
           + f'<div style="padding: 8px 16px 0; font-size: 12px; color: {T3}">对话和整理结果保存 30 天，左滑可以删除</div>'
           + section_label("可以这样问") + rows_card([ex_row(t, last=(i == 1)) for i, t in enumerate(EXAMPLES[1:])], mt=8)
           + home_foot())
    return phone(top, input_bar("输入问题，或点左边拍图片", "今天还能用 4 次"))


def action_row(ic, bg, fg, title, desc, last=False):
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    return (f'<div style="display: flex; align-items: center; gap: 12px; padding: 12px 0; {b}">'
            f'<span style="width: 38px; height: 38px; flex-shrink: 0; border-radius: 11px; background: {bg}; display: flex; align-items: center; justify-content: center">{icon(ic, 20, fg)}</span>'
            f'<div style="flex-grow: 1; min-width: 0"><div style="font-size: 15px; font-weight: 600">{title}</div>'
            f'<div style="margin-top: 1px; font-size: 12.5px; line-height: 1.5; color: {T2}">{desc}</div></div>{icon("chev", 16, T3)}</div>')


def s2_from_notice():
    d = SC5G
    ctx = (f'<div style="margin: 12px 12px 0; padding: 14px 16px 2px; background: {CARD}; border-radius: 12px">'
           f'<div style="display: flex; align-items: center"><span style="font-size: 12px; color: {T3}">正在问这份通知</span>'
           f'<span style="margin-left: auto; font-size: 12.5px; color: {BLUE}">换一份通知</span></div>'
           f'<div style="margin-top: 10px; display: flex; align-items: center; gap: 6px">{tag_type("申报通知")}<span style="font-size: 12px; color: {T2}">{d["region"]}</span></div>'
           f'<div style="margin-top: 7px; font-size: 16.5px; font-weight: 600; line-height: 1.55">{d["title"]}</div>'
           f'{notice_meta(d)}<div style="font-size: 12.5px; color: {T2}">正文和 1 个附件</div>'
           f'<div style="margin-top: 12px; border-top: 1px solid {LINE}">'
           + action_row("list", BLUE_TINT, BLUE, "划重点", "摘出申报对象、条件、时间、材料的原句")
           + action_row("doc", G_TINT, GREEN, "要准备哪些材料", "只看材料相关的原句和附件提醒")
           + action_row("user", O_TINT, ORANGE, "和我的企业对一下", "条件原句和你的企业情况并排列出<br>不判断能否申报", last=True)
           + '</div></div>')
    top = (nav() + strip() + sys_note("从通知详情打开，已带上这份通知；点按钮才开始整理") + ctx
           + section_label("也可以直接问这份通知")
           + f'<div style="display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 12px 0">'
           + "".join(f'<span style="height: 34px; padding: 0 14px; display: inline-flex; align-items: center; border-radius: 17px; background: {CARD}; '
                     f'border: 1px solid {LINE2}; font-size: 13.5px; white-space: nowrap">{t}</span>' for t in ["推荐项目必须已经建成吗？", "企业在哪个平台填报？", "截止到哪天？"])
           + '</div>')
    return phone(top, input_bar("问这份通知，如：截止到哪天？", "今天还能用 5 次 · 划重点和要准备哪些材料共用 1 次"))


def deadline_block():
    """截止时间：8–30 天内的可以显示机器摘录，同一行附原文、标“机器摘录 · 未经人工核对”（D-061）；按原文分清是谁的截止。"""
    rows = [("10月30日前", "企业填报截止", 13, "组织本地区企业，通过“5G+工业互联网发展管理平台”……于10月30日前填报项目并上传相关材料"),
            ("11月4日前", "市（州）报送截止", 18, "……于11月4日前将盖章版推荐项目汇总表发送到指定邮箱")]
    out = []
    for i, (date, role, days, frag) in enumerate(rows):
        b = f"border-top: 1px solid {LINE};" if i else ""
        out.append(f'<div style="padding: 9px 0; {b}"><div style="display: flex; align-items: baseline; gap: 8px">'
                   f'<span style="font-size: 15px; font-weight: 700">{date}</span><span style="font-size: 13px">{role}</span>'
                   f'<span style="margin-left: auto; font-size: 12.5px; font-weight: 600; color: {RED if days <= 14 else T2}">剩 {days} 天</span></div>'
                   f'<div style="margin-top: 3px; font-size: 12.5px; line-height: 1.6; color: {T2}">原文：{frag}</div></div>')
    return (f'<div style="margin-top: 12px; padding: 2px 12px 0; border: 1px solid {LINE2}; border-radius: 10px">'
            f'<div style="display: flex; align-items: center; padding-top: 8px; font-size: 12px; color: {T3}"><span>截止时间</span>'
            f'<span style="margin-left: auto">机器摘录 · 未经人工核对</span></div>{"".join(out)}</div>')


def s3_highlights():
    chips = (f'<div style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px">'
             + cat_chip("全部", 5, on=True) + cat_chip("申报对象", 1) + cat_chip("申报条件", 2) + cat_chip("申报时间", 2)
             + cat_chip("申报方式", 1) + cat_chip("材料清单", 3) + '</div>')
    missing = (f'<div style="margin-top: 10px; display: flex; gap: 6px; font-size: 12.5px; line-height: 1.6; color: {T2}">'
               f'<span style="flex-shrink: 0; color: {T1}; font-weight: 500">没摘到</span><span>支持方式（原文可能没写，也可能漏摘，请看原文）</span></div>')
    items = (quote_item(["申报对象"], S_OBJ, "一、申报要求")
             + quote_item(["申报条件"], S_GUIDE, "一、申报要求")
             + quote_item(["申报时间", "申报方式", "材料清单"], S_PLATFORM, "二、工作程序")
             + quote_item(["申报条件", "申报时间", "材料清单"], S_REVIEW, "三、有关要求")
             + quote_item(["材料清单"], S_ATT, "附件", last=True))
    body = (caution("这份通知有 1 个附件《5G工厂入库项目推荐汇总表》，下面只摘了网页正文，附件内容没有读。申请表、材料清单可能在附件里。", "看原文附件")
            + deadline_block() + chips + missing
            + items)
    top = (nav() + strip() + ctx_bar(SC5G) + user_msg("划重点")
           + ai_card("划重点", body, right="5 句 · 都和原文逐字一致", foot="quote"))
    return phone(top, quick_chips(["要准备哪些材料", "和我的企业对一下", "截止到哪天？"]) + input_bar("问这份通知，如：在哪个平台填报？", "今天还能用 4 次"))


def s4_ask():
    a1 = (para(f'通知要求推荐的是已经完成的项目{inline_num(1)}。还在建设中的项目能不能报，通知没有另外说明，请以原文和主管部门的解释为准。')
          + f'<div style="margin-top: 14px; font-size: 12px; color: {T3}">出处</div>'
          + f'<div style="margin-top: 6px">{src_head(1, "川经信办函〔2026〕356号 · 三、有关要求", "四川省经济和信息化厅 · 2026-10-16 发布")}</div>'
          + quote_block("请各市（州）经济和信息化局严格审核企业填报的材料（<b style=\"font-weight: 600; background: #FFF0B8\">推荐项目需为已完成项目</b>），"
                        "确保项目材料的真实性、合规性和完整性，于11月4日前将盖章版推荐项目汇总表发送到指定邮箱。")
          + link_row(copy=True))
    a2 = (para("没找到。这份通知没有写入选后的补贴或奖励。")
          + para("还查了有据收录的四川省和国家部委的通知（关键词：5G 工厂、补贴、奖励），也没有找到能回答这个问题的原文。", 14, T2, 6)
          + para("没找到不等于没有，可以：", 13.5, T2, 8)
          + f'<div style="display: flex; gap: 8px; margin-top: 10px">{btn("到政府网站查一查", True, "search")}{btn("看原文里的联系电话")}</div>'
          + f'<div style="margin-top: 10px; font-size: 12px; color: {T3}">联网查会用掉 1 次联网次数（今天还剩 [次数] 次）</div>')
    top = (nav() + strip() + ctx_bar(SC5G) + user_msg("推荐项目必须已经建成吗？还在建的能报吗？")
           + ai_card("回答", a1, right="依据 1 句原文", foot="answer")
           + user_msg("入选以后有补贴吗？")
           + ai_card("回答", a2, right="没找到 · 不扣次数", foot="answer"))
    return phone(top, input_bar("继续问这份通知", "今天还能用 3 次"))


def notice_box(d, buttons):
    return (f'<div style="margin-top: 10px; padding: 12px; border: 1px solid {LINE2}; border-radius: 10px">'
            f'<div style="display: flex; align-items: center; gap: 6px">{tag_type("申报通知")}<span style="font-size: 12px; color: {T2}">{d["region"]}</span>'
            f'<span style="margin-left: auto; font-size: 12px; color: {T2}">{d["atts"]} 个附件</span></div>'
            f'<div style="margin-top: 7px; font-size: 15px; font-weight: 600; line-height: 1.55">{d["title"]}</div>'
            f'{notice_meta(d, show_no=False)}'
            f'<div style="display: flex; gap: 8px; margin-top: 12px">{buttons}</div></div>')


def s5_photo():
    ocr = (f'<div style="font-size: 12px; color: {T3}">识别出的文字</div>'
           f'<div style="margin-top: 6px; padding: 10px 12px; background: {QUOTE_BG}; border-radius: 8px; font-size: 13px; line-height: 1.7; color: {T2}">'
           f'<div style="color: {T1}; font-weight: 500">{GD8["title"]}</div><div>{GD8["no"]}</div>'
           f'<div>各地级以上市工业和信息化主管部门，横琴粤澳深度合作区产业发展局：……</div>'
           f'<div style="margin-top: 4px; color: {BLUE}">展开全部识别文字 ›</div></div>')
    found = (f'<div style="margin-top: 14px; display: flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 600">{icon("check", 16, GREEN)}'
             f'在有据收录里找到了这份通知</div>'
             f'<div style="margin-top: 2px; padding-left: 22px; font-size: 12.5px; color: {T2}">标题和文号都对得上 · 政府网站 {GD8["date"]} 发布</div>'
             + notice_box(GD8, btn("划重点", True, grow=True) + btn("要准备哪些材料", grow=True) + btn("看原文")))
    tail = para("后面的回答都按政府网站上的原文来，不按照片里的字。照片识别完就删除，不保存。", 12.5, T2, 12)
    top = (nav() + strip() + user_img(photo_notice(), "图片已通过内容安全检测") + user_msg("帮我找到这份通知的原文")
           + ai_card("识别图片", ocr + found + tail, right="用了 1 次", foot="image"))
    return phone(top, quick_chips(["划重点", "要准备哪些材料", "截止到哪天？"]) + input_bar("问这份通知", "今天还能用 4 次"))


def field(label, value, src="", placeholder=False, last=False):
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    v_color = T3 if placeholder else T1
    s = (f'<span style="flex-shrink: 0; padding: 0 5px; line-height: 18px; border-radius: 4px; background: {G_TINT}; color: {GREEN}; font-size: 11px">{src}</span>'
         if src else "")
    return (f'<div style="display: flex; align-items: center; gap: 8px; padding: 11px 0; {b}"><span style="width: 64px; flex-shrink: 0; font-size: 13px; color: {T2}">{label}</span>'
            f'<span style="flex-grow: 1; min-width: 0; font-size: 14px; color: {v_color}">{value}</span>{s}</div>')


def qual_chip(t, on=False):
    st = (f"background: {BLUE_TINT}; color: {BLUE}; border: 1px solid {BLUE}; font-weight: 500" if on
          else f"background: {FILL}; color: {T1}; border: 1px solid {FILL}")
    return f'<span style="height: 30px; padding: 0 11px; display: inline-flex; align-items: center; border-radius: 15px; font-size: 12.5px; white-space: nowrap; {st}">{t}</span>'


def s6_license():
    ask = (para("对照只用到这几项：成立日期、注册地、营业收入、研发费用、职工人数、已有资质。每项都可以不填。", 14.5)
           + caution("营业执照上有法定代表人姓名、统一社会信用代码等信息。图片只用来识别成立日期和注册地，识别完就删除；"
                     "你填的企业情况只用于这次对照，不发给 AI 模型，7 天后自动清除。", mt=10)
           + f'<div style="display: flex; gap: 8px; margin-top: 12px">{btn("同意并拍营业执照", True, "plus", grow=True)}{btn("手动填写", grow=True)}</div>')
    form = (f'<div style="font-size: 12.5px; color: {T2}">从营业执照读到 2 项，请核对，识别可能有误</div>'
            f'<div style="margin-top: 4px">'
            + f'<div style="display: flex; align-items: center; gap: 8px; padding: 11px 0; border-bottom: 1px solid {LINE}">'
            f'<span style="width: 64px; flex-shrink: 0; font-size: 13px; color: {T2}">哪家企业</span>'
            f'<span style="flex-grow: 1; min-width: 0; font-size: 14px; color: {T3}">[企业简称，只用来区分]</span>'
            f'<span style="flex-shrink: 0; font-size: 12.5px; color: {BLUE}">从我的客户选</span></div>'
            + field("成立日期", "[识别出的日期]", "营业执照")
            + field("注册地", "海南省 · 海口市 · [区县]", "营业执照")
            + f'<div style="padding: 11px 0; border-bottom: 1px solid {LINE}"><div style="font-size: 13px; color: {T2}">已有资质（可多选）</div>'
            + f'<div style="margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px">{qual_chip("高新技术企业")}{qual_chip("科技型中小企业")}'
            + f'{qual_chip("创新型中小企业", True)}{qual_chip("专精特新中小企业")}{qual_chip("专精特新“小巨人”企业")}</div>'
            + f'<div style="margin-top: 10px; display: flex; align-items: center; gap: 8px; font-size: 13px"><span style="color: {T2}">创新型中小企业 · 有效期至</span>'
            f'<span style="color: {T3}">[选择日期]</span><span style="margin-left: auto; font-size: 12px; color: {T3}">每项资质分别填</span></div></div>'
            + field("营业收入", "可不填 · 万元", placeholder=True)
            + field("研发费用", "可不填 · 万元", placeholder=True)
            + field("职工人数", "可不填", placeholder=True, last=True)
            + f'</div><div style="display: flex; margin-top: 12px">{btn("开始对照", True, grow=True)}</div>'
            + f'<div style="margin-top: 8px; text-align: center; font-size: 12px; color: {T3}">开始对照算 1 次；营业执照图片已删除</div>')
    top = (nav() + strip() + ctx_bar(HN) + user_msg("和我的企业对一下")
           + ai_card("先填企业情况", ask, foot=None)
           + user_img(photo_license(), "图片已通过内容安全检测")
           + ai_card("核对企业情况", form, foot=None))
    return phone(top, input_bar("也可以直接打字补充企业情况", "今天还能用 3 次"))


def clause_row(text, src, mine, last=False):
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    if mine:
        right = "".join(f'<div style="{"margin-top: 8px;" if i else ""}"><div style="font-size: 12px; color: {T2}">{k}</div>'
                        f'<div style="margin-top: 2px; font-size: 13.5px; line-height: 1.6; color: {T3 if muted else T1}">{v}</div>'
                        + (f'<div style="margin-top: 2px; font-size: 11px; color: {GREEN}">来自{s}</div>' if s else "") + '</div>'
                        for i, (k, v, s, muted) in enumerate(mine))
        rbg = MINE_BG
    else:
        right = f'<div style="font-size: 13px; line-height: 1.6; color: {T3}">没有对应的填写项，请对照原文自己核对</div>'
        rbg = FILL
    return (f'<div style="padding: 12px 0; {b}">'
            f'<div style="display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 8px">'
            f'<div style="padding: 9px 10px; background: {QUOTE_BG}; border-radius: 8px; font-size: 13.5px; line-height: 1.7; overflow-wrap: anywhere">{text}</div>'
            f'<div style="padding: 9px 10px; background: {rbg}; border-radius: 8px">{right}</div></div>'
            f'<div style="margin-top: 8px; display: flex; align-items: center; font-size: 12px"><span style="color: {T3}">{src}</span>'
            f'<span style="margin-left: 10px; color: {BLUE}">看原文 ›</span>'
            f'<span style="margin-left: auto; color: {T2}">请你自己判断</span></div></div>')


def s7_clause():
    head = (para("左边是 AI 从通知原文摘出的申报对象和条件，右边是你填的企业情况（只在这里并排显示，没有发给 AI 模型）。有据不判断能不能申报，每一行请你自己对照。", 14)
            + caution("这份通知有 4 个附件。认定标准在附件 4《优质中小企业梯度培育管理办法（2026年）》里，这里没有对照，只对照了网页正文。", "看原文附件", mt=10)
            + f'<div style="margin-top: 12px; display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 8px; font-size: 12px; color: {T3}">'
            f'<span>通知原文（正文 5 条，不含附件）</span><span>你填的情况</span></div>')
    rows = "".join(clause_row(t, s, m, last=(i == len(HN_ROWS) - 1)) for i, (t, s, m) in enumerate(HN_ROWS))
    tail = (f'<div style="margin-top: 4px; display: flex; align-items: center; gap: 12px; font-size: 12.5px">'
            f'<span style="color: {BLUE}">修改企业情况</span><span style="color: {BLUE}">删除企业情况</span><span style="margin-left: auto; color: {T3}">不删也会 7 天后清除</span></div>')
    top = (nav() + strip() + ctx_bar(HN) + user_msg("开始对照")
           + ai_card("和我的企业对一下", head + rows + tail, right="条款对照", foot="clause"))
    return phone(top, quick_chips(["要准备哪些材料", "线上填报到哪天？", "划重点"]) + input_bar("问这份通知", "今天还能用 2 次"))


def web_src(n, w, last=False):
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    return (f'<div style="padding: 12px 0; {b}">{src_head(n, w["title"], w["kind"] + " · " + w["site"] + " · " + w["domain"] + " · " + w["date"])}'
            f'{quote_block(w["quote"], 13.5)}'
            f'<div style="margin-top: 8px; display: flex; align-items: center; font-size: 12.5px">'
            f'<span style="display: inline-flex; align-items: center; gap: 3px; color: {BLUE}">{icon("copy", 13, BLUE)}复制网页链接</span>'
            f'<span style="margin-left: auto; padding: 0 6px; line-height: 20px; border-radius: 4px; background: {O_TINT}; color: {O_TXT}; font-size: 11.5px">未经人工核对</span></div></div>')


def s8_web():
    body = (caution("下面的内容来自政府网站，未经有据人工核对。只查了 gov.cn 等政府网站，没有查其他网站。")
            + para("<b style=\"font-weight: 600\">没找到专门规定。</b>政府网站上没有找到“入选 5G 工厂名录给多少补贴”的明确规定。下面两条只是相关：", 14.5, T1, 12)
            + f'<div style="margin-top: 8px; display: flex; flex-direction: column; gap: 8px; font-size: 14.5px; line-height: 1.7">'
            f'<div style="display: flex; gap: 8px"><span style="color: {T3}">·</span><span>四川省发展改革委等部门 2026 年 1 月印发的数字经济政策措施，支持“智改数转”等项目，单个项目不超过设备（含软件）投资额 20%{inline_num(1)}。这不是专门针对 5G 工厂名录的规定。</span></div>'
            f'<div style="display: flex; gap: 8px"><span style="color: {T3}">·</span><span>四川省国资委网站 2025 年 9 月转载的报道里，省经济和信息化厅负责人提到将制定“5G+工业互联网”政策措施{inline_num(2)}。</span></div></div>'
            + f'<div style="margin-top: 14px; font-size: 12px; color: {T3}">网页和摘录（共 2 个）</div>'
            + web_src(1, WEB[0]) + web_src(2, WEB[1], last=True)
            + para("小程序里不能直接打开政府网站，复制链接后在浏览器打开核对。", 12.5, T2, 4))
    top = (nav() + strip() + ctx_bar(SC5G) + user_msg("到政府网站查一查：入选以后有补贴吗？")
           + f'<div style="padding: 14px 16px 0; display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: {T2}">{icon("search", 14, T2)}'
           f'查了政府网站，找到 2 个相关网页 · 用了 1 次联网</div>'
           + ai_card("联网查政府网站", body, right="来自政府网站", foot="web"))
    return phone(top, input_bar("继续问这份通知", "今天还能用 3 次 · 联网还剩 [次数] 次"))


def state_label(text):
    return (f'<div style="padding: 22px 16px 0; display: flex; align-items: center; gap: 8px; font-size: 12px; color: {T3}">'
            f'<span style="height: 1px; width: 16px; background: {LINE2}"></span>{text}<span style="height: 1px; flex-grow: 1; background: {LINE2}"></span></div>')


def state_card(title, text, actions="", right="", tone=None):
    ic = {"warn": icon("info", 16, O_TXT), "off": icon("info", 16, T3), None: ""}[tone]
    return (f'<div style="margin: 10px 12px 0; padding: 12px 14px; background: {CARD}; border-radius: 12px">'
            f'<div style="display: flex; align-items: center; gap: 6px">{ic}<span style="font-size: 14.5px; font-weight: 600">{title}</span>'
            + (f'<span style="margin-left: auto; font-size: 12px; color: {T3}">{right}</span>' if right else "")
            + f'</div><div style="margin-top: 6px; font-size: 13.5px; line-height: 1.7; color: {T2}">{text}</div>'
            + (f'<div style="display: flex; gap: 8px; margin-top: 10px">{actions}</div>' if actions else "") + '</div>')


def check_box(on=True):
    st = f"background: {BLUE}; border: 1.5px solid {BLUE}" if on else f"background: {CARD}; border: 1.5px solid {LINE2}"
    return (f'<span style="width: 16px; height: 16px; flex-shrink: 0; box-sizing: border-box; border-radius: 4px; {st}; display: inline-flex; '
            f'align-items: center; justify-content: center">{icon("check", 12, "#FFFFFF")}</span>')


def s9_states():
    terms = ["有据是第三方整理的信息工具，不是政府平台，不代办申报，也不判断能不能申报。",
             "回答由 AI 根据收录的政府通知原文整理，每句附出处，引用的原句经程序逐字核对；仍可能不全或有误，请以原文和主管部门的解释为准。",
             "收录里找不到时可以联网查政府网站，这部分内容未经人工核对。",
             "上传的图片先做内容安全检测；营业执照只识别成立日期和注册地。不面向未成年人。"]
    consent = (f'<div style="margin: 10px 12px 0; padding: 14px; background: {CARD}; border-radius: 12px">'
               f'<div style="display: flex; align-items: center; gap: 8px">{ai_tag()}<span style="font-size: 15px; font-weight: 600">开始前请看一下</span></div>'
               + "".join(f'<div style="display: flex; gap: 8px; margin-top: 8px; font-size: 13.5px; line-height: 1.7; color: {T2}"><span style="color: {T1}">{i + 1}.</span><span>{t}</span></div>'
                         for i, t in enumerate(terms))
               + f'<div style="margin-top: 12px; display: flex; align-items: center; gap: 8px; font-size: 13px">{check_box()}<span>我已阅读并同意<span style="color: {BLUE}">《AI 助手使用说明》</span></span></div>'
               + f'<div style="display: flex; margin-top: 12px">{btn("开始提问", True, grow=True)}</div>'
               + f'<div style="margin-top: 8px; text-align: center; font-size: 12px; color: {T3}">同意后接着处理你刚才的操作，不用重来</div></div>')
    working = (f'<div style="margin: 10px 12px 0; padding: 12px 14px; background: {CARD}; border-radius: 12px">'
               f'<div style="display: flex; align-items: center; gap: 8px">{ai_tag()}<span style="font-size: 14.5px; font-weight: 600">正在整理</span>'
               f'<span style="margin-left: auto; display: inline-flex; gap: 4px">' + "".join(f'<span style="width: 6px; height: 6px; border-radius: 3px; background: {c}"></span>' for c in (BLUE, "#8FA9EE", "#C9D6F7")) + '</span></div>'
               f'<div style="margin-top: 10px; display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: {T2}">'
               f'<div style="display: flex; align-items: center; gap: 6px">{icon("check", 14, GREEN)}在收录的通知里找到 1 份</div>'
               f'<div style="display: flex; align-items: center; gap: 6px"><span style="width: 14px; height: 14px; box-sizing: border-box; border-radius: 7px; border: 2px solid {BLUE}; border-right-color: transparent"></span>正在和原文逐字核对</div></div>'
               f'<div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid {LINE}; font-size: 12.5px; line-height: 1.6; color: {T3}">比平时慢：服务刚启动，大约还要 1 分钟。可以先离开，整理好的回答会留在这条对话里。</div></div>')
    top = (nav() + strip()
           + state_label("第一次用之前（提问、点按钮、发图片都先出这一步）") + user_msg("海南专精特新线上填报到哪天？") + consent
           + state_label("正在整理（服务刚启动时）") + working
           + state_label("问了范围外的事") + user_msg("帮我写一份 5G 工厂的申报书")
           + state_card("这个做不了", "AI 助手只摘通知原文、回答通知里的问题，不代写申报材料，也不代办申报。可以试试“要准备哪些材料”。", right="不扣次数", tone="off")
           + state_label("今天的次数用完")
           + state_card("今天的 5 次用完了", "明天恢复。处理失败、没有回答的都没有算次数。已经整理好的结果随时可以回来看。", right="明天恢复", tone="off")
           + state_label("没整理出来")
           + state_card("这次没整理出结果，没有扣次数", "这份通知的正文里，没能摘出和原文逐字一致的句子。请直接看原文，或稍后重试。",
                        btn("看原文", True) + btn("重试"), tone="warn")
           + state_label("连不上服务器")
           + state_card("没有发出去", "连不上服务器，你的问题还在输入框里。网络恢复后点重试，不扣次数。", btn("重试", True), tone="warn")
           + state_label("图片没通过检测")
           + state_card("这张图片没有识别", "图片没有通过内容安全检测，没有识别，也没有扣次数。请换一张只拍通知正文的照片。", btn("重新选图片"), tone="warn"))
    return phone(top, input_bar("今天的次数已用完，明天恢复", "已经整理好的结果可以继续看", disabled=True))


# ---------- 评审稿 ----------
SCREENS = [
    ("s1a", "1", "首页 · 第一次打开", s1_first,
     "一句话说清能做什么，下面是三种问法和示例问题；顶部固定一行「AI 生成」，不能关闭。第一次提问前另有使用须知（屏 9）。",
     ["AI 生成", "第三方 · 非政府平台", "次数"]),
    ("s1c", "1", "首页 · 点开「说明」", s1_scope,
     "「说明」里是第十条要公开的适用人群、场合、用途、不适用，加上次数、图片、保存期限、模型和备案号、投诉举报；默认收起。",
     ["第十条说明", "不面向未成年人", "模型和备案号", "投诉举报"]),
    ("s1b", "1", "首页 · 再次打开", s1_again,
     "没有「工具」「任务」：以前的划重点和对照按通知收在「最近问过」里，点开接着问。",
     ["AI 生成", "第三方 · 非政府平台", "次数"]),
    ("s2", "2", "从通知进入", s2_from_notice,
     "在通知详情点「AI 解读」，带着这份通知进来；先给三个最常用的按钮，点了才开始，不自动扣次数。",
     ["AI 生成", "第三方 · 非政府平台", "不判断能否申报"]),
    ("s3", "3", "划重点", s3_highlights,
     "附件提醒放最前；截止时间分清是企业的还是市州的，标「机器摘录 · 未经人工核对」；每句原文只出现一次，标出属于哪几类；没摘到的类别合成一行。",
     ["AI 生成", "出处到原句", "这句摘错了", "不判断能否申报", "模型和备案号"]),
    ("s4", "4", "自由提问", s4_ask,
     "先一句话回答，再贴出所依据的原句；找不到就说没找到、不扣次数，并给出下一步。",
     ["AI 生成", "出处到原句", "找不到就说", "不判断能否申报", "模型和备案号"]),
    ("s5", "5", "拍纸质通知", s5_photo,
     "照片只用来找原文：识别出标题和文号，在收录里找到官方通知后，后面的回答都按政府网站上的原文来。",
     ["AI 生成", "图片安全检测", "不保存图片"]),
    ("s6", "6", "拍营业执照", s6_license,
     "拍照前先说清楚执照上有个人信息、只读两项、图片识别完就删、企业情况不发给模型；读到的每项都要用户核对。",
     ["AI 生成", "个人信息提示", "图片安全检测", "7 天清除"]),
    ("s7", "7", "和我的企业对一下", s7_clause,
     "左边原文条件、右边你填的情况；原句提到的每一项都列出填了什么或「没有填」，只摆事实；每行标「请你自己判断」，没有对应项的条件照样列出。",
     ["AI 生成", "请你自己判断", "不判断能否申报", "附件提醒", "模型和备案号"]),
    ("s8", "8", "联网查政府网站", s8_web,
     "只查政府网站，整块标「来自政府网站 · 未经人工核对」；每条结论对应一个网页和摘录，链接可以复制。",
     ["AI 生成", "未经人工核对", "只查政府网站", "模型和备案号"]),
    ("s9", "9", "次数用完、出错、连不上", s9_states,
     "7 种情况放在一起看，实际一次只出现一种。每种都说清楚原因、扣没扣次数、下一步怎么办；问题留在输入框里，不用重打。",
     ["AI 生成", "使用须知", "次数上限", "不扣次数", "不面向未成年人"]),
]

COMPLIANCE = [
    ("显式「AI 生成」标识", "《人工智能生成合成内容标识办法》第四条",
     "每屏导航栏下方固定一条，不能关闭；每张回答卡片开头、末尾各标一次；复制出去的内容开头带「AI 生成」", "1–9"),
    ("第三方整理 · 非政府平台", "全站统一（D-014）", "顶部那一条；使用须知第 1 条", "1–9"),
    ("不判断能否申报，以通知原文和主管部门解释为准", "用户协议 八·1", "每张回答卡片末尾；对照卡片开头说明，每行「请你自己判断」", "3、4、7、8"),
    ("投诉举报入口", "暂行办法第十五条；用户协议 八·4（15 个工作日内答复）", "说明面板；每张回答卡片末尾；每句「这句摘错了」", "1、3、4、7、8"),
    ("适用人群、场合、用途、不适用", "《生成式人工智能服务管理暂行办法》第十条", "导航栏下方那一条右边的「说明」，点开就是（默认收起）", "1"),
    ("模型名称和备案号", "网信办 2025-09-10 公告", "每张回答卡片末尾、说明面板；[模型名称] [备案号] 等网信办答复后填", "1、3、4、7、8"),
    ("每天次数上限，失败、拒答不扣", "用户协议 八·1", "输入框下方剩余次数；说明面板；每个失败状态写明「不扣次数」", "1–9"),
    ("图片内容安全检测", "内容安全（微信接口）", "每张图片下方标「已通过内容安全检测」；没通过时不识别、不扣次数", "5、6、9"),
    ("营业执照里的个人信息", "个人信息告知同意", "拍照前说明只读两项、识别完删除、不发给模型、7 天清除", "6、7"),
    ("联网内容未经人工核对", "创始人 10/17 定的原则", "整块橙色提醒 + 每个网页标「未经人工核对」，只查政府网站", "8"),
    ("不面向未成年人", "暂行办法第十条", "说明面板「不适用」；使用须知第 4 条", "1、9"),
]

TECH = [
    ("对话要秒回", "api 冷启动约 67 秒（10/12 实测），对话等不了。对话接口要放在常驻的 admin 上，或者给 api 留常驻实例。现在的解读是排队任务加轮询（一般 10–30 秒），对话版要改成边生成边显示。"),
    ("图片先过内容安全", "微信图片检测接口（/wxa/media_check_async）是异步的，结果推送回服务器，要实测等多久；等不了就改用同步的腾讯云图片内容安全（按次计费）。没通过的图片不识别。"),
    ("看图要另配", "DeepSeek 官方 deepseek-v4-pro 不支持看图。两条路：用文字识别接口（通用文字识别、营业执照识别），只把识别出的文字交给 DeepSeek；或者换能看图的模型（要重新评测，公示和登记的模型也跟着改）。"),
    ("联网只查政府网站", "需要能限定站点的搜索接口，按次计费；只放行 gov.cn 等政府域名，网页摘录也过一遍内容安全，再交给模型整理。每条结论必须对得上一个网页的原话，对不上的删掉。"),
    ("每句都有出处", "沿用现在的逐字核对：模型只给段号和原句，程序回原文核对，对不上的删掉；一条都对不上就回答“没找到”。自由提问的结论句也要挂到原句上。"),
    ("登记材料和隐私指引要跟着改", "上线登记写的功能里要加上图片识别、联网检索（先问网信办能否后补）；隐私指引补上提问文字、图片、联网检索的处理和保存期限；协议八·2 补上复制带标识。"),
]

QUESTIONS = [
    ("次数怎么算", "建议：划重点、对照、提问共用每人每天 5 次；联网单独算，每天 [3] 次。也可以沿用现在的「每类 5 次」。"),
    ("对话接口放哪台服务", "建议先放在常驻的 admin 上（不加钱，抓取忙时可能变慢）；用的人多了再给 api 留常驻实例（要加钱，10/12 没同意）。"),
    ("图片怎么识别", "建议用文字识别接口，模型继续用 DeepSeek，公示和登记不用改；换能看图的模型要重新评测、改公示。"),
    ("联网检索用哪家服务、每天几次", "建议先选一家能限定政府网站的搜索接口试用，团队给出两三家的价格和效果对比后你定；次数先从每人每天 [3] 次起。"),
    ("「AI 助手」放在哪", "建议：首页加「AI 助手」入口，通知详情页右下角改叫「AI 解读」；不放进底栏。「工具中心」只留批量名单比对和客户相关通知表。"),
]


def board():
    shots = []
    for sid, n, title, fn, intent, chips in SCREENS:
        tags = "".join(f'<span class="chip">{c}</span>' for c in chips)
        shots.append(f'<figure class="shot" id="{sid}"><figcaption><div class="shot-no">屏 {n}</div><h3>{title}</h3>'
                     f'<p>{intent}</p><div class="chips">{tags}</div></figcaption>'
                     f'<div class="frame-wrap"><div class="frame">{fn()}</div></div></figure>')
    comp_rows = "".join(f'<tr><td class="c-what">{a}</td><td>{b}</td><td>{c}</td><td class="c-num">{d}</td></tr>' for a, b, c, d in COMPLIANCE)
    tech = "".join(f'<li><b>{a}</b><span>{b}</span></li>' for a, b in TECH)
    qs = "".join(f'<li><b>{a}</b><span>{b}</span></li>' for a, b in QUESTIONS)
    src = "".join(f'<li><a href="{u}" target="_blank" rel="noopener">{t}</a></li>' for t, u in SOURCES)
    return f"""<title>有据 AI 助手对话版</title>
<meta name="claude:viewport-fit-sides" content="cover">
{FONTS_LINK.replace("&amp;", "&")}
<style>
/* 评审板：说明在上、手机画面按屏排开，画面本身是小程序的浅色界面，不随主题变；外框和文字跟随主题 */
:root {{
  --bg: #ECEFF4; --surface: #FFFFFF; --ink: #1B2130; --muted: #566072; --faint: #868F9E; --line: #D8DDE6;
  --accent: #2A5CE0; --accent-tint: #E3EBFF; --frame: #FFFFFF; --shadow: 0 1px 2px rgba(20, 30, 60, 0.06), 0 12px 32px rgba(20, 30, 60, 0.10);
  --font: 'Noto Sans SC', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #0E1116; --surface: #161A21; --ink: #E6E9EF; --muted: #A2AABA; --faint: #7C8595; --line: #2A303B;
  --accent: #82A5FF; --accent-tint: #1B2847; --frame: #2A303B; --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 12px 32px rgba(0,0,0,0.35); color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #0E1116; --surface: #161A21; --ink: #E6E9EF; --muted: #A2AABA; --faint: #7C8595; --line: #2A303B;
  --accent: #82A5FF; --accent-tint: #1B2847; --frame: #2A303B; --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 12px 32px rgba(0,0,0,0.35); color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: var(--font); font-size: 15px; line-height: 1.7;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }}
.wrap {{ max-width: 1300px; margin: 0 auto; padding-block: 40px 72px; }}
.eyebrow {{ font-size: 13px; letter-spacing: 0.08em; color: var(--muted); }}
h1 {{ margin: 6px 0 0; font-size: 34px; line-height: 1.25; font-weight: 700; text-wrap: balance; }}
.lede {{ margin: 14px 0 0; max-width: 46em; font-size: 16px; color: var(--muted); }}
.pills {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }}
.pill {{ padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line); background: var(--surface); font-size: 13px; color: var(--muted); }}
.pill b {{ color: var(--ink); font-weight: 600; }}
h2 {{ margin: 56px 0 0; font-size: 22px; line-height: 1.35; font-weight: 700; text-wrap: balance; }}
.sub {{ margin: 6px 0 0; color: var(--muted); font-size: 14.5px; max-width: 52em; }}
.flow {{ margin-top: 18px; display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1.1fr) auto minmax(0, 1.6fr); gap: 12px; align-items: center; }}
.box {{ background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; font-size: 14px; line-height: 1.6; }}
.box small {{ display: block; color: var(--faint); font-size: 12.5px; }}
.box + .box {{ margin-top: 10px; }}
.arrow {{ color: var(--faint); font-size: 20px; }}
.caps {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }}
.caps span {{ padding: 2px 9px; border-radius: 6px; background: var(--accent-tint); color: var(--accent); font-size: 13px; }}
.screens {{ margin-top: 22px; display: grid; grid-template-columns: repeat(auto-fill, minmax(392px, 1fr)); gap: 0 36px; }}
.shot {{ margin: 0 0 52px; display: grid; grid-row: span 2; grid-template-rows: subgrid; row-gap: 16px; min-width: 0; align-content: start; }}
.shot-no {{ font-size: 12.5px; letter-spacing: 0.06em; color: var(--accent); font-weight: 600; }}
.shot h3 {{ margin: 2px 0 0; font-size: 18px; line-height: 1.4; }}
.shot p {{ margin: 6px 0 0; font-size: 14px; line-height: 1.7; color: var(--muted); max-width: 392px; }}
.chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; max-width: 392px; }}
.chip {{ padding: 1px 8px; border-radius: 6px; border: 1px solid var(--line); font-size: 12px; color: var(--muted); background: var(--surface); }}
.frame-wrap {{ max-width: 100%; overflow-x: auto; }}
.frame {{ width: 392px; border: 1px solid var(--frame); border-radius: 26px; overflow: hidden; box-shadow: var(--shadow); background: #F2F4F8; }}
.table-wrap {{ margin-top: 16px; overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }}
table {{ width: 100%; min-width: 720px; border-collapse: collapse; font-size: 14px; line-height: 1.6; }}
th, td {{ text-align: left; vertical-align: top; padding: 11px 14px; border-bottom: 1px solid var(--line); }}
th {{ font-size: 12.5px; font-weight: 600; color: var(--faint); letter-spacing: 0.04em; }}
tr:last-child td {{ border-bottom: 0; }}
.c-what {{ font-weight: 600; width: 22%; }}
.c-num {{ white-space: nowrap; color: var(--accent); font-variant-numeric: tabular-nums; }}
.cols {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 36px; }}
ol.list, ul.list {{ margin: 16px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 10px; }}
.list li {{ background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; display: flex; flex-direction: column; gap: 2px; font-size: 14px; }}
.list li b {{ font-size: 15px; }}
.list li span {{ color: var(--muted); }}
ol.list {{ counter-reset: q; }}
ol.list li {{ counter-increment: q; }}
ol.list li b::before {{ content: counter(q) "　"; color: var(--accent); }}
.sources {{ margin: 12px 0 0; padding-left: 1.2em; font-size: 13.5px; color: var(--muted); }}
.sources a {{ color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }}
.sources a:hover, .sources a:focus-visible {{ text-decoration: underline; }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }}
.foot {{ margin-top: 40px; font-size: 13px; color: var(--faint); }}
@media (max-width: 860px) {{
  .flow {{ grid-template-columns: minmax(0, 1fr); }}
  .arrow {{ transform: rotate(90deg); justify-self: start; margin-left: 12px; }}
  .cols {{ grid-template-columns: minmax(0, 1fr); gap: 0; }}
  h1 {{ font-size: 28px; }}
}}
@media (max-width: 440px) {{ .screens {{ grid-template-columns: minmax(0, 1fr); }} }}
</style>
<div class="wrap">
<header>
<div class="eyebrow">有据 · 设计稿 · 2026-10-17</div>
<h1>AI 助手对话版</h1>
<p class="lede">把政策解读、条款对照合进一个对话页：打字、拍图片都能问；回答先从有据收录的官方通知里找，每句结论带出处，找不到就说找不到。联网只查政府网站，并标明未经人工核对。</p>
<div class="pills"><span class="pill"><b>只做设计</b>，给创始人定方向</span><span class="pill">开发排在首发上线、网信办上线登记问清之后</span>
<span class="pill">原句和网页摘录均取自政府网站原文（10/17 核对）</span><span class="pill">企业情况、对话时间是示例；[ ] 是待定项</span></div>
</header>

<section>
<h2>入口和流程</h2>
<p class="sub">只有一个对话页。从哪里进来，决定对话开头带不带一份通知；原来的两项 AI 能力变成对话里的卡片，用户不用再理解「工具页」「任务」。</p>
<div class="flow">
<div><div class="box">首页「AI 助手」<small>不带通知，空白对话</small></div><div class="box">通知详情「AI 解读」<small>带上这份通知</small></div></div>
<div class="arrow" aria-hidden="true">→</div>
<div class="box">AI 助手对话页<small>顶部固定「AI 生成」；每份通知一条对话，记录保存 30 天</small></div>
<div class="arrow" aria-hidden="true">→</div>
<div class="box">在对话里出现的卡片
<div class="caps"><span>划重点</span><span>要准备哪些材料</span><span>和我的企业对一下</span><span>自由提问</span><span>拍图片</span><span>联网查政府网站</span></div>
<small style="margin-top: 6px">「要准备哪些材料」用同一份划重点结果，不另扣次数</small></div>
</div>
</section>

<section>
<h2>9 屏画面</h2>
<p class="sub">画面宽 390，按小程序真实尺寸画；长的画面是整条对话的长截图，输入框在最下面。图标只用小程序本地图标集里已有的。</p>
<div class="screens">{"".join(shots)}</div>
</section>

<section>
<h2>合规元素放在哪</h2>
<div class="table-wrap"><table><thead><tr><th>要求</th><th>依据</th><th>放在哪</th><th>屏</th></tr></thead><tbody>{comp_rows}</tbody></table></div>
</section>

<div class="cols">
<section><h2>技术前提</h2><ul class="list">{tech}</ul></section>
<section><h2>请你拍板（5 个）</h2><ol class="list">{qs}</ol></section>
</div>

<section>
<h2>画面里用到的政府网站原文</h2>
<ul class="sources">{src}</ul>
</section>
<p class="foot">审阅：Codex（设计主审，看图）、agy（第一次用的经办人和审核人员视角）、Grok（申报顾问视角）各挑一轮，已按核实结果改稿，逐条处理见说明文档。<br>说明文档：docs/设计/AI助手_对话版设计稿_2026-10-17.md · 生成脚本：docs/设计/AI助手对话版/生成脚本/chat.py</p>
</div>
<script>
(function () {{
  // 手机宽度下把 392 宽的画面整体缩小，页面不横向滚动
  function fit() {{
    document.querySelectorAll('.frame').forEach(function (f) {{
      var w = f.parentElement.clientWidth;
      f.style.zoom = w && w < 392 ? String(w / 392) : '';
    }});
  }}
  window.addEventListener('resize', fit);
  fit();
}})();
</script>
"""


def static_page(sid, fn):
    """单屏预览：本机无头 Chrome 截图用；页面底色用品红，量高度时好找画面边界。"""
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{FONTS_LINK}'
            f'<style>body{{margin:0;background:#FF00FF}}</style></head><body>{fn()}</body></html>')


if __name__ == "__main__":
    out_board = Path(sys.argv[1])
    st_dir = Path(sys.argv[2])
    st_dir.mkdir(parents=True, exist_ok=True)
    out_board.parent.mkdir(parents=True, exist_ok=True)
    out_board.write_text(board(), encoding="utf-8")
    for sid, n, title, fn, intent, chips in SCREENS:
        (st_dir / f"{sid}.html").write_text(static_page(sid, fn), encoding="utf-8")
        print(sid, title)
