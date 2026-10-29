# -*- coding: utf-8 -*-
"""「有据」AI 升级两种做法的效果图（2026-10-29）。

创始人 10/28 想把小程序里的 AI 按 10/17 的对话版（#278）全面升级，并且要更新到最新的方案、让用户通过 AI 直接拿到结果；
10/29 说先看效果图再定。这里按同样 6 件事各画一屏：
- A「任务和结果首页」（Claude、Codex 推荐）：AI 首页给一个找通知的输入框，先给能直接看的结果和要办的事；
  长结果在各自的页面里看，对话只用来接着问一份通知。
- B「全做成对话页」：AI 首页就是对话页，所有事都在一条对话里做（10/17 对话版的样子，按现在定的规矩改过）。
两种做法守同样的规矩：只摘原句、程序逐字核对；不判断能不能申报、不代写；企业信息不交给模型；查询、正文、附件、结果都在小程序里；
AI 生成的标出来，页底公示模型和备案号（D-098）；图片第二期、联网第三期（D-078）。
部件、配色、图标沿用 ai_board.py（原型 v2 的 ds.py，图标只用 tools/icons/icons.txt 里有的）；数据是 data_1022.json
（10/22 线上真实收录），原句都过 verbatim() 逐字核对；解读摘哪几句、分到哪一类是示意，以实际结果为准；[ ] 是还没有的内容。

用法：python3 ai_v2.py <看板 HTML> <单屏 HTML 目录>，再 ./render.sh <单屏 HTML 目录> <效果图目录> 屏号...
"""

import sys
from pathlib import Path

import ai_board as ab
from ai_board import (BLUE, BLUE_G1, BLUE_G2, BLUE_TINT, CARD, DATA, DOC, FILL, FIND, FONTS_LINK, LINE, LINE2, Q_T1, Q_T2, Q_W1,
                      Q_W2, T1, T2, T3, adv_card, btn_primary, card, card_head, chip, detail_bar, detail_body, detail_due,
                      detail_head, detail_points, doc_row, first_screen, icon, nav, notice_bar, para, phone, quote, record, result_strip,
                      section_title, tabbar, verbatim)
from ds import capsule  # noqa: E402  （ai_board 导入时已把原型 v2 的目录加进 sys.path）

# D-098：页底公示腾讯混元助手大模型和生成式人工智能服务备案号（ai_board 那一版画的时候还是占位）
DISC_MODEL = "模型：腾讯混元助手大模型"
DISC_FILING = "备案号：Guangdong-TencentHunyuan-20230901"
ab.DISCLOSURE = f"{DISC_MODEL}｜{DISC_FILING}"
LINES = ab.BODY.split("\n")


def line(i):
    """收录正文的第 i 行（这份通知一行就是一句或一条），照样过 verbatim()。"""
    return verbatim(LINES[i])


# ---------- 示意的 AI 解读结果（湖北科技厅那份，原句按行取自正文） ----------
OBJ = [line(10), line(12)]                       # 二、支持对象：（一）企业自主研发项目（第 9 行）、（二）企业委托研发项目（第 11 行）
COND_SELF = [line(i) for i in (16, 17, 18, 19)]  # 三、（一）1．企业自主研发项目（第 15 行是这一组的小标题）
COND_ENTR = [line(i) for i in range(21, 27)]     # 三、（一）2．企业委托研发项目（第 20 行）
COND_LIMIT = [line(28), line(29)]                # 三、（二）限项要求
MAT_SELF = [line(i) for i in range(33, 39)]      # 四、（一）企业自主研发项目（第 32 行）
MAT_ENTR = [line(i) for i in range(40, 46)]      # 四、（二）企业委托研发项目（第 39 行）
TIME = [Q_T1, Q_T2]
WAY = [Q_W1, Q_W2]
# 每类几句；材料清单另有 1 句和申报方式相同（无需提交纸质材料），所以按类加起来比总数多 1
COUNTS = [("申报对象", len(OBJ)), ("申报条件", len(COND_SELF) + len(COND_ENTR) + len(COND_LIMIT)), ("申报时间", len(TIME)),
          ("申报方式", len(WAY)), ("材料清单", len(MAT_SELF) + len(MAT_ENTR) + 1)]
TOTAL = sum(n for _, n in COUNTS) - 1
MISSING = "支持方式"
DUE_SELF, DUE_RECO = DOC["deadlines"]            # 企业申报截止（机器摘录，剩 29 天）；推荐或审核截止（还没核对）
COMPILE = [d for d in FIND if d["ai"]["interpret"]]  # 找通知那 5 份里能解读的 4 份（重庆那份没有完整正文）
FIND_SHORT = ab.FIND_Q[1:]                       # "近 30 天的 5G 工厂申报"：记录里不再重复"找"字
FILE_NAME = "5G工厂名录申报_4份对比表.xlsx"


# ---------- 小部件 ----------
def link(text, size=13, color=BLUE):
    return f'<span style="font-size: {size}px; color: {color}; white-space: nowrap">{text}</span>'


def pill(text, size=13, h=32):
    """浅蓝底的快捷按钮（对话页、卡片里点了直接开始的那种）。"""
    return (f'<span style="flex-shrink: 0; height: {h}px; padding: 0 12px; display: inline-flex; align-items: center; border-radius: {h // 2}px; '
            f'background: {BLUE_TINT}; color: {BLUE}; font-size: {size}px; font-weight: 600; white-space: nowrap">{text}</span>')


def outline_btn(text, size=14, h=38):
    return (f'<span style="min-width: 0; height: {h}px; padding: 0 6px; display: flex; align-items: center; justify-content: center; border-radius: {h // 2}px; '
            f'border: 1px solid {LINE2}; background: {CARD}; color: {T1}; font-size: {size}px; white-space: nowrap">{text}</span>')


def small_btn(text, primary=False):
    st = f"background: {BLUE}; color: #FFFFFF; border: 1px solid {BLUE}" if primary else f"background: {CARD}; color: {T1}; border: 1px solid {LINE2}"
    return (f'<span style="flex: 1; min-width: 0; height: 38px; display: flex; align-items: center; justify-content: center; border-radius: 19px; '
            f'{st}; font-size: 14px; font-weight: 500; white-space: nowrap">{text}</span>')


def checkbox(on):
    if on:
        return (f'<span aria-hidden="true" style="flex-shrink: 0; margin-top: 3px; width: 20px; height: 20px; border-radius: 5px; background: {BLUE}; '
                f'display: flex; align-items: center; justify-content: center">{icon("check", 15, "#FFFFFF")}</span>')
    return (f'<span aria-hidden="true" style="flex-shrink: 0; margin-top: 3px; width: 20px; height: 20px; box-sizing: border-box; border-radius: 5px; '
            f'border: 1.5px solid #C9CDD4; background: {CARD}"></span>')


def seg(options, selected, track=FILL):
    """分段选择：几个选项一行等宽，选中的白底。直接放在页面底色上时 track 用深一点的灰，不然看不出边。"""
    items = []
    for o in options:
        st = (f"background: {CARD}; color: {T1}; font-weight: 600; box-shadow: 0 1px 3px rgba(29,33,41,0.10)" if o == selected
              else f"color: {T2}")
        items.append(f'<span style="min-width: 0; height: 34px; padding: 0 6px; display: flex; align-items: center; justify-content: center; '
                     f'border-radius: 8px; font-size: 13.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; {st}">{o}</span>')
    return (f'<div style="padding: 3px; border-radius: 10px; background: {track}; display: grid; '
            f'grid-template-columns: repeat({len(options)}, minmax(0, 1fr)); gap: 3px">{"".join(items)}</div>')


def bar(items, main):
    """页面底栏（同通知详情底栏的样子）：左边几个图标按钮，右边一个主按钮。"""
    its = "".join(f'<span style="width: 56px; height: 48px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1px; '
                  f'font-size: 11px; color: {T2}">{icon(i, 22, T2)}<span>{t}</span></span>' for t, i in items)
    return (f'<div style="flex-shrink: 0; box-sizing: border-box; padding: 8px 16px 28px; background: {CARD}; border-top: 1px solid {LINE}; '
            f'display: flex; align-items: center; gap: 4px">{its}'
            f'<span style="margin-left: auto; height: 44px; padding: 0 22px; display: inline-flex; align-items: center; background: {BLUE}; '
            f'color: #FFFFFF; border-radius: 22px; font-size: 15px; font-weight: 500; white-space: nowrap">{main}</span></div>')


def button_bar(note, text):
    """页面底部一整行主按钮，上面一行说明。"""
    return (f'<div style="flex-shrink: 0; padding: 10px 16px 28px; background: {CARD}; border-top: 1px solid {LINE}">'
            f'<div style="font-size: 12px; line-height: 1.6; color: {T2}">{note}</div>{btn_primary(text, 8)}</div>')


def mono_tag(text):
    """"只查收录"这类灰标签（不是 AI 生成的结果）。"""
    return (f'<span style="flex-shrink: 0; padding: 0 6px; line-height: 20px; border-radius: 4px; background: {FILL}; color: {T2}; font-size: 12px; '
            f'font-weight: 600; white-space: nowrap">{text}</span>')


def excerpt_tag():
    return (f'<span style="align-self: flex-start; padding: 2px 6px; border-radius: 4px; background: {FILL}; color: {T2}; font-size: 11px; '
            f'font-weight: 600">机器摘录·未经人工核对</span>')


def check_links(mt=6):
    """原句下面的"看原文 ›""这句摘错了"。"""
    return (f'<div style="margin-top: {mt}px; display: flex; align-items: center; font-size: 12.5px">{link("看原文 ›", 12.5)}'
            f'<span style="margin-left: auto; display: inline-flex; align-items: center; gap: 3px; color: {T2}">{icon("flag", 13, T2)}这句摘错了</span></div>')


def due_block():
    """AI 解读结果里的截止（D-061，D-078 第五节）：分清是谁的截止。机器摘录的写日期、剩几天，标"机器摘录·未经人工核对"并附原句；
    日期还没摘出来的，把解读摘到的那句列出来。每句都能看原文、纠错。"""
    d = DUE_SELF
    first = (f'<div style="display: flex; flex-direction: column; gap: 6px">'
             f'<div style="display: flex; align-items: flex-end; justify-content: space-between; gap: 8px"><div style="min-width: 0">'
             f'<div style="font-size: 12px; color: {T2}">{d["kind"]}</div>'
             f'<div style="margin-top: 2px; display: flex; align-items: baseline; gap: 8px"><span style="font-size: 20px; font-weight: 700; line-height: 1.3">'
             f'{ab.month_day(d["date"])}</span><span style="font-size: 13px; color: {T2}">{ab.week_of(d["date"])} {d["time"]}</span></div></div>'
             f'<span style="flex-shrink: 0; height: 26px; padding: 0 10px; display: inline-flex; align-items: center; border-radius: 13px; background: {BLUE_TINT}; '
             f'color: {BLUE}; font-size: 13px; font-weight: 600">剩 {d["remainingDays"]} 天</span></div>{excerpt_tag()}'
             f'<div style="padding: 6px 8px; border-radius: 5px; background: {FILL}; font-size: 13px; line-height: 1.7; color: {T2}">原文：{d["quote"]}</div></div>'
             + check_links(4))
    second = (f'<div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid {LINE}"><div style="font-size: 12px; color: {T2}">{DUE_RECO["kind"]}</div>'
              f'<div style="margin-top: 6px; display: flex; flex-direction: column; gap: 6px">{excerpt_tag()}'
              f'<div style="padding: 6px 8px; border-radius: 5px; background: {FILL}; font-size: 13px; line-height: 1.7; color: {T2}">原文：{Q_T2}</div></div>'
              + check_links(4) + "</div>")
    return first + second


def src_label(*path):
    """这几句在原文哪一节：程序按正文里的小标题认，不用模型。写"位置："、不加灰底，免得当成原句（独立 Claude 第 1 轮）。"""
    return (f'<div style="margin-top: 14px; font-size: 12px; line-height: 1.6; color: {T2}">'
            f'<span style="font-weight: 600">位置：</span>正文 › {" › ".join(path)}</div>')


def q_item(text, first=False):
    b = "" if first else f"border-top: 1px solid {LINE};"
    return (f'<div style="margin-top: 10px; padding-top: 10px; {b}"><div style="font-size: 15px; line-height: 1.75">{text}</div>'
            f'<div style="margin-top: 6px; display: flex; align-items: center; gap: 16px; font-size: 13px">{link("看原文 ›")}'
            f'<span style="margin-left: auto; display: inline-flex; align-items: center; gap: 3px; color: {T2}">{icon("flag", 13, T2)}这句摘错了</span></div></div>')


def count_chips(selected=0, h=30, size=13):
    """按类的胶囊：selected=None 时都不选中（对话卡片里点了打开整页、跳到那一类）。"""
    out = []
    for i, (n, c) in enumerate(COUNTS):
        st = f"background: {BLUE}; color: #FFFFFF; font-weight: 600" if i == selected else f"background: {FILL}; color: {T1}"
        out.append(f'<span style="height: {h}px; padding: 0 11px; display: inline-flex; align-items: center; gap: 4px; border-radius: {h // 2}px; '
                   f'font-size: {size}px; white-space: nowrap; {st}">{n}<span style="opacity: 0.72">{c}</span></span>')
    return "".join(out)


def answer(kind, body):
    """回答卡片（同 ai_board.answer）：页底的模型和备案号拆开写，备案号不从中间断开，「投诉举报」不挤成两行。"""
    if kind != "ai":
        return ab.answer(kind, body)
    t = (f'<span style="padding: 0 6px; line-height: 20px; border-radius: 4px; background: {BLUE}; color: #FFFFFF; font-size: 12px; '
         f'font-weight: 600">AI 生成</span>')
    f = (f'<div style="margin-top: 12px; padding-top: 10px; border-top: 1px solid {LINE}; font-size: 11.5px; line-height: 1.7; color: {T2}">'
         f'<div>不判断能不能申报，以原文和主管部门解释为准。</div><div>{DISC_MODEL}</div>'
         f'<div style="display: flex; align-items: flex-start; gap: 12px"><span style="min-width: 0; overflow-wrap: anywhere">{DISC_FILING}</span>'
         f'<span style="margin-left: auto; flex-shrink: 0; color: {T2}">投诉举报</span></div></div>')
    return (f'<div style="margin: 12px 12px 0; padding: 12px 14px 12px; background: {CARD}; border-radius: 12px">'
            f'<div style="display: flex">{t}</div><div style="margin-top: 8px">{body}</div>{f}</div>')


def quote_plain(text):
    """不加编号的原句（原句自己带"1．2．"这类编号时用，免得编号重复）。"""
    return (f'<div style="margin-top: 10px"><div style="padding: 8px 10px; background: {ab.QUOTE_BG}; border-radius: 8px; font-size: 14.5px; line-height: 1.75">{text}</div>'
            f'<div style="margin-top: 6px; display: flex; align-items: center; gap: 14px; font-size: 12.5px"><span style="color: {T3}">出处：正文</span>'
            f'{link("看原文 ›", 12.5)}<span style="margin-left: auto; display: inline-flex; align-items: center; gap: 3px; color: {T2}">'
            f'{icon("flag", 13, T2)}纠错</span></div></div>')


def ai_foot():
    return (f'<div style="padding: 18px 12px 0; text-align: center; font-size: 12px; line-height: 1.8; color: {T3}"><div>{DISC_MODEL}</div><div>{DISC_FILING}</div></div>'
            f'<div style="padding: 6px 12px 0; display: flex; justify-content: center; gap: 24px; font-size: 13px; color: {BLUE}">'
            f'<span>使用帮助 ›</span><span>联系客服 ›</span><span>投诉举报 ›</span></div>')


def doc_with_due(d=DOC):
    """列表行要的 due 字段：详情页的第一种截止（企业申报截止）。"""
    return dict(d, due=d["deadlines"][0])


# ---------- A：任务和结果首页 ----------
A_HERO = "截止、条件、材料直接从政府原文摘给你，几份还能整理成一张表；每句都能回原文核对，不判断能不能申报。"
A_EXAMPLES = ("近 30 天的 5G 工厂申报", "宁夏近 7 天的申报")
TASKS = [("doc", "AI 解读", "一份通知的<br>原句分好类"), ("check", "准备材料", "材料原句<br>列成清单"), ("copy", "条款对照", "条件和企业<br>情况并排"),
         ("download", "整理成文件", "几份通知出成<br>Excel、Word"), ("info", "问这份通知", "从原文摘<br>相关的句子"), ("list", "查名单", "企业在不在<br>公开名单里")]


def a_head():
    """AI 首页页头：品牌蓝底，标题、出处说明、一句话说明；下面是找通知的输入框（和首页搜索框一个样子，按规则找，不用 AI）。"""
    ex = "".join(f'<span style="height: 28px; padding: 0 11px; display: inline-flex; align-items: center; border-radius: 14px; '
                 f'background: rgba(255,255,255,0.16); color: #FFFFFF; font-size: 12.5px; white-space: nowrap">{t}</span>' for t in A_EXAMPLES)
    box = (f'<div style="margin-top: 12px; height: 44px; padding: 0 14px; box-sizing: border-box; display: flex; align-items: center; gap: 8px; '
           f'border-radius: 22px; background: #FFFFFF; color: {T3}; font-size: 14.5px">{icon("search", 18, T3)}'
           f'<span style="min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">找通知：说要找什么，或输入标题、文号</span></div>')
    return (f'<div style="flex-shrink: 0; background: linear-gradient(180deg, {BLUE_G1} 0%, {BLUE_G2} 100%); color: #FFFFFF; padding: 0 16px 36px">'
            f'<div style="height: 48px; position: relative; display: flex; align-items: center; justify-content: center">'
            f'<span style="font-size: 17px; font-weight: 600">AI</span>{capsule().replace("right: 10px", "right: -6px")}</div>'
            f'{notice_bar(on_blue=True, nextv=True)}'
            f'<div style="margin-top: 8px; font-size: 13.5px; line-height: 1.7; color: rgba(255,255,255,0.92)">{A_HERO}</div>{box}'
            f'<div style="margin-top: 10px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px">'
            f'<span style="font-size: 12.5px; color: rgba(255,255,255,0.82)">试试</span>{ex}</div>'
            f'<div style="margin-top: 8px; font-size: 12px; color: rgba(255,255,255,0.82)">找通知只查收录的通知，不用 AI、不算次数</div></div>')


def a_continue():
    acts = (f'<div style="padding: 0 0 14px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px">{chip("AI 解读")}'
            f'{link("准备材料 ›")}{link("问这份通知 ›")}</div>')
    return card(card_head("刚才看的通知") + doc_row(doc_with_due(), None, last=True, role=True, inapp=True) + acts)


def ready_row(ic, title, desc, right="", last=False):
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    r = f'<span style="flex-shrink: 0; font-size: 13px; color: {T2}; white-space: nowrap">{right}</span>' if right else ""
    return (f'<div style="padding: 12px 0; display: flex; align-items: center; gap: 12px; {b}">'
            f'<span style="flex-shrink: 0; width: 36px; height: 36px; border-radius: 10px; background: {BLUE_TINT}; display: flex; '
            f'align-items: center; justify-content: center">{icon(ic, 20, BLUE)}</span>'
            f'<div style="flex: 1; min-width: 0"><div style="font-size: 15px; font-weight: 600">{title}</div>'
            f'<div style="margin-top: 2px; font-size: 12.5px; line-height: 1.6; color: {T2}">{desc}</div></div>{r}{icon("chev", 16, T3)}</div>')


def a_ready():
    # 三行都是现成的页面，不用 AI：近 7 天能解读的申报通知（现在 AI 首页那张卡片）、关注的新文件、截止
    rows = (ready_row("doc", "近 7 天新的申报通知", "都能 AI 解读，按发布日期从新到旧", f'{DATA["recent"]["total"]} 份')
            + ready_row("bell", "关注的新文件", "湖北 · 科技计划与研发<br>上次看过以后新收录的", "[N] 份")
            + ready_row("calendar", "截止", "按剩几天排；机器摘录的截止附原句", last=True))
    return card(card_head("直接能看的") + rows)


def task_tile(ic, name, desc):
    return (f'<div style="min-width: 0; padding: 12px 10px 12px 12px; border-radius: 10px; background: {FILL}; display: flex; flex-direction: column; gap: 6px">'
            f'<span style="width: 30px; height: 30px; border-radius: 8px; background: {CARD}; display: flex; align-items: center; justify-content: center">'
            f'{icon(ic, 18, BLUE)}</span><span style="font-size: 14.5px; font-weight: 600; line-height: 1.4">{name}</span>'
            f'<span style="font-size: 12px; line-height: 1.5; color: {T2}">{desc}</span></div>')


def a_tasks():
    tiles = "".join(task_tile(*t) for t in TASKS)
    return card(card_head("要办的事")
                + f'<div style="padding: 12px 0 4px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px">{tiles}</div>'
                + f'<div style="padding: 6px 0 14px; font-size: 12px; line-height: 1.6; color: {T3}">AI 解读、条款对照、问这份通知今天各还剩 5 次。'
                  f'<br>准备材料不另算次数；找通知、查名单不用 AI。</div>')


def a_records():
    recs = (record(f"AI 解读 · {DOC['title']}", "[日期] · 已完成")
            + record('整理成文件 · 5G 工厂名录申报 4 份<span style="white-space: nowrap">（Excel 对比表）</span>', "[日期] · 已生成，在「文件」里")
            + record(f"找通知 · {FIND_SHORT}", "[日期] · 找到 5 份", last=True))
    return card(card_head("我的 AI 记录", "全部") + recs)


def a_home():
    top = a_head() + ab.ai_strip() + a_continue() + a_ready() + a_tasks() + a_records() + ai_foot() + '<div style="height: 20px"></div>'
    return phone(top, tabbar())


def a_card():
    """详情页"要点"下面的卡片（位置不动，D-075）：主按钮开始解读，下面三个直达，各进各的页面。"""
    right = f'<span style="font-size: 12.5px; font-weight: 400; color: {T2}">今天还剩 <b style="font-weight: 700; color: {BLUE}">5</b> 次</span>'
    three = (f'<div style="margin-top: 10px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px">'
             + "".join(outline_btn(t, 13.5) for t in ("准备材料", "条款对照", "问这份通知")) + "</div>")
    return card(section_title("AI 解读", right)
                + f'<div style="margin-top: 4px; font-size: 14px; line-height: 1.7">约半分钟把申报对象、条件、时间、材料的原文原句摘出来分好类，每句都能回原文核对。</div>'
                + btn_primary("开始解读") + three
                + f'<div style="margin-top: 8px; padding-bottom: 16px; text-align: center; font-size: 12px; color: {T3}">准备材料用解读的结果，不另算次数</div>')


def detail_with(ai_part, h=1290):
    top = nav("文件详情") + notice_bar(nextv=True) + detail_head(True) + detail_due() + detail_points() + ai_part + detail_body(True)
    return first_screen(top, detail_bar(True), h=h)


def a_detail():
    return detail_with(a_card())


def a_summary():
    title = f'<div style="font-size: 17px; font-weight: 600; line-height: 1.6">{DOC["title"]}</div>'
    meta = (f'<div style="margin-top: 4px; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; font-size: 12.5px; color: {T2}">'
            f'<span>{DOC["issuer"]} · {DOC["date"]} 发布</span>{link("看原文 ›", 12.5)}</div>')
    facts = (f'<div style="margin-top: 12px; padding-top: 12px; border-top: 1px solid {LINE}; display: grid; grid-template-columns: auto minmax(0, 1fr); '
             f'gap: 6px 12px; font-size: 13px; line-height: 1.6"><span style="color: {T3}">读了</span><span>正文（这份通知没有附件）</span>'
             f'<span style="color: {T3}">没摘到</span><span>{MISSING}：可能原文没写，请看原文</span></div>')
    return card(f'<div style="padding: 16px 0">{title}{meta}<div style="margin-top: 12px; padding-top: 12px; border-top: 1px solid {LINE}">'
                f'{due_block()}</div>{facts}</div>')


def a_jump():
    return card(f'<div style="padding: 14px 0"><div style="font-size: 13px; color: {T2}">共摘出 {TOTAL} 句，按类看（有 1 句同时属于两类）</div>'
                f'<div style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px">{count_chips()}</div></div>')


def sec_title(name, n):
    return f'<div style="font-size: 17px; font-weight: 600">{name} <span style="font-size: 13px; font-weight: 400; color: {T2}">{n} 句</span></div>'


def result_sections():
    obj = adv_card(sec_title("申报对象", len(OBJ))
                   + src_label(line(8), line(9)) + q_item(OBJ[0], True)
                   + src_label(line(8), line(11)) + q_item(OBJ[1], True))
    cond = adv_card(sec_title("申报条件", COUNTS[1][1])
                    + src_label(line(13), line(14), line(15)) + "".join(q_item(t, i == 0) for i, t in enumerate(COND_SELF))
                    + src_label(line(13), line(14), line(20)) + "".join(q_item(t, i == 0) for i, t in enumerate(COND_ENTR[:2])))
    return obj + cond


def a_result():
    top = nav("AI 解读") + result_strip() + a_summary() + a_jump() + result_sections()
    return first_screen(top, bar([("准备材料", "check"), ("加入整理", "download")], "接着问这份通知"), h=1880)


def cond_chip(label, value, close=False):
    ic = icon("close", 14, T2) if close else icon("down", 14, T2)
    return (f'<span style="height: 30px; padding: 0 8px 0 11px; display: inline-flex; align-items: center; gap: 3px; border-radius: 15px; '
            f'border: 1px solid {LINE2}; background: {CARD}; font-size: 13px; white-space: nowrap"><span style="color: {T2}">{label}</span>{value}{ic}</span>')


def a_find():
    search = (f'<div style="padding: 10px 12px 0"><div style="height: 40px; padding: 0 12px; box-sizing: border-box; display: flex; align-items: center; gap: 8px; '
              f'border-radius: 20px; background: {CARD}; border: 1px solid {LINE2}">{icon("search", 18, T3)}'
              f'<span style="flex: 1; min-width: 0; font-size: 14.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">{ab.FIND_Q}</span>'
              f'{icon("close", 16, T3)}</div></div>')
    add = (f'<span style="height: 30px; padding: 0 11px; display: inline-flex; align-items: center; gap: 2px; border-radius: 15px; border: 1px dashed {LINE2}; '
           f'color: {BLUE}; font-size: 13px; white-space: nowrap">{icon("plus", 14, BLUE)}地区</span>')
    conds = card(f'<div style="padding: 14px 0"><div style="display: flex; align-items: center; justify-content: space-between; gap: 8px">'
                 f'<span style="font-size: 14px; font-weight: 600">按这些条件找的</span>{mono_tag("只查收录")}</div>'
                 f'<div style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px">{cond_chip("类型：", "申报通知")}{cond_chip("发布：", "近 30 天")}'
                 f'{cond_chip("标题里有：", "5G工厂", close=True)}{add}</div>'
                 f'<div style="margin-top: 8px; font-size: 12px; line-height: 1.6; color: {T3}">点条件就能改；只查收录的通知，不用 AI、不算次数</div></div>')
    rows = "".join(doc_row(d, "AI 解读" if d["ai"]["interpret"] else None, last=i == len(FIND) - 1, role=True, inapp=True) for i, d in enumerate(FIND))
    res = card(f'<div style="padding-top: 12px; font-size: 13px; color: {T2}">找到 {DATA["find"]["total"]} 份，按发布日期从新到旧</div>{rows}')
    note = (f'<div style="padding: 10px 16px 0; font-size: 12.5px; line-height: 1.6; color: {T2}">重庆那份还没有完整正文，不能 AI 解读，也整理不了。点标题能看到原因。</div>')
    bottom = button_bar(f"能 AI 解读的 {len(COMPILE)} 份可以一起整理，最多 10 份", f"把这 {len(COMPILE)} 份整理成对比表")
    return phone(nav("找通知") + notice_bar(nextv=True) + search + conds + res + note + '<div style="height: 16px"></div>', bottom)


def check_row(text, on, first=False):
    b = "" if first else f"border-top: 1px solid {LINE};"
    return (f'<div style="padding: 12px 0; display: flex; gap: 10px; {b}">{checkbox(on)}'
            f'<div style="flex: 1; min-width: 0"><div style="font-size: 14.5px; line-height: 1.75">{text}</div>'
            f'<div style="margin-top: 4px; display: flex; align-items: center; gap: 16px; font-size: 12.5px">{link("看原文 ›", 12.5)}'
            f'<span style="margin-left: auto; display: inline-flex; align-items: center; gap: 3px; color: {T2}">{icon("flag", 13, T2)}这句摘错了</span></div></div></div>')


def a_materials():
    done = 2
    head = card(f'<div style="padding: 14px 0"><div style="font-size: 15px; font-weight: 600; line-height: 1.6">{DOC["title"]}</div>'
                f'<div style="margin-top: 6px; font-size: 12.5px; line-height: 1.6; color: {T2}">从 AI 解读的「材料清单」来，不另算次数。<br>勾选只存在这台手机上，不上传。</div></div>')
    prog = (f'<div style="margin-top: 12px; display: flex; align-items: center; gap: 10px"><span style="font-size: 13px; font-weight: 600; white-space: nowrap">'
            f'已备 {done} / {len(MAT_SELF)} 项</span><span style="flex: 1; height: 6px; border-radius: 3px; background: {FILL}; overflow: hidden">'
            f'<span style="display: block; width: {done * 100 // len(MAT_SELF)}%; height: 100%; background: {BLUE}"></span></span></div>')
    rows = "".join(check_row(t, i < done, i == 0) for i, t in enumerate(MAT_SELF))
    shared = (f'<div style="margin-top: 4px; padding: 12px 0 14px; border-top: 1px solid {LINE}"><div style="font-size: 12.5px; color: {T2}">'
              f'另有 1 句和「申报方式」相同：</div><div style="margin-top: 6px; padding: 8px 10px; border-radius: 8px; background: {ab.QUOTE_BG}; '
              f'font-size: 14px; line-height: 1.75">{Q_W2}</div></div>')
    choose = (f'<div style="padding-top: 14px; font-size: 13px; color: {T2}">两类项目要的材料不一样，先选一类：</div>'
              f'<div style="margin-top: 8px">{seg(["企业自主研发项目", "企业委托研发项目"], "企业自主研发项目")}</div>')
    lst = card(choose + src_label(line(31), line(32)) + prog + rows + shared)
    note = (f'<div style="padding: 10px 16px 0; font-size: 12.5px; line-height: 1.6; color: {T2}">这份通知没有附件，材料都写在正文里；以原文和主管部门的解释为准。</div>')
    top = nav("准备材料") + result_strip() + head + lst + note + '<div style="height: 16px"></div>'
    return phone(top, bar([("加入整理", "download")], "接着问这份通知"))


def compile_row(d, last=False):
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    return (f'<div style="padding: 12px 0; display: flex; align-items: flex-start; gap: 10px; {b}"><div style="flex: 1; min-width: 0">'
            f'<div style="font-size: 12px; color: {T2}">{d["region"]}</div>'
            f'<div style="margin-top: 2px; font-size: 14.5px; font-weight: 600; line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; '
            f'-webkit-box-orient: vertical; overflow: hidden">{d["title"]}</div>'
            f'<div style="margin-top: 2px; font-size: 12px; line-height: 1.6; color: {T2}">{d["issuer"]} · <span style="white-space: nowrap">{d["date"]}</span></div></div>'
            f'<span style="flex-shrink: 0; margin-top: 2px">{icon("close", 16, T3)}</span></div>')


def date_snippet(q, reach=30):
    """长句只取写日期的那一小句：从日期前面最近的标点后开始，到日期后面最近的逗号、句号、分号为止，前后加"…"
    （截取的是原句里连着的一段，文件里放整句）。"""
    import re
    if not q:
        return q
    m = re.search(r"\d{1,2}月\d{1,2}日", q)
    if not m:
        return q
    a = max((q.rfind(c, max(0, m.start() - reach), m.start()) for c in "，。；）)"), default=-1)
    a = a + 1 if a >= 0 else max(0, m.start() - 10)
    ends = [i for i in (q.find(c, m.end()) for c in "，。；") if 0 <= i <= m.end() + reach]
    b = min(ends) + 1 if ends else min(len(q), m.end() + reach)
    return ("…" if a > 0 else "") + q[a:b] + ("…" if b < len(q) else "")


def preview_table():
    """Excel 对比表的前三列预览：申报时间一列放的是收录里摘到的截止原句；黑龙江那份当它还没解读，写"等解读完才有"。"""
    def cell(text, head=False, clamp=3):
        if head:
            return (f'<div style="min-width: 0; padding: 7px 8px; font-size: 11.5px; font-weight: 600; line-height: 1.55; color: {T2}; '
                    f'background: {FILL}">{text}</div>')
        return (f'<div style="min-width: 0; padding: 7px 8px; border-top: 1px solid {LINE}"><div style="font-size: 12px; line-height: 1.55; '
                f'color: {T1}; display: -webkit-box; -webkit-line-clamp: {clamp}; -webkit-box-orient: vertical; overflow: hidden">{text}</div></div>')
    cols = "grid-template-columns: 52px minmax(0, 1.05fr) minmax(0, 1.45fr)"
    rows = [cell("地区", True) + cell("通知", True) + cell("申报时间（原句）", True)]
    for d in COMPILE:
        q = date_snippet((d.get("due") or {}).get("quote"))
        rows.append(cell(d["province"]) + cell(d["title"]) + (cell(q) if q else cell(f'<span style="color: {T3}">等解读完才有</span>')))
    return (f'<div style="margin-top: 10px; border: 1px solid {LINE2}; border-radius: 8px; overflow: hidden; display: grid; {cols}">'
            + "".join(rows) + "</div>")


def a_compile():
    sel = card(card_head(f"选了 {len(COMPILE)} 份（最多 10 份）", "+ 再加")
               + "".join(compile_row(d, i == len(COMPILE) - 1) for i, d in enumerate(COMPILE)))
    picks = [("申报对象", True), ("申报条件", True), ("申报时间", True), ("材料清单", True), ("申报方式", False), ("支持方式", False)]
    grid = "".join(f'<div style="display: flex; align-items: flex-start; gap: 8px; font-size: 14px; line-height: 1.6">{checkbox(on)}<span style="margin-top: 2px">{n}</span></div>'
                   for n, on in picks)
    what = card(f'<div style="padding: 14px 0"><div style="font-size: 15px; font-weight: 600">放哪些内容</div>'
                f'<div style="margin-top: 10px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 12px">{grid}</div></div>')
    fmt = card(f'<div style="padding: 14px 0"><div style="font-size: 15px; font-weight: 600">出成</div><div style="margin-top: 10px">'
               f'{seg(["Excel 对比表", "Word 汇编"], "Excel 对比表")}</div>'
               f'<div style="margin-top: 10px; font-size: 13px; line-height: 1.7; color: {T2}">一份通知一行、一类内容一列；每格是原文原句，后面注明出处（正文写“正文”，附件写第几段、第几行）；没摘到的格子空着，在“读了”那一格写没摘到几类。</div>'
               f'{preview_table()}<div style="margin-top: 8px; font-size: 12px; color: {T3}">预览只显示前三列</div>'
               f'<div style="margin-top: 12px; padding: 8px 10px; border-radius: 8px; background: {BLUE_TINT}; font-size: 12.5px; line-height: 1.7; color: {T1}">'
               f'文件开头写着「AI 生成 · 句子摘自政府原文、未改写」，文件属性里也带隐式标识；生成后在「文件」里能找到。</div></div>')
    bottom = button_bar("还没 AI 解读的先解读，每份约半分钟。<br>这样的新解读每天最多 30 份，不占 AI 解读的 5 次。<br>今天还能生成 20 个文件。", "生成 Excel 对比表")
    return phone(nav("整理成文件") + notice_bar(nextv=True) + sel + what + fmt + '<div style="height: 16px"></div>', bottom)


# ---------- B：全做成对话页 ----------
def tab_nav(title):
    """底栏页的标题栏：没有返回，右上是胶囊。"""
    return (f'<header style="height: 48px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; position: relative; background: {CARD}">'
            f'<span style="font-size: 17px; font-weight: 500">{title}</span>{capsule()}</header>')


def b_top():
    strip = (f'<div style="flex-shrink: 0; height: 40px; padding: 0 16px; display: flex; align-items: center; justify-content: space-between; gap: 8px; '
             f'background: {CARD}; border-bottom: 1px solid {LINE}"><span style="min-width: 0; font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; '
             f'text-overflow: ellipsis">标「AI 生成」的回答由 AI 生成 · 以原文为准</span><span style="flex-shrink: 0; font-size: 13px; color: {BLUE}">说明</span></div>')
    return tab_nav("AI 助手") + notice_bar(nextv=True) + strip


def b_pinned():
    """带着一份通知问的时候，通知固定在顶部，可以换。"""
    return (f'<div style="margin: 10px 12px 0; padding: 12px 14px; background: {CARD}; border-radius: 12px; display: flex; gap: 10px; align-items: flex-start">'
            f'<div style="flex: 1; min-width: 0"><div style="font-size: 12px; color: {T2}">正在问这份通知</div>'
            f'<div style="margin-top: 2px; font-size: 14.5px; font-weight: 600; line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; '
            f'-webkit-box-orient: vertical; overflow: hidden">{DOC["title"]}</div>'
            f'<div style="margin-top: 2px; font-size: 12px; color: {T2}">{DOC["issuer"]} · {DOC["date"]}</div></div>'
            f'<span style="flex-shrink: 0; font-size: 13px; color: {BLUE}">换一份 ›</span></div>')


def bubble(text):
    return (f'<div style="display: flex; justify-content: flex-end; padding: 16px 12px 0 56px">'
            f'<div style="padding: 8px 14px 9px; background: {BLUE}; color: #FFFFFF; border-radius: 18px 18px 4px 18px; font-size: 15px; line-height: 1.6">{text}</div></div>')


def suggest(*items):
    c = "".join(f'<span style="height: 32px; padding: 0 12px; display: inline-flex; align-items: center; border-radius: 16px; background: {CARD}; '
                f'border: 1px solid {LINE2}; font-size: 13px; color: {T1}; white-space: nowrap">{t}</span>' for t in items)
    return f'<div style="padding: 10px 12px 0; display: flex; flex-wrap: wrap; gap: 8px">{c}</div>'


B_HINT = "说要找什么通知，或问一份通知里的事"
B_NOTE = "找通知不用 AI、不算次数；问通知里的事由 AI 摘原句，今天还剩 5 次。<br>请不要写企业名称、手机号。"


def b_input(ph=B_HINT, note=B_NOTE, pb=10):
    return (f'<div style="flex-shrink: 0; background: {CARD}; border-top: 1px solid {LINE}; padding: 8px 12px {pb}px">'
            f'<div style="display: flex; align-items: center; gap: 8px">'
            f'<div style="flex-grow: 1; min-width: 0; height: 40px; padding: 0 14px; box-sizing: border-box; border-radius: 20px; background: {FILL}; display: flex; '
            f'align-items: center; font-size: 14.5px; color: {T3}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">{ph}</div>'
            f'<span style="flex-shrink: 0; height: 36px; padding: 0 15px; display: inline-flex; align-items: center; border-radius: 18px; background: {BLUE}; '
            f'color: #FFFFFF; font-size: 14.5px; font-weight: 500">发送</span></div>'
            f'<div style="margin-top: 7px; font-size: 11.5px; line-height: 1.6; color: {T3}">{note}</div></div>')


def b_page(top, ph=B_HINT, note=B_NOTE):
    return phone(top + '<div style="height: 18px"></div>', b_input(ph, note) + tabbar())


def b_home():
    items = [("找通知", "说要找什么，按规则在收录里找，不用 AI"), ("看懂一份通知", "截止、条件、材料，AI 从原文摘原句"),
             ("整理成文件", "几份通知出 Excel 对比表、Word 汇编")]
    rows = "".join(f'<div style="margin-top: 8px; display: flex; gap: 8px; font-size: 14px; line-height: 1.6">'
                   f'<span style="flex-shrink: 0; margin-top: 9px; width: 5px; height: 5px; border-radius: 50%; background: {BLUE}"></span>'
                   f'<span><b style="font-weight: 600">{a}：</b>{b}</span></div>' for a, b in items)
    hello = answer("sys", f'<div style="font-size: 15px; font-weight: 600">在这里可以</div>{rows}' + para("不判断能不能申报，不代写材料。", 12.5, T2, 8))
    recent = (f'<div style="margin: 10px 12px 0; padding: 0 14px 14px; background: {CARD}; border-radius: 12px">'
              f'<div style="padding-top: 12px; font-size: 12px; color: {T2}">刚才看的通知</div>{doc_row(doc_with_due(), None, last=True, role=True, inapp=True)}'
              f'<div style="display: flex; flex-wrap: wrap; gap: 8px">{pill("AI 解读")}{pill("要准备哪些材料")}{pill("条款对照")}</div></div>')
    examples = (f'<div style="padding: 16px 16px 0; font-size: 13px; color: {T2}">可以这样说</div>' + suggest(ab.FIND_Q, "找宁夏近 7 天的申报"))
    hist = card(card_head("最近的对话", "全部")
                + record(DOC["title"], "[日期] · AI 解读、问了 2 句") + record(FIND_SHORT, "[日期] · 找通知、整理成文件", last=True))
    return b_page(b_top() + hello + recent + examples + hist)


def b_card():
    """B 的详情页卡片：三个按钮加一行提问框，点哪个都进对话页、带上这份通知。"""
    right = f'<span style="font-size: 12.5px; font-weight: 400; color: {T2}">今天还剩 <b style="font-weight: 700; color: {BLUE}">5</b> 次</span>'
    pills = f'<div style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px">{pill("AI 解读", 13.5, 34)}{pill("要准备哪些材料", 13.5, 34)}{pill("条款对照", 13.5, 34)}</div>'
    ask = (f'<div style="margin-top: 12px; display: flex; align-items: center; gap: 8px">'
           f'<div style="flex: 1; min-width: 0; height: 40px; padding: 0 14px; box-sizing: border-box; border-radius: 20px; background: {FILL}; display: flex; '
           f'align-items: center; font-size: 14px; color: {T3}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">问这份通知里的事，比如截止、条件</div>'
           f'<span style="flex-shrink: 0; height: 36px; padding: 0 15px; display: inline-flex; align-items: center; border-radius: 18px; background: {BLUE}; '
           f'color: #FFFFFF; font-size: 14.5px; font-weight: 500">发送</span></div>')
    return card(section_title("AI 助手", right)
                + f'<div style="margin-top: 4px; font-size: 14px; line-height: 1.7">点哪个都进 AI 助手的对话页，带上这份通知；AI 只从原文摘原句，每句都能回原文核对。</div>'
                + pills + ask + '<div style="height: 16px"></div>')


def b_detail():
    return detail_with(b_card())


def b_interpret():
    body = (para(f"摘出了 {TOTAL} 句，分在 5 类（可能没摘全）：")
            + f'<div style="margin-top: 10px; padding: 10px 12px; border-radius: 10px; border: 1px solid {LINE}">{due_block()}</div>'
            + f'<div style="margin-top: 12px; display: flex; flex-wrap: wrap; gap: 6px">{count_chips(0, 28, 12.5)}</div>'
            + quote(1, OBJ[0]) + quote(2, OBJ[1])
            + f'<div style="margin-top: 12px; padding: 9px 0; border-radius: 8px; background: {FILL}; text-align: center; font-size: 13px; color: {BLUE}">'
              f'看其余 4 类（{TOTAL - len(OBJ)} 句）</div>'
            + para(f"没摘到：{MISSING}，可能原文没写。", 13, T2, 10) + para(ab.READ_BODY, 13, T2, 4))
    top = b_top() + b_pinned() + bubble("AI 解读这份通知") + answer("ai", body) + suggest("要准备哪些材料", "条款对照", "整理成文件")
    return b_page(top, "问这份通知里的事", "今天还剩：AI 解读 4 次，问这份通知 5 次。<br>请不要写企业名称、手机号。")


def b_find():
    rows = "".join(doc_row(d, "AI 解读" if d["ai"]["interpret"] else None, last=i == len(FIND) - 1, role=True, inapp=True) for i, d in enumerate(FIND))
    body = (para("按这些条件找的：类型 申报通知 · 发布 近 30 天 · 标题里有 5G工厂", 13.5, T2)
            + para(f"找到 {DATA['find']['total']} 份，按发布日期从新到旧：", 14.5, T1, 4) + f'<div style="margin-top: 2px">{rows}</div>'
            + para("没有「AI 解读」按钮的是现在不能解读的通知（比如还没有拿到完整正文），点标题能看到原因。", 12.5, T2, 8))
    top = b_top() + bubble(ab.FIND_Q) + answer("find", body) + suggest(f"把能解读的 {len(COMPILE)} 份整理成对比表", "只看河北的")
    return b_page(top)


def b_materials():
    body = (para(f"摘出了原文里写材料的 {COUNTS[4][1]} 句（可能没摘全，以原文为准）。两类项目要的不一样，先列企业自主研发项目的 {len(MAT_SELF)} 项：")
            + f'<div style="margin-top: 10px; font-size: 12px; line-height: 1.6; color: {T2}">原文：{line(31)} › {line(32)}</div>'
            + "".join(quote_plain(t) for t in MAT_SELF)
            + f'<div style="margin-top: 12px; padding: 9px 0; border-radius: 8px; background: {FILL}; text-align: center; font-size: 13px; color: {BLUE}">'
              f'看企业委托研发项目的 {len(MAT_ENTR)} 项</div>'
            + para(ab.READ_BODY, 13, T2, 10)
            + f'<div style="margin-top: 8px">{link("做成能勾选的清单 ›", 13.5)}</div>')
    top = b_top() + b_pinned() + bubble("要准备哪些材料？") + answer("ai", body)
    return b_page(top, "问这份通知里的事", "这一问用 AI 解读的结果，不另算次数。<br>请不要写企业名称、手机号。")


def b_compile():
    items = "".join(f'<div style="padding: 8px 0; display: flex; gap: 8px; border-top: 1px solid {LINE}; font-size: 13.5px; line-height: 1.6">'
                    f'<span style="flex-shrink: 0; color: {T2}">{d["province"]}</span><span style="min-width: 0; white-space: nowrap; overflow: hidden; '
                    f'text-overflow: ellipsis">{d["title"]}</span></div>' for d in COMPILE)
    confirm = answer("sys", para(f"要整理这 {len(COMPILE)} 份：", 14)
                     + f'<div style="margin-top: 8px">{items}</div>'
                     + para("重庆那份还没有完整正文，不能整理。", 12.5, T2, 2)
                     + para("放申报对象、条件、时间、材料清单，出 Excel 对比表。", 13.5, T1, 8)
                     + para("还没 AI 解读的先解读，每份约半分钟，不占每天 5 次。", 12.5, T2, 4)
                     + f'<div style="margin-top: 12px; display: flex; gap: 8px">{small_btn("开始整理", True)}{small_btn("改一下")}</div>')
    file_card = (f'<div style="display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 10px; background: {FILL}">'
                 f'<span style="flex-shrink: 0; width: 40px; height: 40px; border-radius: 8px; background: {CARD}; display: flex; align-items: center; '
                 f'justify-content: center">{icon("doc", 22, BLUE)}</span><div style="flex: 1; min-width: 0">'
                 f'<div style="font-size: 14.5px; font-weight: 600; line-height: 1.5; overflow-wrap: anywhere">{FILE_NAME}</div>'
                 f'<div style="margin-top: 2px; font-size: 12px; color: {T2}">{len(COMPILE)} 份通知 · 4 类内容 · Excel</div></div></div>'
                 f'<div style="margin-top: 10px; display: flex; gap: 8px">{small_btn("打开文件", True)}{small_btn("转发")}</div>'
                 + para("文件开头写着「AI 生成 · 句子摘自政府原文、未改写」。<br>文件里也带隐式标识，在「文件」里能再找到。", 12, T3, 8))
    top = b_top() + bubble(f"把能解读的 {len(COMPILE)} 份整理成 Excel 对比表") + confirm + bubble("开始整理") + answer("ai", file_card)
    return b_page(top)


# ---------- C：A+B 结合（Claude、Codex、agy 一起设计，10/29） ----------
# 对话当入口和线索（B），整页当交付物（A）。三方各自设计后合成（方案原文在共享笔记 ai-combine-1029/）：
# AI 首页照 A 先给结果，顶上是找通知的框；B 的对话入口放进"刚才看的通知"卡片，只问这份通知（agy）；
# 问一问是普通页面（能带着通知打开），回答都是摘要卡片、最多列 3 句（agy），长结果点"看完整结果"打开整页；
# 整页底栏"回到对话"回到原来那条消息，整页里做出来的文件在对话里补一张卡片（Codex）；准备材料是解读结果页的一种看法（Codex）。
# 第 1 轮审阅（Codex、agy、独立 Claude）以后：次数写全、准备材料什么时候算次数写清、没带通知时输入框只找通知、
# 按钮做的事在对话里标"点了按钮"、"这句摘错了"统一、材料清单页标题跟着看法走、页底补模型和备案号。
C_HINT = "说要找什么通知，比如地区、类型、文号"
ASK_EXAMPLES = ("要满足哪些条件？", "在哪里申报？")
VIEWS_SEG = ["全部原句（30 句）", "准备材料（能勾选）"]
PAGE_TRACK = "#E3E6EC"   # 放在页面底色上的分段选择用的底
NO_NAMES = "请不要写企业名称、手机号。"


def small_note(html, mt=8, pb=0):
    """说明文字：12px 用 T2（T3 在白底上对比度只有 3.2:1，独立 Claude 第 1 轮）。"""
    return f'<div style="margin-top: {mt}px; padding-bottom: {pb}px; font-size: 12px; line-height: 1.7; color: {T2}">{html}</div>'


def c_strip():
    """AI 首页的固定标识条：协议写定页面顶部固定显示"AI 生成"；说清是标出来的那部分，免得刚才看的通知、找通知也像 AI 生成的（agy 第 1 轮）。"""
    return ab.ai_strip().replace("AI 生成 · 以原文为准", "标「AI 生成」的内容由 AI 生成 · 以原文为准")


def ask_row(btn="问"):
    return (f'<div style="display: flex; align-items: center; gap: 8px">'
            f'<div style="flex: 1; min-width: 0; height: 40px; padding: 0 14px; box-sizing: border-box; border-radius: 20px; background: {FILL}; display: flex; '
            f'align-items: center; font-size: 14px; color: {T3}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">问这份通知里的事，比如截止、条件</div>'
            f'<span style="flex-shrink: 0; height: 36px; padding: 0 15px; display: inline-flex; align-items: center; border-radius: 18px; background: {BLUE}; '
            f'color: #FFFFFF; font-size: 14.5px; font-weight: 500">{btn}</span></div>')


def c_continue():
    """刚才看的通知：A 的截止和直达，加 B 的对话入口——两个常问问题和"问别的"，点了进问一问、带上这份通知。
    不放提问框（D-075 第四节"AI 首页不放空白提问框"不用改；Codex、agy 第 2 轮都主张这样，一页两个输入框也容易看岔）。"""
    acts = (f'<div style="padding: 0 0 12px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px">{chip("AI 解读")}'
            f'{link("准备材料 ›")}{link("条款对照 ›")}</div>')
    asks = "".join(f'<span style="height: 30px; padding: 0 11px; display: inline-flex; align-items: center; border-radius: 15px; background: {CARD}; '
                   f'border: 1px solid {LINE2}; font-size: 12.5px; color: {T1}; white-space: nowrap">{t}</span>' for t in ASK_EXAMPLES)
    more = (f'<span style="height: 30px; padding: 0 11px; display: inline-flex; align-items: center; gap: 1px; border-radius: 15px; background: {BLUE_TINT}; '
            f'font-size: 12.5px; font-weight: 600; color: {BLUE}; white-space: nowrap">问别的{icon("chev", 13, BLUE)}</span>')
    ask = (f'<div style="padding: 12px 0 14px; border-top: 1px solid {LINE}"><div style="font-size: 12.5px; color: {T2}">问这份通知</div>'
           f'<div style="margin-top: 8px; display: flex; flex-wrap: wrap; gap: 8px">{asks}{more}</div>'
           + small_note("点了进问一问、带上这份通知；<br>今天还剩 5 次，找到原句才算。", 6) + "</div>")
    return card(card_head("刚才看的通知") + doc_row(doc_with_due(), None, last=True, role=True, inapp=True) + acts + ask)


C_TASKS = [("doc", "AI 解读", "一份通知的<br>原句分好类"), ("check", "准备材料", "材料原句<br>列成清单"), ("copy", "条款对照", "条件和企业<br>情况并排"),
           ("download", "整理成文件", "几份通知出成<br>Excel、Word"), ("info", "问一问", "找通知，或问<br>一份通知"), ("list", "查名单", "企业在不在<br>公开名单里")]


def c_tasks():
    """要办的事：同 A 的 6 格；下面把每样的次数写全（Codex、agy 第 1 轮：问一问每天 20 句、整理成文件的额度、准备材料什么时候算次数）。"""
    tiles = "".join(task_tile(*t) for t in C_TASKS)
    rules = ("AI 解读、准备材料、条款对照：点了先选通知", "AI 解读、条款对照、问这份通知：今天各还剩 5 次",
             "问这份通知找到原句才算 1 次，没找到不算；", "另外每天最多问 20 句，找没找到都算", "整理成文件：每天最多 20 个，一次最多 10 份",
             "准备材料：没解读过的先解读（算 1 次 AI 解读）", "找通知、查名单不用 AI，不算次数")
    return card(card_head("要办的事")
                + f'<div style="padding: 12px 0 4px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px">{tiles}</div>'
                + small_note("<br>".join(rules), 6, 14))


def c_records():
    recs = (record(f"问一问 · {DOC['title']}", "[日期] · AI 解读、准备材料、问了 2 句")
            + record(f"问一问 · {FIND_SHORT}", "[日期] · 找到 5 份，整理成 Excel 对比表")
            + record(f"条款对照 · {DOC['title']}", "[日期] · 已完成", last=True))
    return card(card_head("我的 AI 记录", "全部") + recs)


def c_home():
    top = a_head() + c_strip() + c_continue() + a_ready() + c_tasks() + c_records() + ai_foot() + '<div style="height: 20px"></div>'
    return phone(top, tabbar())


def c_card():
    """详情卡片：A 的"开始解读"和直达按钮，加 B 的一行提问框（发出去进问一问对话页、带上这份通知）。"""
    right = f'<span style="font-size: 12.5px; font-weight: 400; color: {T2}">今天还剩 <b style="font-weight: 700; color: {BLUE}">5</b> 次</span>'
    two = (f'<div style="margin-top: 10px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px">'
           + outline_btn("准备材料", 13.5) + outline_btn("条款对照", 13.5) + "</div>")
    ask = f'<div style="margin-top: 14px; padding-top: 14px; border-top: 1px solid {LINE}">{ask_row()}</div>'
    return card(section_title("AI 解读", right)
                + f'<div style="margin-top: 4px; font-size: 14px; line-height: 1.7">约半分钟把申报对象、条件、时间、材料的原文原句摘出来分好类，每句都能回原文核对。</div>'
                + btn_primary("开始解读") + two
                + small_note("没解读过的点「准备材料」会先解读（算 1 次）；<br>条款对照今天还剩 5 次。")
                + ask + small_note(f"问这份通知今天还剩 5 次，找到原句才算；<br>另外每天最多问 20 句，找没找到都算。<br>{NO_NAMES}<br>AI 只摘原句，不判断能不能申报。", 8, 16))


def c_detail():
    return detail_with(c_card(), h=1390)


def c_top(pinned=True):
    """问一问对话页：普通页面（左上返回），出处说明、固定的 AI 标识条；带着通知时通知固定在顶部。"""
    strip = (f'<div style="flex-shrink: 0; height: 40px; padding: 0 16px; display: flex; align-items: center; justify-content: space-between; gap: 8px; '
             f'background: {CARD}; border-bottom: 1px solid {LINE}"><span style="min-width: 0; font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; '
             f'text-overflow: ellipsis">标「AI 生成」的回答由 AI 生成 · 以原文为准</span><span style="flex-shrink: 0; font-size: 13px; color: {BLUE}">说明</span></div>')
    return nav("问一问") + notice_bar(nextv=True) + strip + (b_pinned() if pinned else "")


def tapped(text):
    """点按钮发出的那一步：蓝色气泡上写"点了按钮"（打字只用来找通知、问这份通知，Codex 第 1 轮）。"""
    return ab.user_msg("点了按钮", text)


def q_chat(n, text):
    """对话里的原句：同 ai_board.quote，"纠错"统一叫"这句摘错了"（agy 第 1 轮）。"""
    return ab.quote(n, text).replace("纠错</span>", "这句摘错了</span>").replace("出处：正文", "位置：正文")


def open_full(text):
    """摘要卡片里打开整页的整行按钮。"""
    return (f'<div style="margin-top: 12px; height: 40px; display: flex; align-items: center; justify-content: center; gap: 2px; border-radius: 20px; '
            f'background: {BLUE_TINT}; color: {BLUE}; font-size: 14px; font-weight: 600">{text}{icon("chev", 15, BLUE)}</div>')


def c_page(top, ph, note):
    return phone(top + '<div style="height: 18px"></div>', b_input(ph, note, pb=28))


def c_interpret():
    body = (para(f"摘出了 {TOTAL} 句，分在 5 类，可能没摘全。先看截止和申报对象：")
            + f'<div style="margin-top: 10px; padding: 10px 12px; border-radius: 10px; border: 1px solid {LINE}">{due_block()}</div>'
            + f'<div style="margin-top: 12px; display: flex; flex-wrap: wrap; gap: 6px">{count_chips(None, 28, 12.5)}</div>'
            + small_note("点类别打开完整结果、跳到那一类；<br>材料清单里有 1 句和申报方式相同。", 6)
            + q_chat(1, OBJ[0]) + q_chat(2, OBJ[1])
            + open_full(f"看完整结果（{TOTAL} 句）")
            + para(f"没摘到：{MISSING}，可能原文没写。", 13, T2, 10) + para(ab.READ_BODY, 13, T2, 4))
    top = c_top() + tapped("AI 解读这份通知") + answer("ai", body) + suggest("准备材料", "条款对照", "加入整理")
    return c_page(top, "接着问这份通知里的事", f"今天还剩：问这份通知 5 次，AI 解读 4 次。<br>找到原句才算 1 次；还能问 20 句，找没找到都算。<br>对话保存 30 天；{NO_NAMES}")


def res_foot():
    """整页最下面：模型和备案号、投诉举报（同现在解读结果页的页底，agy 第 1 轮）。"""
    return (f'<div style="padding: 18px 12px 0; text-align: center; font-size: 12px; line-height: 1.8; color: {T3}"><div>{DISC_MODEL}</div><div>{DISC_FILING}</div>'
            f'<div style="margin-top: 4px; color: {BLUE}">投诉举报 ›</div></div>')


def c_result():
    """从对话里点"看完整结果"打开的整页：同 A 的解读结果，顶上多一个看法切换（全部原句 / 材料清单），底栏"回到对话"。"""
    view = f'<div style="margin: 10px 12px 0">{seg(VIEWS_SEG, VIEWS_SEG[0], PAGE_TRACK)}</div>'
    top = nav("AI 解读") + result_strip() + a_summary() + view + a_jump() + result_sections() + res_foot()
    return first_screen(top, bar([("条款对照", "copy"), ("加入整理", "plus")], "回到对话"), h=1880)


def c_find():
    add = (f'<span style="height: 30px; padding: 0 11px; display: inline-flex; align-items: center; gap: 2px; border-radius: 15px; border: 1px dashed {LINE2}; '
           f'color: {BLUE}; font-size: 13px; white-space: nowrap">{icon("plus", 14, BLUE)}地区</span>')
    chips_row = (f'<div style="margin-top: 8px; display: flex; flex-wrap: wrap; gap: 8px">{cond_chip("类型：", "申报通知")}{cond_chip("发布：", "近 30 天")}'
                 f'{cond_chip("标题里有：", "5G工厂", close=True)}{add}</div>')
    shown = FIND[:3]
    rows = "".join(doc_row(d, "AI 解读" if d["ai"]["interpret"] else None, last=i == len(shown) - 1, role=True, inapp=True) for i, d in enumerate(shown))
    body = (f'<div style="display: flex; align-items: baseline; justify-content: space-between; gap: 8px"><span style="font-size: 14px; font-weight: 600">按这些条件找的</span>'
            f'<span style="font-size: 12px; color: {T3}">点条件就能改</span></div>{chips_row}'
            + para(f"找到 {DATA['find']['total']} 份，按发布日期从新到旧，先列 {len(shown)} 份：", 14, T1, 10)
            + f'<div style="margin-top: 2px">{rows}</div>'
            + small_note("没有「AI 解读」按钮的是现在不能解读的通知（比如还没有拿到完整正文），点标题能看到原因。", 4)
            + open_full(f"看全部 {DATA['find']['total']} 份"))
    top = c_top(pinned=False) + bubble(ab.FIND_Q) + answer("find", body) + suggest(f"把能解读的 {len(COMPILE)} 份整理成文件")
    return c_page(top, C_HINT, f"找通知不用 AI、不算次数。<br>要问某一份里的事，点那一行进去再问；{NO_NAMES}")


def utabs(options, selected):
    """下划线分页（项目类型），和上面的看法切换区分开。"""
    out = []
    for o in options:
        st = f"color: {T1}; font-weight: 600; border-bottom: 2px solid {BLUE}" if o == selected else f"color: {T2}; border-bottom: 2px solid transparent"
        out.append(f'<span style="padding: 10px 0 8px; font-size: 14px; white-space: nowrap; {st}">{o}</span>')
    return f'<div style="display: flex; gap: 24px; border-bottom: 1px solid {LINE}">{"".join(out)}</div>'


def c_materials():
    """"准备材料"就是解读结果页的"材料清单"看法，标题跟着看法走（agy 第 1 轮）：两类项目分开，能勾选，勾选只存在手机上。"""
    done = 2
    head = card(f'<div style="padding: 14px 0"><div style="font-size: 15px; font-weight: 600; line-height: 1.6">{DOC["title"]}</div>'
                f'<div style="margin-top: 6px; font-size: 12.5px; line-height: 1.6; color: {T2}">用 AI 解读的结果，没解读过的先解读（算 1 次）。<br>'
                f'勾选只存在这台手机上，不上传；<br>换手机或清除小程序数据会丢。</div></div>')
    view = f'<div style="margin: 10px 12px 0">{seg(VIEWS_SEG, VIEWS_SEG[1], PAGE_TRACK)}</div>'
    prog = (f'<div style="margin-top: 12px; display: flex; align-items: center; gap: 10px"><span style="font-size: 13px; font-weight: 600; white-space: nowrap">'
            f'已备 {done} / {len(MAT_SELF)} 项</span><span style="flex: 1; height: 6px; border-radius: 3px; background: {FILL}; overflow: hidden">'
            f'<span style="display: block; width: {done * 100 // len(MAT_SELF)}%; height: 100%; background: {BLUE}"></span></span></div>')
    rows = "".join(check_row(t, i < done, i == 0) for i, t in enumerate(MAT_SELF))
    shared = (f'<div style="margin-top: 4px; padding: 12px 0 14px; border-top: 1px solid {LINE}"><div style="font-size: 12.5px; color: {T2}">'
              f'另有 1 句和「申报方式」相同（两类项目都适用）：</div><div style="margin-top: 6px; padding: 8px 10px; border-radius: 8px; background: {ab.QUOTE_BG}; '
              f'font-size: 14px; line-height: 1.75">{Q_W2}</div>'
              f'<div style="margin-top: 6px; display: flex; align-items: center; font-size: 12.5px">{link("看原文 ›", 12.5)}'
              f'<span style="margin-left: auto; display: inline-flex; align-items: center; gap: 3px; color: {T2}">{icon("flag", 13, T2)}这句摘错了</span></div></div>')
    lst = card(f'<div style="padding-top: 14px; font-size: 13px; color: {T2}">两类项目要的材料不一样，按你报的那一类看：</div>'
               + utabs([f"企业自主研发项目 {len(MAT_SELF)}", f"企业委托研发项目 {len(MAT_ENTR)}"], f"企业自主研发项目 {len(MAT_SELF)}")
               + src_label(line(31), line(32)) + prog + rows + shared)
    note = (f'<div style="padding: 10px 16px 0; font-size: 12.5px; line-height: 1.6; color: {T2}">这份通知没有附件，材料都写在正文里；以原文和主管部门的解释为准。</div>')
    top = nav("准备材料") + result_strip() + head + view + lst + note + res_foot() + '<div style="height: 20px"></div>'
    return phone(top, bar([("条款对照", "copy"), ("加入整理", "plus")], "回到对话"))


def c_compile():
    items = "".join(f'<div style="padding: 8px 0; display: flex; gap: 8px; border-top: 1px solid {LINE}; font-size: 13.5px; line-height: 1.6">'
                    f'<span style="flex-shrink: 0; color: {T2}">{d["province"]}</span><span style="min-width: 0; white-space: nowrap; overflow: hidden; '
                    f'text-overflow: ellipsis">{d["title"]}</span></div>' for d in COMPILE)
    rules = ("还没 AI 解读的先解读，每份约半分钟。", "这样的新解读每天最多 30 份，不占 AI 解读的 5 次。", "今天还能生成 20 个文件，一次最多 10 份。")
    confirm = answer("sys", para(f"要整理这 {len(COMPILE)} 份：", 14)
                     + f'<div style="margin-top: 8px">{items}</div>'
                     + para("重庆那份还没有完整正文，不能整理。", 12.5, T2, 2)
                     + para("放对象、条件、时间、材料清单，出 Excel 对比表。", 13.5, T1, 10)
                     + small_note("前三列预览（长句只露出写日期的一段）：", 4) + preview_table()
                     + small_note("<br>".join(rules), 10)
                     + f'<div style="margin-top: 12px; display: flex; gap: 8px">{small_btn("开始整理", True)}{small_btn("改内容或格式")}</div>')
    file_card = (f'<div style="display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 10px; background: {FILL}">'
                 f'<span style="flex-shrink: 0; width: 40px; height: 40px; border-radius: 8px; background: {CARD}; display: flex; align-items: center; '
                 f'justify-content: center">{icon("doc", 22, BLUE)}</span><div style="flex: 1; min-width: 0">'
                 f'<div style="font-size: 14.5px; font-weight: 600; line-height: 1.5; overflow-wrap: anywhere">{FILE_NAME}</div>'
                 f'<div style="margin-top: 2px; font-size: 12px; color: {T2}">{len(COMPILE)} 份通知 · 4 类内容 · Excel</div></div></div>'
                 f'<div style="margin-top: 10px; display: flex; gap: 8px">{small_btn("打开文件", True)}{small_btn("转发")}</div>'
                 + para("每句后面注明出处（正文写「正文」，附件写第几段、第几行），<br>照着到站内阅读里找原文。<br>文件开头写着「AI 生成 · 句子摘自政府原文、未改写」<br>和模型、备案号，也带隐式标识；在「文件」里能再找到。", 12, T2, 8))
    top = (c_top(pinned=False) + tapped(f"把能解读的 {len(COMPILE)} 份整理成文件") + confirm + tapped("开始整理") + answer("ai", file_card))
    return c_page(top, C_HINT, f"找通知不用 AI、不算次数。<br>{NO_NAMES}")


# ---------- 六件事，A、B 各一屏 ----------
# (行号, 这件事, 一句说明, A 屏, B 屏)；每屏 (屏号, 标题, 画面, 说明, 标签)
ROWS = [
    ("1", "打开 AI", "从底栏点「AI」进来，第一屏看到什么。",
     ("A1_AI首页", "任务和结果首页", a_home,
      "顶上是找通知的输入框（和首页搜索框一个样子，按规则找、不用 AI）。下面先给不用问就能看的：刚才看的通知（截止和下一步）、近 7 天新的申报通知、"
      "关注的新文件、截止；再是六件要办的事，点了直接到那件事的页面。",
      ["一进来先给结果", "输入框只找通知", "六件事各有入口"]),
     ("B1_对话首页", "对话页", b_home,
      "进来就是对话页：一段话说能做什么，刚才看的通知带三个按钮，下面是例子和最近的对话，最底下是输入框。找通知、问通知都在这个框里说，"
      "后台按规则分开；分不清时要再问用户是哪一种，没选通知就问也要先让用户选通知。",
      ["像聊天", "要先想怎么说", "一个框做所有事"])),
    ("2", "从一份通知进来", "通知详情页「要点」下面的 AI 卡片（两种做法都在这个位置，D-075）。",
     ("A2_通知详情", "卡片：开始解读，三件事直达", a_detail,
      "主按钮「开始解读」，下面三个直达：准备材料、条款对照、问这份通知，各进各的页面。标题卡那行「这份通知可以 AI 解读 ↓」不变。",
      ["位置不动", "三件事直达"]),
     ("B2_通知详情", "卡片：三个按钮加提问框", b_detail,
      "卡片换成三个按钮加一行提问框，点哪个都进 AI 助手对话页、带上这份通知。底栏页不能直接带着参数打开，"
      "带着通知进对话、换通知、返回原来的通知，都要另外处理。",
      ["能直接打字问", "都进同一个对话"])),
    ("3", "看懂一份通知", "点「开始解读」以后，约半分钟出结果（湖北科技厅这份，原句逐字取自正文，摘哪几句是示意）。",
     ("A3_解读结果", "整页的解读结果", a_result,
      "先给一眼看的：两种截止分开写、读了哪些、没摘到什么；再一排按类跳。句子按原文的小标题分组（程序认标题，不用模型），"
      "两类项目的条件不会混在一起；每句能看原文、纠错。底栏接着问、准备材料、加入整理。",
      ["先给一眼看的", "按原文小标题分组", "整页好核对"]),
     ("B3_对话里解读", "对话里的一张卡片", b_interpret,
      "同样的结果放在对话里的一张卡片：先显示一类，其余折叠。30 句全展开要滑很长；往上翻时和别的回答混在一起，下次要找得翻对话。",
      ["折叠着看", "长结果挤在对话里"])),
    ("4", "找通知", "输入「找近 30 天的 5G 工厂申报」（10/22 线上真实找到 5 份）。",
     ("A4_找通知", "结果页，条件能点着改", a_find,
      "进结果页。条件写成能点的胶囊（类型、发布时间、标题里的词），点了就改，不用重打一句；每行能直接 AI 解读；"
      "底下一键把能解读的 4 份整理成对比表。标「只查收录」，不标 AI 生成。",
      ["条件能点着改", "一键整理", "不算次数"]),
     ("B4_对话里找", "对话里的结果卡片", b_find,
      "对话里回一张「只查收录」的卡片，同样 5 份。想改条件要再说一句（比如「只看河北的」），每改一次多一张卡片。",
      ["再说一句改条件", "不算次数"])),
    ("5", "准备材料", "这份通知分两类项目，材料要求不一样。",
     ("A5_准备材料", "能勾选的清单页", a_materials,
      "单独一页清单：先选报哪一类；每项是原文原句，能看原文、纠错；勾选只存在手机上，不上传。用解读的结果，不另算次数。"
      "这份没有附件就照实写。",
      ["能勾选", "按项目类型分开", "不另算次数"]),
     ("B5_对话里材料", "对话里列出原句", b_materials,
      "对话里列出 6 句材料原句，另一类折叠。要勾选、下次接着用，还是得点「做成能勾选的清单」打开一个清单页（就是 A 的那一页）。",
      ["要勾选还得开页面"])),
    ("6", "几份整理成文件", "把找到的 4 份 5G 工厂申报整理成 Excel 对比表（AI 汇编第一期，D-082）。",
     ("A6_整理成文件", "整理页，先看预览", a_compile,
      "整理页：选了哪几份（最多 10 份）、放哪些内容、出 Excel 还是 Word，先看预览再生成。预览里的截止原句是收录里真实摘到的。"
      "生成后在「文件」里也能找到。",
      ["先看预览", "最多 10 份", "带 AI 标识"]),
     ("B6_对话里整理", "确认卡片和文件卡片", b_compile,
      "对话里先回一张确认卡片（整理哪几份、放什么），点「开始整理」后回一张文件卡片。想改内容、换格式要再说一句或点「改一下」。",
      ["确认卡片", "文件卡片"])),
]

SCREENS = [s for r in ROWS for s in (r[3], r[4])]

C_SHOTS = [
    ("C1_AI首页", "AI 首页：先给结果，对话入口放进刚才看的通知", c_home,
     "照 A 先给结果：顶上是找通知的框（按规则找、不用 AI、不算次数），下面刚才看的通知、直接能看的、要办的事、最近的对话和结果。"
     "B 的对话入口放进「刚才看的通知」卡片：两个常问问题和「问别的」，点了进问一问、带上这份通知，在对话页里打字。首页不放提问框，D-075 第四节不用改。"
     "「要办的事」里 AI 解读、准备材料、条款对照点了先选通知；「问一问」不带通知也能进（先只能找通知）。没看过通知时，上面那张卡片换成「先选一份通知」。次数在下面写全。"
     "图里「整理成文件」那格和它的额度、页头「几份还能整理成一张表」、记录里的 Excel 是 AI 汇编上线以后的样子，汇编上线前都不显示。",
     ["先给结果", "常问问题一点就问", "次数写全"]),
    ("C2_通知详情", "通知详情：直达整页，也能直接问", c_detail,
     "卡片还在「要点」下面。A 的「开始解读」和「准备材料、条款对照」直达整页；B 的一行提问框，发出去进问一问、带上这份通知。"
     "几样次数分开写；没解读过的点「准备材料」会先解读，写明算 1 次。",
     ["位置不动", "直达整页", "卡片里能直接问"]),
    ("C3_对话里解读", "问一问：摘要卡片", c_interpret,
     "问一问是普通页面（不是底栏页），带着通知打开，通知固定在顶上。回答是摘要卡片：两种截止的原句照列，按类几句，其余只列第一类、最多 3 句，"
     "「看完整结果」或点类别打开整页；卡片下面跟下一步按钮。两种截止都附原句、能看原文和纠错。"
     "这一问来自带着通知刚打开问一问时输入框上面的「AI 解读这份通知」（这份还没解读过才有）；解读、准备材料、整理成文件都是点按钮做的（气泡上写「点了按钮」），打字只用来找通知、问这份通知。",
     ["截止加 3 句以内", "看完整结果", "下一步按钮"]),
    ("C3b_解读整页", "整页解读：底栏回到对话", c_result,
     "从对话打开的整页：先给一眼看的，再按类跳、按原文小标题分组（认不出小标题就照原文顺序列）；顶上能切到「准备材料（能勾选）」。"
     "底栏「回到对话」回到原来那条消息；从详情直接打开的，底栏是「接着问这份通知」。点「加入整理」后页面上出现「去整理（1 份）」。页底照现在写模型和备案号。",
     ["整页好核对", "能切到准备材料", "回到对话"]),
    ("C4_对话里找", "问一问：找通知", c_find,
     "在对话里说要找什么：回一张「只查收录」卡片，条件能点着改（A 的胶囊），先列 3 份，「看全部 5 份」打开结果页（同 A4）。"
     "从 AI 首页找通知框进来的，直接到结果页。没带通知时输入框只提示找通知。",
     ["条件能点着改", "先列 3 份", "不算次数"]),
    ("C5_准备材料", "准备材料：解读结果的另一种看法", c_materials,
     "「准备材料」就是解读结果页里的「准备材料（能勾选）」看法，标题跟着变成「准备材料」：两类项目分开，每项是原文原句，能勾选，勾选只存在这台手机上（换手机会丢，写明了）。"
     "用解读的结果，没解读过的先解读、算 1 次。",
     ["少一个页面", "能勾选", "不另算次数"]),
    ("C6_对话里整理", "问一问：整理成文件", c_compile,
     "点「整理成文件」：确认卡片里有几份、放什么、前三列预览（A 的预览放进 B 的卡片）和额度，「开始整理」后回文件卡片；"
     "改内容或格式打开整理页（同 A6）。整理时的新解读每天最多 30 份（#332），文件在对话里留一张卡片，在「文件」里也能找到。"
     "这是 AI 汇编上线以后的画面；上线前，各屏里「加入整理」「整理成文件」的按钮都不显示。",
     ["确认里有预览", "文件留在对话里"]),
]

FLOW = [("入口", "AI 首页 · 通知详情的 AI 卡片 · 列表行的「AI 解读」"), ("→", "↓"),
        ("问一问对话页", "常问问题、问别的、详情里的提问框、要办的事里的「问一问」进来；回答是摘要卡片：截止原句加第一类最多 3 句"), ("⇄", "↕"),
        ("整页结果", "「AI 解读」「开始解读」直接到这里；对话里点「看完整结果」也到这里，底栏回到对话"), ("→", "↓"),
        ("找回", "我的 AI 记录 · 文件")]

# (在哪, 点什么, 去哪)
ENTRY = [
    ("AI 首页顶上", "找通知框", "找通知结果页（整页，条件能点着改；底下「整理成文件」）"),
    ("AI 首页 · 刚才看的通知", "AI 解读", "解读整页（底栏「接着问这份通知」）"),
    ("", "准备材料 ›", "解读整页的「准备材料（能勾选）」"),
    ("", "条款对照 ›", "条款对照页（现在的）"),
    ("", "常问问题、问别的", "问一问，带上这份通知"),
    ("AI 首页 · 要办的事", "AI 解读、准备材料、条款对照", "先选一份通知（默认刚才看的），再到上面那几页"),
    ("", "问一问", "问一问，不带通知：先只能找通知，选了通知再问"),
    ("", "整理成文件", "整理页：选通知、内容、格式，先看预览（AI 汇编上线后才有）"),
    ("", "查名单", "查名单页（现在的）"),
    ("通知详情 · AI 卡片", "开始解读、准备材料、条款对照", "解读整页、准备材料看法、条款对照页"),
    ("", "提问框", "问一问，带上这份通知"),
    ("列表行（查询结果、关注的新文件、截止、找通知卡片）", "AI 解读", "解读整页"),
    ("问一问 · 带着通知刚打开时", "AI 解读这份通知（这份还没解读过时才有）、常问问题", "对话里的解读摘要卡片（算 1 次 AI 解读）、问这份通知"),
    ("问一问 · 回答卡片", "看完整结果、类别胶囊", "解读整页，跳到那一类（底栏「回到对话」）"),
    ("问一问 · 下一步按钮", "准备材料、条款对照、加入整理", "准备材料看法、条款对照页、加进整理（出现「去整理（N 份）」）"),
    ("问一问 · 找通知卡片", "看全部 N 份、整理成文件", "找通知结果页、对话里的整理确认卡片"),
]

FROM_A = ["AI 首页先给结果和要办的事", "长结果整页看：解读、材料清单、找通知全部结果、整理页",
          "条件写成能点着改的胶囊", "句子按原文小标题分组，每句看原文、纠错", "整理前先看预览"]
FROM_B = ["一句话就能开始：详情卡片里直接问，刚才看的通知点常问问题就问", "回答是对话里的卡片，带下一步按钮",
          "带着一份通知接着问，通知固定在顶上", "整理成文件用对话里的确认卡片和文件卡片", "做过的事按对话找回"]

# 三方怎么合的：(问题, Claude, Codex, agy, 合成后)
MERGE = [
    ("对话页放哪", "普通页面，能带着通知打开", "普通页面，带通知编号打开", "二级全屏页，不做底栏页",
     "问一问做成普通页面，从详情、刚才看的通知带着通知打开；底栏 AI 首页不是对话"),
    ("AI 首页的对话入口", "顶上一条能找也能问的「问一问」入口", "不放框，点例子进对话；找通知用搜索框", "不放通用的框，「刚才看的通知」卡片里放一行只问这份通知",
     "顶上照 A 是找通知的框；「刚才看的通知」卡片里放两个常问问题和「问别的」，点了进问一问、带上这份通知；不放提问框，D-075 第四节不用改"),
    ("对话里放多少", "摘要卡片，最多 5 句", "简短答案和摘要卡片", "高浓度摘要，不超过 3 句", "摘要卡片：两种截止的原句照列，其余只列第一类、最多 3 句；别的「看完整结果」"),
    ("整页怎么回对话", "返回上一页", "回到原来那条消息；整页里做出的选择、文件回写成对话里的卡片", "返回上一页，回来后直接能接着问",
     "从对话打开的，底栏「回到对话」回到原来那条消息；从详情直接打开的，底栏「接着问这份通知」；整页里生成的文件在对话里补一张卡片"),
    ("准备材料", "单独整页或解读页里都行", "解读结果的一种看法", "单独的勾选整页", "解读结果页里的「准备材料（能勾选）」看法：少一个页面；没解读过的先解读（算 1 次）"),
    ("找通知", "对话里一张卡片，看全部开结果页", "结果页，条件能改；对话里先确认条件", "结果页，顶上一句检索小结",
     "首页找通知框进结果页（A）；对话里说的回卡片：条件能点着改、先列 3 份、看全部"),
    ("先做哪些", "没提", "先做带通知的对话和整页衔接，AI 汇编按 #332 随后上", "没提", "同 Codex（要你定）"),
    ("次数用完", "次数写在按钮和卡片上", "发起前写清剩几次、扣不扣", "提议分享或企业认证换次数",
     "发起前写清剩几次；不采纳分享换次数（违反微信诱导分享的规定）和企业认证（要新收企业信息）"),
]

# 审阅（第 1 轮：Codex gpt-6.1-terra、agy、独立 Claude）里没采纳的，和留到开发设计里定的
NOT_ADOPTED = [
    ("把原句里的网址换成「请在电脑端访问」（agy）", "原句要逐字，不能改；网址照原文显示成文字，不能点、不单独给复制按钮，和现在的正文一样"),
    ("去掉 AI 首页顶上的「AI 生成」条（agy）", "协议写定 AI 功能页面顶部固定显示「AI 生成」标识；改成「标「AI 生成」的内容由 AI 生成」，说清只指标出来的那部分"),
    ("问句里的企业名称在交给模型前拦下（Codex）", "号码类（信用代码、手机号、身份证号、银行卡号）服务器已经拦下、不发出不保存；企业名称认不全、拦不住，#328 定的是提醒不要写，照这个做"),
    ("额度用完用分享或企业认证换次数（agy）", "分享换次数违反微信诱导分享的规定；企业认证要新收集企业信息"),
    ("整理时「不占 5 次」会被无限套用（agy）", "整理时的新解读另有每天 30 份的上限（#332），画面上写明了"),
    ("材料清单加「导出清单」（agy）", "先不做，随 AI 汇编（出 Word）以后再定"),
    ("详情页标题改叫「通知详情」（独立 Claude）", "线上现在就叫「文件详情」，改名要全站一起改，另做"),
    ("「剩29天」「剩 29 天」两种写法、详情页「非官方分类」折行（独立 Claude）", "是线上现有组件的写法，这次不动；改组件时一起改"),
    ("开发上的约定算没解决（Codex 第 2 轮）", "这一版是定方向的效果图；回到对话怎么定位、文件怎么回写、勾选怎么存这几样，记在右边，开发设计稿里逐条写清再请 Codex 审"),
]
DEV_LATER = [
    ("回到对话", "记住对话、消息和滚动位置，消息加载完再定位；页面栈不够时重新打开对话（Codex）"),
    ("文件回到对话里", "服务器按任务编号只写一次文件消息；关页面、失败、重复点、恢复都有状态；完成提醒只在用户点了以后请求订阅（Codex）"),
    ("本机勾选", "按账号、通知、项目类别、原句和结果版本存；换账号分开，结果更新后不错勾（Codex）"),
    ("打字认不出要做什么", "让用户选「找通知」还是「问这份通知」；问到企业在不在名单里，带去查名单页（Codex）"),
    ("没看过通知的人", "「刚才看的通知」换成「先选一份通知」：近 7 天新的、关注的新文件、找通知（agy）"),
    ("加入整理以后", "页面上出现「去整理（N 份）」，点了进整理（agy）"),
    ("小标题认不出", "照原文顺序列，不硬分组（Codex）"),
    ("AI 汇编上线前", "这些都不显示：首页「整理成文件」那格和它的额度那行、页头那句「几份还能整理成一张表」、记录里的文件、问一问里的「加入整理」「整理成文件」按钮、整页底栏的「加入整理」；C6 是上线以后的画面（你 10/19 定的不给用户看没做完的功能；Codex）"),
    ("次数和失败", "按钮旁边先写剩几次；用完了按钮变灰、写原因和什么时候恢复；解读失败、额度不够、正文缺了，卡片上写原因，没做成的不算（Codex）"),
]

SAME = [
    "只从政府原文里摘原句，程序逐字核对，每句能看原文、纠错",
    "不判断能不能申报，不打分，不代写材料",
    "条款对照里填的企业情况不交给模型，只在页面上和条件并排；问一问里打的字会交给模型，号码类服务器先拦下，企业名称请用户不要写（《问一问使用说明》第 3 条）",
    "查询、正文、附件、结果都在小程序里，不跳浏览器（10/23 定）",
    "AI 生成的标「AI 生成」，页底写模型和备案号（D-098）；按规则找的不标",
    "AI 解读、条款对照、问这份通知各每天 5 次；找通知、查名单不用 AI、不算次数",
    "截止照 D-061：机器摘录的附原句、标「未经人工核对」",
    "图片第二期、联网第三期（D-078）；问一问里不另做查企业，带去查名单页（D-093）",
]

PLACES = [
    ("列表的每一行", "查询结果、关注的新文件、截止、AI 首页里能解读的通知，行上直接给「AI 解读」"),
    ("通知详情", "「要点」下面的卡片，位置不动（D-075）"),
    ("关注的新文件", "一键把新来的几份整理成对比表（A 在列表底下，B 在对话里说）"),
    ("截止", "截止前提醒走微信订阅消息，用户自己点订阅，只用人工核对过的日期"),
    ("我的客户", "客户申报线索表（D-086）：先拿 2–4 份样品表给顾问试，再做进产品"),
    ("文件", "生成的 Excel、Word 都在这里；打开、转发前再查一次通知还在不在"),
]

GAPS = [
    ("截止要有人核", "线上人工核对过的截止现在是 0 条；7 天内截止的要每天有人核，提醒和「快到期」才能用（Codex）"),
    ("附件、扫描件", "每个结果写清读了哪些、哪些没读到、没读完；扫描件先评测识字准不准（Codex、agy）"),
    ("通知改了、撤了", "旧的解读、清单、文件要跟着作废，不能拿旧句子当现在的（Codex）"),
    ("今年和去年改了哪几句", "顾问最想知道门槛变没变，要做左右对照（agy）"),
    ("一键关掉 AI", "出事时马上停用 AI，页面只留原文和规则找到的结果（agy）"),
    ("区县转发的通知", "企业多数在区县报，要接上区县的转发和受理地方（agy）"),
]

OLD = [
    ("首页搜索框下面一条「AI 助手」，不放底栏", "底栏第 5 格「AI」（D-075）"),
    ("所有事都进同一个对话", "解读、对照、问一问各有入口和结果页（D-078、D-093）；A 照这个做，B 回到对话版的做法"),
    ("详情卡片在截止时间下面、要点上面", "卡片留在「要点」下面，标题卡加一行提示（D-075）"),
    ("先一句话回答，再贴原句", "只摘原句、程序逐字核对，不写结论（D-078）"),
    ("拍照、营业执照、联网查政府网站", "第一期都不放：图片第二期、联网第三期；营业执照因为企业信息去哪先不做"),
    ("「和我的企业对一下」", "企业情况用户自己填，只在页面上和条件原句并排，不交给模型、不判断（D-086）"),
    ("复制原文链接，联网结果列外链", "正文、附件、结果都在小程序里看（10/23 审核退回后定）"),
    ("每份通知一条对话，统一存 30 天", "问一问的对话存 30 天；解读、对照照它们现在的规矩"),
    ("每天一共 5 次，材料和划重点共用一次", "解读、对照、问这份通知各每天 5 次；找通知不算次数"),
    ("只读正文", "也读附件（D-079），写明读了哪些；原文链接失效的不解读（#347）"),
]

SOURCES_NOTE = ("画面里的通知都是 10/22 从线上只读接口取回的真实收录；剩几天按 10/22 算。原句逐字取自收录的正文（不在正文里的生成脚本直接报错），"
                "解读摘哪几句、分到哪一类是示意，以实际结果为准；[ ] 是还没有的数字和日期。")


def shot(sid, title, fn, note, chips, side, rec=False, cls=None):
    tags = "".join(f'<span class="chip">{c}</span>' for c in chips)
    badge = '<span class="rec">推荐</span>' if rec else ""
    return (f'<figure class="shot" id="{sid}"><figcaption><div class="side side-{cls or side.lower()}">{side}{badge}</div><h3>{title}</h3>'
            f'<p>{note}</p><div class="chips">{tags}</div></figcaption>'
            f'<div class="frame-wrap"><div class="frame">{fn()}</div></div></figure>')


def sources():
    seen, items = set(), []
    for d, note in [(DOC, "：主例子（详情、解读、准备材料、对话里问）")] + [(d, "") for d in FIND] + [(d, "") for d in ab.RECENT_NEXT]:
        if d["url"] in seen:
            continue
        seen.add(d["url"])
        items.append(f'<li><a href="{d["url"]}" target="_blank" rel="noopener">{d["issuer"]} · {d["title"]}（{d["date"]}）</a>{note}</li>')
    return "".join(items)


def page():
    rows = []
    for no, what, sub, a, b in ROWS:
        rows.append(f'<section class="row" id="row{no}"><div class="row-head"><span class="row-no">{no}</span><h2>{what}</h2></div><p class="sub">{sub}</p>'
                    f'<div class="pair">{shot(*a, "A", rec=True)}{shot(*b, "B")}</div></section>')
    c_shots = "".join(shot(*c, "A+B 结合", cls="c") for c in C_SHOTS)
    from_a = "".join(f"<li>{t}</li>" for t in FROM_A)
    from_b = "".join(f"<li>{t}</li>" for t in FROM_B)
    merge = "".join(f'<tr><td class="q">{q}</td><td>{a}</td><td>{b}</td><td>{c}</td><td class="m">{m}</td></tr>' for q, a, b, c, m in MERGE)
    same = "".join(f"<li>{t}</li>" for t in SAME)
    not_adopted = "".join(f"<li><b>{a}</b>：{b}</li>" for a, b in NOT_ADOPTED)
    dev_later = "".join(f"<li><b>{a}</b>：{b}</li>" for a, b in DEV_LATER)
    places = "".join(f'<li><b>{a}</b><span>{b}</span></li>' for a, b in PLACES)
    gaps = "".join(f'<li><b>{a}</b><span>{b}</span></li>' for a, b in GAPS)
    old = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in OLD)
    entry = "".join(f'<tr><td class="q">{a}</td><td>{b}</td><td>{c}</td></tr>' for a, b, c in ENTRY)
    flow = "".join(
        (f'<div class="node"><b>{t}</b><span>{d}</span></div>' if i % 2 == 0
         else f'<div class="arrow" aria-hidden="true"><span class="h">{t}</span><span class="v">{d}</span></div>')
        for i, (t, d) in enumerate(FLOW))
    return f"""<title>有据 AI 升级两种做法</title>
<meta name="claude:viewport-fit-sides" content="cover">
{FONTS_LINK.replace("&amp;", "&")}
<style>
/* 评审板：先放 A+B 结合版（思路、7 屏、三方怎么合的、要定的），再放第一版 A、B 对比；画面本身是小程序的浅色界面，不随主题变，外框和文字跟随主题 */
:root {{
  --bg: #ECEFF4; --surface: #FFFFFF; --ink: #1B2130; --muted: #566072; --faint: #6B7484; --line: #D8DDE6;
  --accent: #2A5CE0; --accent-tint: #E3EBFF; --b-ink: #7A4A12; --b-tint: #FBEEDD; --c-ink: #0B6E58; --c-tint: #DFF3EC; --frame: #FFFFFF;
  --shadow: 0 1px 2px rgba(20, 30, 60, 0.06), 0 12px 32px rgba(20, 30, 60, 0.10);
  --font: 'Noto Sans SC', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #0E1116; --surface: #161A21; --ink: #E6E9EF; --muted: #A2AABA; --faint: #8C95A5; --line: #2A303B;
  --accent: #82A5FF; --accent-tint: #1B2847; --b-ink: #F2C48A; --b-tint: #3A2A17; --c-ink: #7FD9BE; --c-tint: #12302A; --frame: #2A303B;
  --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 12px 32px rgba(0,0,0,0.35); color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #0E1116; --surface: #161A21; --ink: #E6E9EF; --muted: #A2AABA; --faint: #8C95A5; --line: #2A303B;
  --accent: #82A5FF; --accent-tint: #1B2847; --b-ink: #F2C48A; --b-tint: #3A2A17; --c-ink: #7FD9BE; --c-tint: #12302A; --frame: #2A303B;
  --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 12px 32px rgba(0,0,0,0.35); color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: var(--font); font-size: 15px; line-height: 1.7;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }}
.wrap {{ max-width: 1100px; margin: 0 auto; padding-block: 40px 72px; }}
.eyebrow {{ font-size: 13px; letter-spacing: 0.08em; color: var(--muted); }}
h1 {{ margin: 6px 0 0; font-size: 34px; line-height: 1.25; font-weight: 700; text-wrap: balance; }}
.lede {{ margin: 14px 0 0; max-width: 46em; font-size: 16px; color: var(--muted); }}
.pills {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }}
.pill {{ padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line); background: var(--surface); font-size: 13px; color: var(--muted); }}
.pill b {{ color: var(--ink); font-weight: 600; }}
h2 {{ margin: 0; font-size: 22px; line-height: 1.35; font-weight: 700; text-wrap: balance; }}
h2 small {{ margin-left: 10px; font-size: 14px; font-weight: 400; color: var(--faint); }}
section {{ margin-top: 56px; }}
.sub {{ margin: 6px 0 0; color: var(--muted); font-size: 14.5px; max-width: 52em; }}
.flow {{ margin-top: 18px; display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto minmax(0, 1fr) auto minmax(0, 1fr); gap: 10px; }}
.node {{ min-width: 0; padding: 12px 14px; border-radius: 12px; background: var(--surface); border: 1px solid var(--line); display: flex; flex-direction: column; gap: 4px; }}
.node b {{ font-size: 15px; }}
.node span {{ font-size: 13px; line-height: 1.6; color: var(--muted); }}
.arrow {{ display: flex; align-items: center; justify-content: center; color: var(--c-ink); font-size: 20px; font-weight: 700; }}
.arrow .v {{ display: none; }}
.two {{ margin-top: 16px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
.opt {{ min-width: 0; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; }}
.opt h3 {{ margin: 4px 0 0; font-size: 18px; line-height: 1.4; }}
.opt p {{ margin: 8px 0 0; font-size: 14.5px; color: var(--muted); }}
ul.plain {{ margin: 10px 0 0; padding-left: 1.2em; display: grid; gap: 4px; font-size: 14.5px; }}
.side {{ display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; letter-spacing: 0.06em; font-weight: 700; }}
.side-a {{ color: var(--accent); }}
.side-b {{ color: var(--b-ink); }}
.side-c {{ color: var(--c-ink); }}
.rec {{ padding: 0 7px; border-radius: 999px; background: var(--accent-tint); color: var(--accent); font-size: 12px; letter-spacing: 0; line-height: 20px; }}
ul.list {{ margin: 16px 0 0; padding: 0; list-style: none; display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; }}
.list li {{ background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; display: flex; flex-direction: column; gap: 3px; font-size: 14px; min-width: 0; }}
.list li b {{ font-size: 15px; }}
.list li span {{ color: var(--muted); }}
ul.rules {{ margin: 14px 0 0; padding-left: 1.2em; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px 32px; font-size: 14.5px; }}
.rules li {{ min-width: 0; }}
.row-head {{ display: flex; align-items: baseline; gap: 12px; }}
.row-no {{ flex-shrink: 0; width: 28px; height: 28px; border-radius: 8px; background: var(--accent-tint); color: var(--accent); font-size: 15px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center; font-variant-numeric: tabular-nums; position: relative; top: 2px; }}
.screens {{ margin-top: 22px; display: grid; grid-template-columns: repeat(auto-fill, minmax(min(392px, 100%), 1fr)); column-gap: 36px; row-gap: 0; }}
.screens .shot {{ margin-bottom: 52px; }}
.pair {{ margin-top: 20px; display: grid; grid-template-columns: repeat(2, minmax(0, 392px)); grid-template-rows: auto auto; column-gap: 56px; row-gap: 16px; justify-content: start; }}
.shot {{ margin: 0; display: grid; grid-row: span 2; grid-template-rows: subgrid; row-gap: 16px; min-width: 0; align-content: start; }}
.shot h3 {{ margin: 2px 0 0; font-size: 18px; line-height: 1.4; }}
.shot p {{ margin: 6px 0 0; font-size: 14px; line-height: 1.7; color: var(--muted); max-width: 392px; }}
.chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; max-width: 392px; }}
.chip {{ padding: 1px 8px; border-radius: 6px; border: 1px solid var(--line); font-size: 12px; color: var(--muted); background: var(--surface); }}
.frame-wrap {{ max-width: 100%; overflow-x: auto; }}
.frame {{ width: 392px; border: 1px solid var(--frame); border-radius: 26px; overflow: hidden; box-shadow: var(--shadow); background: #F2F4F8; }}
.table-wrap {{ margin-top: 16px; overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }}
table {{ width: 100%; min-width: 640px; border-collapse: collapse; font-size: 14px; line-height: 1.6; }}
table.merge {{ min-width: 920px; font-size: 13.5px; }}
th, td {{ text-align: left; vertical-align: top; padding: 11px 14px; border-bottom: 1px solid var(--line); }}
th {{ font-size: 12.5px; font-weight: 600; color: var(--faint); letter-spacing: 0.04em; }}
td.q {{ font-weight: 600; white-space: nowrap; }}
td.m {{ background: var(--c-tint); color: var(--ink); font-weight: 500; }}
tr:last-child td {{ border-bottom: 0; }}
.decide {{ margin-top: 16px; padding: 18px 20px; border-radius: 14px; background: var(--surface); border: 1px solid var(--line); }}
.decide ol {{ margin: 0; padding-left: 1.3em; display: grid; gap: 10px; }}
.decide li b {{ font-weight: 600; }}
.decide p {{ margin: 12px 0 0; font-size: 14px; color: var(--muted); }}
.h3 {{ margin: 22px 0 0; font-size: 16px; }}
.v1 {{ margin-top: 72px; padding-top: 32px; border-top: 1px solid var(--line); }}
.sources {{ margin: 12px 0 0; padding-left: 1.2em; font-size: 13.5px; color: var(--muted); }}
.sources a {{ color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }}
.sources a:hover, .sources a:focus-visible {{ text-decoration: underline; }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }}
.foot {{ margin-top: 40px; font-size: 13px; color: var(--faint); overflow-wrap: anywhere; }}
@media (max-width: 900px) {{
  h1 {{ font-size: 28px; }}
  .two, ul.rules {{ grid-template-columns: minmax(0, 1fr); }}
  .flow {{ grid-template-columns: minmax(0, 1fr); gap: 6px; }}
  .arrow .h {{ display: none; }}
  .arrow .v {{ display: inline; }}
  .pair {{ grid-template-columns: minmax(0, 392px); grid-template-rows: none; row-gap: 40px; }}
  .shot {{ grid-row: auto; grid-template-rows: auto auto; }}
}}
</style>
<div class="wrap">
<header>
<div class="eyebrow">有据 · AI 升级 · 效果图 · 2026-10-29 第二版</div>
<h1>AI 升级：A+B 结合</h1>
<p class="lede">你看完 A、B 两种做法以后说 A+B 结合效果最好。Claude、Codex、agy 先拿同一份材料各自独立设计了结合的方案，再一条条合成了下面这一版：
说一句就能开始（B），结果先给、长结果整页看（A）。结合版 7 屏在前面，上午那版 A、B 对比放在后面。</p>
<div class="pills"><span class="pill"><b>只出效果图和说明</b>，没改代码、没动线上</span>
<span class="pill">通知是 10/22 线上真实收录的，剩几天按 10/22 算</span>
<span class="pill">原句逐字取自正文；解读摘哪几句是示意</span><span class="pill">[ ] 是还没有的数字和日期</span></div>
</header>

<section>
<h2>结合的思路</h2>
<p class="sub">对话当入口和线索，整页当交付物。从哪进来都能一句话开始；结果在对话里先给要紧的几行，长的打开整页看、核对、勾选、出文件，看完回到同一条对话接着问。</p>
<div class="flow">{flow}</div>
<h3 class="h3">每个按钮去哪</h3>
<div class="table-wrap"><table><thead><tr><th>在哪</th><th>点什么</th><th>去哪</th></tr></thead><tbody>{entry}</tbody></table></div>
<div class="two">
<div class="opt"><div class="side side-a">从 A 拿的</div><ul class="plain">{from_a}</ul></div>
<div class="opt"><div class="side side-b">从 B 拿的</div><ul class="plain">{from_b}</ul></div>
</div>
</section>

<section>
<h2>结合版效果图</h2>
<p class="sub">主例子是湖北科技厅那份通知；找通知、整理成文件用的是 10/22 线上找到的 5 份 5G 工厂申报。</p>
<div class="screens">{c_shots}</div>
</section>

<section>
<h2>三方怎么合的</h2>
<p class="sub">三方拿同一份材料各自写方案（Codex 用 gpt-6.1-terra），互相没看过，再逐条合。最右一列是合成后的做法。</p>
<div class="table-wrap"><table class="merge"><thead><tr><th>问题</th><th>Claude</th><th>Codex</th><th>agy</th><th>合成后</th></tr></thead><tbody>{merge}</tbody></table></div>
</section>

<section>
<h2>要你定的</h2>
<div class="decide"><ol>
<li><b>先做哪些。</b>建议先做问一问对话页（带着通知问）、对话和整页的衔接、材料清单看法；AI 汇编（出 Excel、Word）按 #332 的开发设计随后上（Codex 的建议）。</li>
</ol>
<p>首页不放提问框（常问问题点了就进问一问），D-075 第四节不用改。不影响这次提审：带 AI 提审还是现在的 AI 解读、条款对照（D-098）。你定了以后，出完整的设计稿和开发计划。</p></div>
</section>

<section>
<h2>审阅怎么处理的<small>agy 3 轮、Codex 5 轮、独立 Claude 4 轮，最后都是「剩余必须改：无」</small></h2>
<p class="sub">画面上能改的都改了：首页不放提问框；每个按钮去哪列成表；推荐或审核截止列出摘到的原句；次数写全（找到原句才算 1 次，另外每天最多问 20 句、找没找到都算）；准备材料什么时候算次数；点按钮做的事在对话里标出来；「这句摘错了」统一；页底写模型和备案号；文件里的出处照 #332 写。下面是没采纳的和留到开发设计里定的。</p>
<div class="two">
<div class="opt"><h3>没采纳的</h3><ul class="plain">{not_adopted}</ul></div>
<div class="opt"><h3>开发设计里定的</h3><ul class="plain">{dev_later}</ul></div>
</div>
</section>

<section>
<h2>结合版和两种做法都守的规矩</h2>
<ul class="rules">{same}</ul>
</section>

<section class="v1">
<h2>第一版：A、B 两种做法<small>10/29 上午给你看的</small></h2>
<div class="two">
<div class="opt"><div class="side side-a">A</div><h3>任务和结果首页</h3>
<p>底栏「AI」进来，先给能直接看的结果和要办的事；找通知的输入框按规则找。AI 解读、准备材料、整理成文件各有自己的页面；对话只用来接着问一份通知。</p></div>
<div class="opt"><div class="side side-b">B</div><h3>全做成对话页</h3>
<p>底栏「AI」进来就是一个对话页，找通知、解读、准备材料、整理成文件都在这一条对话里做，像 10/17 的对话版。</p></div>
</div>
</section>

{"".join(rows)}

<section>
<h2>AI 放进哪些地方</h2>
<p class="sub">两种做法都做。让 AI 出现在用户本来就在看的地方，不用专门去找。</p>
<ul class="list">{places}</ul>
</section>

<section>
<h2>两种做法都还缺的</h2>
<ul class="list">{gaps}</ul>
<p class="sub">没采纳的：agy 建议导出的文件「去掉 AI 痕迹」。国家标准要求导出的 AI 生成内容带显式和隐式标识（GB 45438），协议里也这样写了，所以不做。</p>
</section>

<section>
<h2>10/17 对话版里已经过时的地方</h2>
<p class="sub">两种做法都按右边一列改过了。</p>
<div class="table-wrap"><table><thead><tr><th>10/17 对话版</th><th>现在定的</th></tr></thead><tbody>{old}</tbody></table></div>
</section>

<section>
<h2>画面里用到的真实数据</h2>
<p class="sub">{SOURCES_NOTE}</p>
<ul class="sources">{sources()}</ul>
</section>
<p class="foot">生成脚本：docs/设计/AI板块原型/生成脚本/ai_v2.py · 效果图：docs/设计/AI板块原型/效果图_AI升级/ · 分支 claude/ai-assistant-v2-1029（没合并）</p>
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


if __name__ == "__main__":
    out_board = Path(sys.argv[1])
    st_dir = Path(sys.argv[2])
    st_dir.mkdir(parents=True, exist_ok=True)
    out_board.parent.mkdir(parents=True, exist_ok=True)
    out_board.write_text(page(), encoding="utf-8")
    for sid, title, fn, *_ in SCREENS + C_SHOTS:
        (st_dir / f"{sid}.html").write_text(ab.static_page(fn), encoding="utf-8")
        print(sid, title)
