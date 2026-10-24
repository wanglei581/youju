"""「有据」AI 板块原型（2026-10-22）：按 D-075、D-078 画"当前版本"和"下一版"两组手机画面，和给创始人看的看板。

- 当前版本：手上的体验版（main），照小程序现在的代码画，样子在开发者工具里对过（10/22）。
- 下一版：问一问第一期（D-078），界面文字照开发设计 #328 第十二节；AI 首页两行能点是推进计划第 0 步第 1 处。
沿用原型 v2 的规范（../../原型v2/生成脚本/ds.py）和 #278 的做法：每屏一段 HTML，看板里直接放画面，
单屏页面交给 render.sh 用无界面 Chrome 出效果图。图标只用小程序本地图标集里有的（tools/icons/icons.txt），
用了表外的图标直接报错。
数据：data_1022.json，10/22 从线上只读接口取回的真实收录（只留公开字段）。原句都按收录的正文逐字核对
（不在正文里的直接报错）；解读、问答回答里摘哪几句是示意，以实际结果为准；[ ] 是还没有的内容。

用法：python3 ai_board.py <看板 HTML> <单屏 HTML 目录>
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "原型v2" / "生成脚本"))

import ds  # noqa: E402
from ds import (BG, BLUE, BLUE_G1, BLUE_G2, BLUE_TINT, CARD, FILL, FONT, FONTS_LINK, LINE, LINE2, O_TINT, O_TXT,  # noqa: E402
                RED, T1, T2, T3, capsule)

# ---------- 图标：只许用小程序本地图标集里有的 ----------
ICONS_TXT = HERE.parents[3] / "tools" / "icons" / "icons.txt"
ALLOWED = {line.split("#")[0].strip() for line in ICONS_TXT.read_text(encoding="utf-8").splitlines()} - {""}
# 详情页底栏"转发"、对话页"删除这条对话"、说明收起；SVG 10/22 补进原型 v2 的图标目录（tdesign-icons-svg@0.4.7）
ds.LIB_ICONS.update({"share": "share", "delete": "delete", "up": "chevron-up"})


def icon(name, size=18, color=T1, filled=False):
    file = ds.LIB_FILLED[name] if filled else ds.LIB_ICONS[name]
    if file not in ALLOWED:
        raise KeyError(f"图标 {file} 不在小程序本地图标集（tools/icons/icons.txt）里")
    return ds.icon(name, size, color, filled=filled)


QUOTE_BG = "#F7F8FA"   # 原文原句的底色：和结果页、详情页"原文摘录"一样
BTN_GREY = "#F2F2F2"   # 微信 style v2 默认按钮的底色（结果页"开始对照"）

# ---------- 真实数据（data_1022.json） ----------
DATA = json.loads((HERE / "data_1022.json").read_text(encoding="utf-8"))
RECENT = DATA["recent"]["items"]              # AI 首页"近 7 天能解读的申报通知"，共 DATA["recent"]["total"] 份
# 下一版：宁夏那份（74619）原网站 10/22 撤稿，照 #347 原文链接失效的不再给 AI 解读，也就不在这个列表里（Codex 10/24）
RECENT_NEXT = [d for d in RECENT if d["id"] != DATA["withdrawn"]["id"]]
FIND = DATA["find"]["items"]                  # 找通知例子：近 30 天、申报通知、标题里有 5G工厂
# 主例子：湖北省科学技术厅《……企业自主研发和委托研发项目纳入省级科技计划项目的通知》（能解读、没有附件，企业申报截止 11/20）。
# 原来选的宁夏《技术交易补助》那份 10/22 下午原网站撤稿了（data_1022.json 的 withdrawn），换成这份
DOC = DATA["doc70955"]
NO_BODY = DATA["doc71229"]                    # 不能解读：没有完整正文（重庆 5G 工厂）
KIND_DOC = DATA["kind_example"]["items"][0]   # 不能解读：资讯与指南（广东研修班通知）
LOOKUP = DATA["lookup"]                       # 查名单例子
LIST_DOC = LOOKUP["items"][0]                 # 名单（新疆天山杯获奖名单）
NONE_LOOKUP = DATA["lookup_none"]             # 查名单没查到的例子
BODY = DOC["body"]["text"]


def verbatim(text):
    """原型里的原句必须是收录正文里的一句，抄错了直接报错。"""
    if text not in BODY:
        raise ValueError(f"原句不在收录的正文里：{text}")
    return text


# AI 解读结果、问答回答（示意）：原句逐字取自收录的正文（按行取整句）；分到哪一类、摘哪几句是按原文推断的，以实际结果为准
Q_OBJ1 = verbatim("湖北省内企业自筹资金、自行立项、自主实施（不是委托外部科研单位）的研发项目，且企业自主研发项目研发费用应达500万元（含）以上。")
Q_OBJ2 = verbatim("湖北省内的高等学校、科研机构（含新型研发机构）、医疗卫生机构等单位（以下简称科研单位）承担我省企业委托的研发项目，"
                  "且单个项目横向经费实际到账金额须达200万元（含）以上，农业领域项目实际到账金额须达100万元（含）以上。")
Q_C0 = verbatim("1．企业自主研发项目纳入省级科技计划项目应同时满足以下全部条件：")
Q_C1 = verbatim("（1）企业为研发项目的牵头实施单位，注册地在湖北省内。研发项目围绕解决企业或产业关键技术问题进行研究攻关，形成重大科技成果，且未获得国家和省级科技计划项目支持；")
Q_C2 = verbatim("（2）单个研发项目企业自筹资金应达500万元（含）以上，项目研发费用管理应按照《研发费用加计扣除政策执行指引（2.0版）》执行，项目经费须单独建账、独立核算；")
Q_C3 = verbatim("（3）申报项目立项时间为2023年1月1日以后，研发项目应在2026年度申报前已完成验收并合格，项目验收可由企业根据内部验收标准自行组织验收或委托第三方验收；")
Q_C4 = verbatim("（4）企业没有逾期未验收的省级科技计划项目，项目负责人应为申报单位的在职人员，项目负责人、项目牵头单位和项目参与单位均未在科研违规失信惩戒期内。")
Q_LIMIT = verbatim("1．每个项目负责人每年申报1项，每家企业每年申报不超过3项；")
Q_T1 = verbatim("2026年度首轮集中评审，申报单位网上申报截止时间为2026年11月20日17时，逾期系统将自动关闭无法提交材料。")
Q_T2 = verbatim("推荐单位网上审核推荐截止时间为2026年11月23日17时。")
Q_PUB = verbatim("经省科技厅党组审议决策后，对拟纳入省级科技计划项目的进行公示，公示期为5个工作日，公示无异议的项目确定项目编号并发文公布。")
Q_W1 = verbatim("企业自主研发项目由企业牵头申报，企业委托研发类项目限受托的科研单位申报，依托“湖北省科技计划项目管理平台”（https://kjt.hubei.gov.cn/jhgl）进行申报。")
Q_W2 = verbatim("（二）征集工作全过程在湖北省科技计划项目管理信息系统内完成，无需提交纸质材料。")
Q_M1 = verbatim("1．企业自主研发项目纳入省级科技计划申请书；")
Q_M3 = verbatim("3．项目研发费用相关财务证明材料，如项目研发费用专项审计报告、税务部门核定的研发费用加计扣除相关证明材料、项目研发支出辅助账等；")

TODAY = "2026-10-22"
DISCLOSURE = "模型：[模型名称]｜备案号：[备案号]"


# ---------- 小程序现有的部件（尺寸按 750rpx = 390 宽取一半，同原型 v2） ----------
def phone(top, bottom="", min_h=844):
    return (f'<div class="yj-phone" style="width: 390px; min-height: {min_h}px; display: flex; flex-direction: column; background: {BG}; color: {T1}; '
            f'font-family: {FONT}; font-size: 14px; line-height: 1.5; font-variant-numeric: tabular-nums; position: relative; overflow: hidden; text-align: left">'
            f'{top}<div style="flex-grow: 1; min-height: 16px"></div>{bottom}</div>')


def first_screen(top, bottom, h=844):
    """只看页面上面一段（默认手机第一屏 844 高）：下面的内容截掉，底部操作栏固定在最下面。"""
    return (f'<div class="yj-phone" style="width: 390px; height: {h}px; display: flex; flex-direction: column; background: {BG}; color: {T1}; '
            f'font-family: {FONT}; font-size: 14px; line-height: 1.5; font-variant-numeric: tabular-nums; position: relative; overflow: hidden; text-align: left">'
            f'<div style="flex: 1; min-height: 0; overflow: hidden">{top}</div>{bottom}</div>')


def sheet(top):
    """几种情况放一起看的说明页：手机宽，高度随内容。"""
    return phone(top + '<div style="height: 20px"></div>', min_h=0)


def nav(title):
    """系统导航栏：白底、左上返回、右上胶囊。"""
    return (f'<header style="height: 48px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; position: relative; background: {CARD}">'
            f'<span aria-hidden="true" style="position: absolute; left: 6px; top: 4px; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center">{icon("back", 22, T1)}</span>'
            f'<span style="font-size: 17px; font-weight: 500">{title}</span>{capsule()}</header>')


def notice_bar(on_blue=False, nextv=False):
    """全站统一的出处说明（notice-bar 组件）。下一版：站内阅读以后「每条附原文链接」容易被看成要去外链
    （agy、Grok 10/23 都提了），画成产品原来页脚的说法「每条都有出处」；全站组件，最后跟 #349 定。"""
    color = "rgba(255,255,255,0.82)" if on_blue else T3
    tail = "每条都有出处" if nextv else "每条附原文链接"
    return (f'<div style="height: 24px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; gap: 4px; font-size: 11.5px; color: {color}">'
            f'{icon("shield", 12, color)}第三方整理 · 非政府平台 · {tail}</div>')


def card(inner, extra=""):
    return f'<div style="margin: 10px 12px 0; padding: 0 16px; background: {CARD}; border-radius: 12px; {extra}">{inner}</div>'


def section_title(text, right=""):
    return (f'<div style="display: flex; align-items: center; justify-content: space-between; padding: 14px 0 4px; font-size: 16px; font-weight: 600">'
            f'<span>{text}</span>{right}</div>')


def btn_primary(text, mt=12):
    return (f'<div style="margin-top: {mt}px; height: 44px; display: flex; align-items: center; justify-content: center; border-radius: 22px; '
            f'background: {BLUE}; color: #FFFFFF; font-size: 15px; font-weight: 500">{text}</div>')


def btn_default(text, mt=12):
    """微信 style v2 的默认按钮：宽 184、居中、浅灰底粗体（结果页"开始对照"在真机上就是这样）。"""
    return (f'<div style="margin: {mt}px auto 0; width: 184px; height: 40px; display: flex; align-items: center; justify-content: center; '
            f'border-radius: 5px; background: {BTN_GREY}; color: {T1}; font-size: 15px; font-weight: 700">{text}</div>')


def text_btn(text, mt=0):
    """结果页的"看原文""这句摘错了"：微信默认按钮去掉底色后居中显示的灰字。"""
    return f'<div style="margin-top: {mt}px; padding: 8px 0; text-align: center; font-size: 13px; font-weight: 600; color: {T2}">{text}</div>'


def tag(kind):
    c = {"申报通知": (BLUE_TINT, BLUE), "名单": (O_TINT, O_TXT), "资讯与指南": (FILL, T2)}[kind]
    return (f'<span style="padding: 0 6px; line-height: 20px; border-radius: 4px; background: {c[0]}; color: {c[1]}; font-size: 12px; '
            f'font-weight: 500; white-space: nowrap">{kind}</span>')


def status_tag(text):
    """名单状态（只照录标题用词）：公示 = 橙色虚线框，公布 = 灰色实线框。"""
    st = f"border: 1px dashed {O_TXT}; color: {O_TXT}" if text == "公示" else f"border: 1px solid {T2}; color: {T1}"
    return f'<span style="padding: 0 6px; line-height: 18px; border-radius: 4px; {st}; font-size: 12px; white-space: nowrap">{text}</span>'


def chip(text):
    """列表行、刚才看的通知上的小胶囊按钮（ai-button 组件）。"""
    return (f'<span style="flex-shrink: 0; padding: 3px 10px; border-radius: 999px; background: {BLUE_TINT}; color: {BLUE}; '
            f'font-size: 12px; line-height: 1.5; font-weight: 600; white-space: nowrap">{text}</span>')


def copy_link(size=12.5):
    return (f'<span style="margin-left: auto; flex-shrink: 0; display: inline-flex; align-items: center; gap: 3px; color: {BLUE}; '
            f'font-size: {size}px; white-space: nowrap">{icon("copy", 14, BLUE)}复制原文链接</span>')


def detail_link(size=12.5):
    """下一版的列表行（10/23 创始人定：查询、正文、附件、结果都在小程序里完成，不让用户复制网址去浏览器）：
    照 #349 把"复制原文链接"换成"查看详情"，图标不再用复制的。"""
    return (f'<span style="margin-left: auto; flex-shrink: 0; display: inline-flex; align-items: center; gap: 1px; color: {BLUE}; '
            f'font-size: {size}px; white-space: nowrap">查看详情{icon("chev", 14, BLUE)}</span>')


def month_day(iso):
    return f"{int(iso[5:7])}月{int(iso[8:10])}日"


def due_view(d, role=False):
    """列表行右上角的截止（D-061，同 utils/format.js 的 dueView）：机器摘录的写日期并附原句，其余只写一句。
    role=True（找通知的结果）照"截止"页的写法带上是谁的截止，如"企业申报截止 10月30日"（D-078 第五节）。"""
    due = d.get("due")
    if due and due.get("date") and due.get("source") in ("excerpt", "verified"):
        days = due.get("remainingDays")
        if due["source"] == "verified" or (due.get("quote") and 8 <= days <= 30):
            text = (f"{due.get('kind') or '申报截止'} {month_day(due['date'])} · 剩{days}天" if role
                    else f"{month_day(due['date'])}截止 · 剩{days}天")
            excerpt = due["quote"] if due["source"] == "excerpt" else ""
            return text, excerpt
    return ("截止未核对，请以原文为准" if d.get("dueSoonUnverified") else "截止时间见原文"), ""


def doc_row(d, button=None, last=False, role=False, inapp=False):
    """列表行（doc-row 组件）：类型、地区、截止 → 标题 → 机器摘录的原句 → 发文机关 → 日期、复制原文链接、按钮。
    行上有按钮时发文机关单独一行（真机上就是这样，给日期、复制链接和按钮腾地方）。"""
    right, excerpt = due_view(d, role)
    head = (f'<div style="display: flex; flex-wrap: wrap; align-items: center; gap: 4px 6px">{tag(d["kind"])}'
            f'<span style="font-size: 12px; color: {T2}; white-space: nowrap">{d["region"]}</span>'
            f'<span style="margin-left: auto; font-size: 12px; color: {T2}; white-space: nowrap">{right}</span></div>')
    ex = ""
    if excerpt:
        ex = (f'<div style="margin-top: 4px; display: flex; flex-direction: column; gap: 2px">'
              f'<span style="align-self: flex-start; padding: 1px 5px; border-radius: 3px; background: {FILL}; color: {T2}; font-size: 11px">机器摘录·未经人工核对</span>'
              f'<span style="font-size: 12px; line-height: 1.6; color: {T2}; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden">原文：{excerpt}</span></div>')
    own_line = bool(button) or len(d["issuer"]) > 11
    issuer = f'<div style="margin-top: 7px; font-size: 12.5px; line-height: 1.6; color: {T2}">{d["issuer"]}</div>' if own_line else ""
    who = d["date"] if own_line else f'{d["issuer"]} · {d["date"]}'
    meta = (f'<div style="margin-top: {2 if own_line else 7}px; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; font-size: 12.5px; color: {T2}">'
            f'<span style="white-space: nowrap">{who}</span>{detail_link() if inapp else copy_link()}{chip(button) if button else ""}</div>')
    border = "" if last else f"border-bottom: 1px solid {LINE};"
    return (f'<div style="padding: 14px 0 13px; {border}">{head}'
            f'<div style="margin-top: 7px; font-size: 16px; font-weight: 600; line-height: 24px">{d["title"]}</div>{ex}{issuer}{meta}</div>')


STAR = "polygon(50% 4%, 61% 39%, 96% 50%, 61% 61%, 50% 96%, 39% 61%, 4% 50%, 39% 39%)"


def spark(on, color):
    """底栏 AI 那一格的四角星（和小程序一样是样式画的）：未选中空心，选中实心。"""
    inner = ("" if on else f'<span style="position: absolute; left: 5.5px; top: 5.5px; width: 11px; height: 11px; background: #FFFFFF; '
                           f'clip-path: {STAR}; -webkit-clip-path: {STAR}"></span>')
    return (f'<span aria-hidden="true" style="position: relative; width: 22px; height: 22px; margin: 1px; background: {color}; '
            f'clip-path: {STAR}; -webkit-clip-path: {STAR}">{inner}</span>')


def tabbar(active="AI"):
    """自定义底栏（D-075）：AI 对这个人开着时 5 格，AI 在中间。"""
    out = []
    for label, key in (("首页", "home"), ("关注", "bell"), ("AI", None), ("文件", "doc"), ("我的", "user")):
        on = label == active
        color = BLUE if on else T3
        ic = spark(on, color) if key is None else icon(key, 24, color, filled=on)
        out.append(f'<div style="display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; color: {color}; '
                   f'font-size: 11px; {"font-weight: 600;" if on else ""}">{ic}<span>{label}</span></div>')
    return (f'<div style="flex-shrink: 0; height: 84px; box-sizing: border-box; padding-bottom: 30px; background: {CARD}; border-top: 1px solid {LINE}; '
            f'display: grid; grid-template-columns: repeat(5, minmax(0, 1fr))">{"".join(out)}</div>')


def contact_block():
    """联系客服（contact-button 组件）：整行描边按钮，下面两行说明。"""
    return (f'<div style="margin-top: 12px; min-height: 44px; display: flex; align-items: center; justify-content: center; border-radius: 8px; '
            f'border: 1px solid {LINE2}; color: {BLUE}; font-size: 15px; font-weight: 600">联系客服</div>'
            f'<div style="margin-top: 6px; text-align: center; font-size: 12px; line-height: 1.6; color: {T2}">一般当天或第二天回复</div>'
            f'<div style="margin-top: 6px; text-align: center; font-size: 12px; line-height: 1.6; color: {T2}">客服只解答小程序怎么用，不代办申报、不判断能不能报</div>')


# ---------- AI 首页 ----------
AI_HERO = "在通知原文里找出申报对象、条件、时间和材料，每句都能回原文核对；不判断能不能申报。"


def ai_top(nextv=False):
    """AI 首页是自定义导航：品牌蓝页头里写"AI"，出处说明条和一句话说明也在蓝底上。"""
    return (f'<div style="flex-shrink: 0; background: linear-gradient(180deg, {BLUE_G1} 0%, {BLUE_G2} 100%); color: #FFFFFF; padding: 0 16px 36px">'
            f'<div style="height: 48px; position: relative; display: flex; align-items: center; justify-content: center">'
            f'<span style="font-size: 17px; font-weight: 600">AI</span>{capsule().replace("right: 10px", "right: -6px")}</div>'
            f'{notice_bar(on_blue=True, nextv=nextv)}'
            f'<div style="margin-top: 8px; font-size: 14px; line-height: 1.7; color: rgba(255,255,255,0.92)">{AI_HERO}</div></div>')


def ai_strip():
    """压在页头下沿的固定说明条：显式"AI 生成"，不能关；"说明"展开适用人群、场合、用途、不适用、保存多久。"""
    return (f'<div style="position: relative; margin: -20px 12px 0; padding: 0 14px; min-height: 44px; display: flex; align-items: center; '
            f'justify-content: space-between; background: {CARD}; border-radius: 12px; box-shadow: 0 2px 10px rgba(29,33,41,0.05)">'
            f'<span style="font-size: 13px; font-weight: 600">AI 生成 · 以原文为准</span><span style="font-size: 13px; color: {BLUE}">说明</span></div>')


def ability(name, desc, right, sub="", arrow=False, last=False):
    a = icon("chev", 16, T3) if arrow else ""
    s = f'<div style="margin-top: 2px; font-size: 12px; line-height: 1.6; color: {T3}">{sub}</div>' if sub else ""
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    return (f'<div style="padding: 12px 0; display: flex; align-items: center; gap: 8px; {b}">'
            f'<div style="flex: 1; min-width: 0"><div style="font-size: 16px; font-weight: 600">{name}</div>'
            f'<div style="margin-top: 3px; font-size: 12.5px; line-height: 1.6; color: {T2}">{desc}</div>{s}</div>'
            f'<div style="flex-shrink: 0; text-align: right; font-size: 12.5px; color: {T2}; white-space: nowrap">{right}</div>{a}</div>')


def left(n, prefix=""):
    p = f'<div style="font-size: 11.5px; color: {T3}">{prefix}</div>' if prefix else ""
    return f'{p}<div>今天还剩 <span style="font-size: 16px; font-weight: 700; color: {BLUE}">{n}</span> 次</div>'


def find_button():
    return (f'<div style="margin-top: 14px; height: 40px; display: flex; align-items: center; justify-content: center; border: 1px solid {BLUE}; '
            f'border-radius: 20px; background: {BLUE_TINT}; color: {BLUE}; font-size: 14px; font-weight: 500">去找一份通知</div>')


def card_head(title, more=""):
    m = f'<span style="font-size: 13px; color: {BLUE}">{more}</span>' if more else ""
    return (f'<div style="min-height: 44px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid {LINE}">'
            f'<span style="font-size: 15px; font-weight: 600">{title}</span>{m}</div>')


def record(title, meta, last=False):
    b = "" if last else f"border-bottom: 1px solid {LINE};"
    return (f'<div style="padding: 12px 0; display: flex; align-items: center; gap: 8px; {b}"><div style="flex: 1; min-width: 0">'
            f'<div style="font-size: 14px; line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden">{title}</div>'
            f'<div style="margin-top: 3px; font-size: 12px; color: {T3}">{meta}</div></div>{icon("chev", 16, T3)}</div>')


def ai_home(nextv):
    if nextv:
        # 下一版：第三行"问一问"（#328 第十二节第 4 条）；三行能点（第 0 步第 1 处）；对照那一行的说明按 D-078 第五节改
        rows = (ability("AI 解读", "把一份通知的原句分好类，约半分钟", left(5), arrow=True)
                + ability("条款对照", "把条件和企业情况摆在一起看", left(5), arrow=True)
                + ability("问一问", "找通知、查名单；或就一份通知提问，AI 只摘原句", left(5, "问这份通知"), arrow=True, last=True)
                # agy 第 2 轮：三行都写"还剩 5 次"，看不出各算各的
                + f'<div style="margin-top: 2px; font-size: 12px; line-height: 1.6; color: {T3}">三项的次数各算各的，每天 0 点恢复；找通知、查名单不限次。</div>')
    else:
        rows = (ability("AI 解读", "把一份通知的原句分好类，约半分钟", left(5))
                + ability("条款对照", "申报条件和一家企业的情况并排列出", left(5), last=True))
    abilities = (f'<div style="margin: 10px 12px 0; padding: 6px 16px 14px; background: {CARD}; border-radius: 12px">'
                 f'{rows}{find_button()}</div>')
    ops = chip("AI 解读") + f'<span style="font-size: 13px; color: {BLUE}">条款对照 ›</span>'
    if nextv:
        ops += f'<span style="font-size: 13px; color: {BLUE}">问这份通知 ›</span>'
    last_doc = card(card_head("刚才看的通知")
                    + f'<div style="padding: 12px 0 4px"><div style="font-size: 15px; font-weight: 600; line-height: 1.6">{DOC["title"]}</div>'
                    f'<div style="margin-top: 4px; font-size: 12.5px; color: {T2}">{DOC["issuer"]} · {DOC["date"]}</div></div>'
                    f'<div style="padding: 8px 0 14px; display: flex; align-items: center; gap: 16px">{ops}</div>')
    recent_docs = RECENT_NEXT if nextv else RECENT
    rows = "".join(doc_row(d, "AI 解读", last=i == len(recent_docs) - 1, inapp=nextv) for i, d in enumerate(recent_docs))
    recent = card(card_head("近 7 天能解读的申报通知", "全部")
                  + f'<div style="padding-top: 8px; font-size: 12px; color: {T3}">点标题看通知全文，点“AI 解读”直接开始</div>{rows}')
    if nextv:
        recs = (record(f"问一问 · {DOC['title']}", "[日期] · 2 问")
                + record("问一问 · 最近一问（查名单）：中国电信股份有限公司新疆分公司", "[日期] · 3 问")
                + record(f"AI 解读 · {DOC['title']}", "[日期] · 已完成", last=True))
    else:
        recs = (record(f"AI 解读 · {DOC['title']}", "[日期] · 已完成")
                + record(f"条款对照 · {DOC['title']}", "[日期] · 已完成", last=True))
    records = card(card_head("我的 AI 记录", "全部") + recs)
    helpc = card(card_head("用的时候遇到问题？") + contact_block()
                 + f'<div style="margin-top: 12px; padding-bottom: 14px; display: flex; justify-content: center; gap: 32px; font-size: 13px; color: {BLUE}">'
                 f'<span>使用帮助 ›</span><span>投诉举报 ›</span></div>')
    # 模型名称和备案号：现在没配，页面上不显示这一行；上线时必须有（上线清单），下一版画上占位
    # 页脚原来那行「有据 · 第三方整理 · 每条都有出处」#339（10/22 合并）去掉了：出处说明只在顶部写一次
    foot = (f'<div style="padding: 18px 12px 0; text-align: center; font-size: 12px; line-height: 1.8; color: {T3}">{DISCLOSURE}</div>' if nextv else "")
    return phone(ai_top(nextv) + ai_strip() + abilities + last_doc + recent + records + helpc + foot + '<div style="height: 20px"></div>', tabbar())


# ---------- 通知详情 ----------
def detail_head():
    tags = (tag("申报通知") + f'<span style="padding: 0 6px; line-height: 18px; border-radius: 4px; border: 1px solid {LINE2}; color: {T2}; font-size: 12px; '
            f'white-space: nowrap">{DOC["category"]}</span><span style="font-size: 12px; color: {T2}; line-height: 20px">{DOC["region"]}</span>')
    return card(f'<div style="padding: 16px 0">'
                f'<div style="display: flex; flex-wrap: wrap; align-items: center; gap: 6px">{tags}</div>'
                f'<div style="margin-top: 10px; font-size: 20px; line-height: 30px; font-weight: 700">{DOC["title"]}</div>'
                f'<div style="margin-top: 10px; font-size: 13px; color: {T2}">{DOC["issuer"]} · {DOC["date"]} 发布</div>'
                f'<div style="margin-top: 8px; padding: 2px 0; font-size: 13px; color: {BLUE}">这份通知可以 AI 解读 ↓</div>'
                f'<div style="margin-top: 6px; font-size: 12px; line-height: 1.6; color: {T3}">类型、类别和名单状态按标题归类，仅供检索参考，非官方分类</div></div>')


WEEK = "一二三四五六日"


def cn_date(iso):
    return f"{int(iso[:4])}年{int(iso[5:7])}月{int(iso[8:10])}日"


def week_of(iso):
    from datetime import date
    return "周" + WEEK[date(int(iso[:4]), int(iso[5:7]), int(iso[8:10])).weekday()]


def detail_due():
    """截止时间卡片（D-061，同 pages/detail 的 deadlineRows）：每个类型一行；机器摘录的（8–30 天到期）写日期、
    标"机器摘录·未经人工核对"并附原句；其余写"截止未核对，请以原文为准"。"""
    rows = []
    for i, d in enumerate(DOC["deadlines"]):
        line = f"margin-top: 10px; border-top: 1px solid {LINE};" if i else ""
        if d.get("source") == "excerpt" and d.get("date") and d.get("quote") and 8 <= d.get("remainingDays", 0) <= 30:
            when = week_of(d["date"]) + (f" {d['time']}" if d.get("time") else "")
            rows.append(f'<div style="padding-top: 10px; display: flex; flex-direction: column; gap: 5px; {line}">'
                        f'<div style="display: flex; align-items: flex-end; justify-content: space-between"><div>'
                        f'<div style="font-size: 12px; color: {T2}">{d["kind"]}</div>'
                        f'<div style="margin-top: 2px; display: flex; align-items: baseline; gap: 8px"><span style="font-size: 24px; font-weight: 700; line-height: 1.25">{cn_date(d["date"])}</span>'
                        f'<span style="font-size: 13px; color: {T2}">{when}</span></div></div>'
                        f'<span style="height: 26px; padding: 0 10px; display: inline-flex; align-items: center; border-radius: 13px; background: {BLUE_TINT}; color: {BLUE}; '
                        f'font-size: 13px; font-weight: 600">剩 {d["remainingDays"]} 天</span></div>'
                        f'<span style="align-self: flex-start; padding: 2px 6px; border-radius: 4px; background: {FILL}; color: {T2}; font-size: 11px; font-weight: 600">机器摘录·未经人工核对</span>'
                        f'<div style="padding: 6px 8px; border-radius: 5px; background: {FILL}; font-size: 13px; line-height: 1.7; color: {T2}">原文：{d["quote"]}</div></div>')
        else:
            rows.append(f'<div style="padding-top: 10px; {line}"><div style="font-size: 12px; color: {T2}">{d["kind"]}</div>'
                        f'<div style="margin-top: 4px; font-size: 15px">截止未核对，请以原文为准</div></div>')
    return card(f'<div style="padding: 16px 0"><div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px">'
                f'<span style="font-size: 12px; color: {T2}">截止时间</span><span style="font-size: 12px; color: {T3}">以原文为准</span></div>{"".join(rows)}</div>')


def detail_points():
    rows = DOC["points"]
    kv = "".join(f'<div style="display: flex; gap: 12px; padding: 12px 0 {16 if i == len(rows) - 1 else 12}px; '
                 f'{"" if i == len(rows) - 1 else f"border-bottom: 1px solid {LINE};"} font-size: 14px; line-height: 1.6">'
                 f'<span style="flex-shrink: 0; min-width: 72px; color: {T3}">{k}</span><span style="flex: 1">{v}</span></div>' for i, (k, v) in enumerate(rows))
    return card(section_title("要点") + kv)


def ai_card(nextv):
    """"要点"下面的 AI 解读卡片（ai-entry 组件），位置不动（创始人 10/17 定）。下一版在"开始解读"下面加"问这份通知"。"""
    if nextv:
        links = (f'<div style="margin-top: 10px; padding: 4px 0; display: flex; justify-content: center; align-items: center; gap: 14px; font-size: 14px; color: {BLUE}">'
                 f'<span>问这份通知 ›</span><span style="width: 1px; height: 14px; background: {LINE2}"></span><span>也可以做条款对照</span></div>')
    else:
        links = f'<div style="margin-top: 10px; padding: 4px 0; text-align: center; font-size: 14px; color: {BLUE}">也可以做条款对照</div>'
    return card(section_title("AI 解读")
                + f'<div style="margin-top: 4px; font-size: 14px; line-height: 1.7">约半分钟把这份通知的申报对象、条件、时间、材料等原文原句摘出来分好类，每条都能回原文核对。</div>'
                + btn_primary("开始解读") + links + '<div style="height: 16px"></div>')


PAGE_HEAD = ("当前位置：", DOC["title"], "湖北省科学技术厅")   # 网页导航、标题、发文机关（发布时间那一行按格式认）
PAGE_TAIL = ("附件:", "欢迎关注", "相关文档", "关联链接", "×", "您访问的链接即将离开")


def clean_body(text):
    """照 #345 去掉正文开头的网页导航、标题、发布时间、发文机关，和末尾的公众号、相关文档、离站弹窗这些网页上的字。"""
    import re
    lines = text.split("\n")
    while lines and (lines[0].startswith(PAGE_HEAD) or re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \|", lines[0])):
        lines.pop(0)
    while lines and lines[-1].startswith(PAGE_TAIL):
        lines.pop()
    return "\n".join(lines)


def body_preview(nextv=False):
    """正文先显示 600 字左右，在换行处断开（同 pages/detail/index.js 的 bodyPreview）。"""
    body = clean_body(BODY) if nextv else BODY
    cut = body.rfind("\n", 0, 600)
    return body[:cut if cut > 400 else 600] + "……"


def detail_body(nextv=False):
    text = body_preview(nextv).replace("\n", "<br>")
    return card(section_title("正文")
                + f'<div style="margin: -2px 0 8px; font-size: 11px; line-height: 1.6; color: {T3}">从原网页或 PDF 自动提取，未经人工逐字核对；截止日期可先看上面的“截止时间”，一切以原文为准。</div>'
                + f'<div style="font-size: 14px; line-height: 1.8">{text}</div>'
                + f'<div style="margin-top: 8px; padding-bottom: 16px; font-size: 13px; color: {BLUE}">展开全文</div>')


def detail_bar(nextv=False):
    # 下一版照 #349：主按钮「站内阅读」；「导出」改叫「结果」（结果先在小程序里看，另存 Excel/Word 是可选的）
    main = f'{icon("doc", 16, "#FFFFFF")}站内阅读' if nextv else f'{icon("copy", 16, "#FFFFFF")}复制原文链接'
    first = "结果" if nextv else "导出"
    items = "".join(f'<span style="width: 50px; height: 48px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1px; '
                    f'font-size: 11px; color: {T2}">{icon(i, 22, T2)}<span>{t}</span></span>' for t, i in ((first, "download"), ("纠错", "flag"), ("转发", "share")))
    return (f'<div style="flex-shrink: 0; box-sizing: border-box; padding: 8px 16px 28px; background: {CARD}; border-top: 1px solid {LINE}; display: flex; align-items: center; gap: 4px">'
            f'{items}<span style="margin-left: auto; height: 44px; padding: 0 22px; display: inline-flex; align-items: center; gap: 6px; background: {BLUE}; '
            f'color: #FFFFFF; border-radius: 22px; font-size: 15px; font-weight: 500">{main}</span></div>')


def detail(nextv):
    top = nav("文件详情") + notice_bar(nextv=nextv) + detail_head() + detail_due() + detail_points() + ai_card(nextv) + detail_body(nextv)
    # 画到 AI 卡片下面一点：这份通知有截止时间，手机第一屏只到截止时间那一块，卡片要往下滑才看到
    return first_screen(top, detail_bar(nextv), h=1160)


# ---------- 卡片在不能解读时（当前版本） ----------
def state_label(text, sub=""):
    s = f'<div style="margin-top: 2px; font-size: 12px; line-height: 1.6; color: {T3}">{sub}</div>' if sub else ""
    return f'<div style="padding: 18px 16px 0"><div style="font-size: 13px; font-weight: 600; color: {T2}">{text}</div>{s}</div>'


def off_card(reason, nxt=""):
    n = f'<div style="margin-top: 10px; padding: 4px 0; text-align: center; font-size: 14px; color: {BLUE}">{nxt}</div>' if nxt else ""
    return card(section_title("AI 解读") + f'<div style="margin-top: 4px; font-size: 14px; line-height: 1.7; color: {T2}">{reason}</div>{n}'
                + '<div style="height: 16px"></div>')


def card_states():
    nb = NO_BODY
    top = (f'<div style="padding: 20px 16px 0; font-size: 16px; font-weight: 600">"AI 解读"卡片不能用时</div>'
           f'<div style="padding: 4px 16px 0; font-size: 12.5px; line-height: 1.65; color: {T2}">卡片总在"要点"下面同一个位置，写原因和下一步。下面前三种是 10/22 线上真实的通知。</div>'
           + state_label("名单", f"{LIST_DOC['title']}（{LIST_DOC['issuer']} · {LIST_DOC['date']}）")
           + off_card("名单不需要 AI 解读。要查企业在不在名单上，可以用“查名单”。", "去查名单")
           + state_label("没有完整正文（有附件）", f"{nb['title']}（{nb['issuer']} · {nb['date']}，{len(nb['attachments'])} 个附件）")
           + off_card("还没有拿到完整正文，正文可能在附件里。", "看附件")
           + state_label("不是申报通知或政策文件", f"{KIND_DOC['title']}（{KIND_DOC['issuer']} · {KIND_DOC['date']}）")
           + off_card("这类文件不做 AI 解读。", "复制原文链接")
           + state_label("正文超过 2.4 万字", "这一种现在没有按钮（推进计划第四节第 1 部分最后一段：开放前走查时补一个按钮）。#349 这一种也还没给；建议和其他几种一样给「站内阅读正文」")
           + off_card("正文超过 2.4 万字，暂不能解读，请先看要点和原文。")
           + state_label("后台暂时处理不了（任何一份通知）")
           + off_card("AI 暂时不可用，请稍后再试。"))
    return sheet(top)


# ---------- AI 解读结果页 ----------
def result_strip():
    """结果页顶部吸顶的标识：开头"AI 生成："由页面固定写，后面是后台给的提示（去掉开头的"AI 生成，"）。"""
    return (f'<div style="flex-shrink: 0; padding: 12px 16px; background: {BLUE_TINT}; font-size: 14px; line-height: 1.7">'
            f'<span style="font-weight: 600">AI 生成：</span>仅供参考，以原文和主管部门解释为准</div>')


def adv_card(inner):
    return card(f'<div style="padding: 16px 0; font-size: 15px; line-height: 1.7">{inner}</div>')


def result_item(text, also=""):
    a = f'<div style="margin-top: 8px; font-size: 13px; line-height: 1.7; color: {T2}">也属于：{also}</div>' if also else ""
    return (f'<div style="margin-top: 12px; padding-top: 12px; border-top: 1px solid {LINE}">{text}{a}{text_btn("这句摘错了", 2)}</div>')


def result_section(title, items, same=""):
    s = f'<div style="margin-top: 8px; font-size: 15px; color: {T2}">另有和「{same}」相同的原句，见上。</div>' if same else ""
    return adv_card(f'<div style="font-size: 17px; font-weight: 600">{title}</div>{s}' + "".join(result_item(*it) for it in items))


SECTIONS = [
    ("申报对象", [(Q_OBJ1,), (Q_OBJ2,)], ""),
    ("申报条件", [(Q_C1,), (Q_C3,), (Q_C4,), (Q_LIMIT,)], ""),
    ("申报时间", [(Q_T1,), (Q_T2,)], ""),
    ("申报方式", [(Q_W1,), (Q_W2, "材料清单")], ""),
    ("材料清单", [(Q_M1,), (Q_M3,)], "申报方式"),
]
MISSING = "支持方式"


def missing_card(nextv=False):
    """当前版本照线上写"可能写在附件里"；下一版读了附件，这份没有附件就照实写（Grok 第 4 轮）。"""
    tail = "这份通知没有附件，可能原文没写，请查看原文。" if nextv else "可能写在附件里，也可能原文没写，请查看原文。"
    return adv_card(f'<div style="color: {T2}">正文里没找到：{MISSING}。{tail}</div>')


def clause_card():
    return adv_card(f'<div style="font-size: 17px; font-weight: 600">条款对照（可选）</div>'
                    f'<div style="margin-top: 8px">把这份通知的申报条件和你填的企业情况并排列出，方便逐条自查，不判断能否申报。</div>{btn_default("开始对照")}')


def ask_more_card():
    """下一版新加（#328 第十二节第 4 条）：结果下面、"用的时候遇到问题？"上面。"""
    return adv_card(f'<div>对这份通知还有疑问，可以接着问，AI 从原文里摘相关的原句</div>{btn_default("接着问这份通知")}')


def result_tail(nextv):
    disc = (f'<div style="padding: 20px 12px 0; text-align: center; font-size: 14px; line-height: 1.7; color: {T2}">{DISCLOSURE}</div>' if nextv else "")
    helpc = adv_card(f'<div style="font-size: 17px; font-weight: 600">用的时候遇到问题？</div>{contact_block()}')
    foot = (f'<div style="padding: 20px 12px 24px; text-align: center; font-size: 14px; line-height: 1.7; color: {T2}">'
            f'<span style="color: {BLUE}">投诉举报</span></div>')
    return clause_card() + (ask_more_card() if nextv else "") + disc + helpc + foot


def result_page():
    title = adv_card(f'<div style="font-size: 17px; font-weight: 600">{DOC["title"]}</div>{text_btn("看原文", 4)}')
    secs = "".join(result_section(*s) for s in SECTIONS)
    return phone(nav("AI 解读") + notice_bar() + result_strip() + title + secs + missing_card() + result_tail(False))


def result_bottom():
    """下一版：结果页滑到最下面（顶部的"AI 生成"一直吸顶）。"""
    last = result_section(*SECTIONS[-1])
    return phone(nav("AI 解读") + result_strip() + last + missing_card(True) + result_tail(True))


# ---------- 问一问对话页（下一版，#328 第十二节） ----------
def chat_strip():
    return (f'<div style="flex-shrink: 0; height: 40px; padding: 0 16px; display: flex; align-items: center; justify-content: space-between; '
            f'background: {CARD}; border-bottom: 1px solid {LINE}"><span style="font-size: 13px; font-weight: 600">问这份通知的回答由 AI 生成 · 以原文为准</span>'
            f'<span style="font-size: 13px; color: {BLUE}">说明</span></div>')


def doc_card():
    """从一份通知进来：通知卡片固定在顶部，"AI 解读""条款对照"照 AI 首页"刚才看的通知"那张卡的规则给。"""
    return (f'<div style="margin: 10px 12px 0; padding: 14px 16px; background: {CARD}; border-radius: 12px">'
            f'<div style="font-size: 15px; font-weight: 600; line-height: 1.6">{DOC["title"]}</div>'
            f'<div style="margin-top: 4px; font-size: 12.5px; color: {T2}">{DOC["issuer"]} · {DOC["date"]}</div>'
            f'<div style="margin-top: 10px; display: flex; align-items: center; gap: 8px">{chip("AI 解读")}{chip("条款对照")}'
            f'<span style="margin-left: auto; font-size: 13px; color: {BLUE}">换一份通知 ›</span></div></div>')


# 没带通知时"问这份通知"是灰的：原因直接写在三个技能按钮下面（agy 第 2 轮：灰按钮看不出为什么；#328 原来放在页顶通知卡片的位置）
NO_DOC = "想问某份通知里的具体内容（比如截止、条件）：先用“找通知”找到它，再点那一行的“问这份通知”。"


SKILLS = ("找通知", "问这份通知", "查名单")
EXAMPLES = {
    # #328 的两句（广东今年专精特新、四川近 30 天科技项目）10/22 线上都是 0 份，换成查得到的（见设计说明第四节）
    "找通知": (["找近 30 天的 5G 工厂申报", "找宁夏近 7 天的申报"], "手上有通知的，直接输入它的标题或文号"),
    # 例子是所有通知共用的固定文字：换成每份申报通知都答得上的（agy 第 4 轮：#328 的"对成立时间有什么要求？"很多通知没写，点了只会答"没找到"）
    "问这份通知": (["企业申报截止是哪天？", "要交哪些材料？", "要满足哪些条件？"], ""),
    "查名单": ([], "只在收录的政府公开名单（认定、公示、获奖等）里，按企业全称或统一社会信用代码核对，列出对上的名单名称、发布机关和日期"),
}
HINT = {
    "找通知": ("说说要找什么通知，或输入标题、文号",
             "这一项只帮你找到通知，不用 AI，不算次数；想知道某份通知里怎么写的，找到后点那一行的“问这份通知”"),
    "问这份通知": ("问这份通知里的事",
              "你打的字会交给 AI 处理，请不要写企业名称、信用代码、手机号（要查企业上过哪些名单，点“查名单”）。"
              "今天还剩 {n} 次（找到原句才算，没找到不算）"),
    "查名单": ("企业全称或统一社会信用代码", "这一项只在收录的名单里比对，不交给 AI，所以可以输入企业名称；不算次数"),
}


def skills(selected, ask_off=False, examples=True, ask_note=""):
    segs = []
    for s in SKILLS:
        if s == selected:
            st = f"background: {BLUE}; color: #FFFFFF; border: 1px solid {BLUE}; font-weight: 600"
        elif s == "问这份通知" and ask_off:
            st = f"background: {CARD}; color: #C9CDD4; border: 1px solid {LINE}"
        else:
            st = f"background: {CARD}; color: {T1}; border: 1px solid {LINE2}"
        segs.append(f'<span style="height: 38px; display: flex; align-items: center; justify-content: center; border-radius: 19px; font-size: 14px; {st}">{s}</span>')
    ex = (f'<div style="margin-top: 8px; padding: 8px 10px; border-radius: 8px; background: {FILL}; font-size: 12.5px; line-height: 1.6; color: {T2}">{ask_note}</div>'
          if ask_note else "")
    if examples:
        chips, note = EXAMPLES[selected]
        c = "".join(f'<span style="height: 32px; padding: 0 12px; display: inline-flex; align-items: center; border-radius: 16px; background: {CARD}; '
                    f'border: 1px solid {LINE2}; font-size: 13px; white-space: nowrap">{t}</span>' for t in chips)
        ex += (f'<div style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px">{c}</div>' if c else "") + (
            f'<div style="margin-top: 8px; font-size: 12px; line-height: 1.6; color: {T3}">{note}</div>' if note else "")
    return (f'<div style="padding: 14px 12px 0"><div style="font-size: 13px; color: {T2}">你想做什么？（一次选一个）</div>'
            f'<div style="margin-top: 8px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px">{"".join(segs)}</div>{ex}</div>')


def input_bar(skill, n=5, can_send=True):
    ph, note = HINT[skill]
    send = BLUE if can_send else "#BCCDF7"
    return (f'<div style="flex-shrink: 0; background: {CARD}; border-top: 1px solid {LINE}; padding: 8px 12px 28px">'
            f'<div style="display: flex; align-items: center; gap: 8px">'
            f'<div style="flex-grow: 1; min-width: 0; height: 40px; padding: 0 14px; box-sizing: border-box; border-radius: 20px; background: {FILL}; display: flex; '
            f'align-items: center; font-size: 14.5px; color: {T3}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">{ph}</div>'
            f'<span style="flex-shrink: 0; height: 36px; padding: 0 15px; display: inline-flex; align-items: center; border-radius: 18px; background: {send}; '
            f'color: #FFFFFF; font-size: 14.5px; font-weight: 500">发送</span></div>'
            f'<div style="margin-top: 7px; font-size: 11.5px; line-height: 1.6; color: {T3}">{note.format(n=n)}</div></div>')


def user_msg(skill, text):
    """蓝色气泡：你说的话，左上角小字写是哪个技能。"""
    return (f'<div style="display: flex; justify-content: flex-end; padding: 16px 12px 0 56px">'
            f'<div style="padding: 7px 14px 9px; background: {BLUE}; color: #FFFFFF; border-radius: 18px 18px 4px 18px">'
            f'<div style="font-size: 11px; line-height: 1.5; color: rgba(255,255,255,0.78)">{skill}</div>'
            f'<div style="font-size: 15px; line-height: 1.6">{text}</div></div></div>')


def answer(kind, body, foot=True):
    """回答卡片：问这份通知开头标"AI 生成"；找通知、查名单不是 AI 生成的，标"只查收录"；
    正在找、没做成是系统提示，不是 AI 生成的，不标（agy 10/23）。"""
    if kind == "sys":
        return (f'<div style="margin: 12px 12px 0; padding: 12px 14px 12px; background: {CARD}; border-radius: 12px">{body}</div>')
    if kind == "ai":
        t = (f'<span style="padding: 0 6px; line-height: 20px; border-radius: 4px; background: {BLUE}; color: #FFFFFF; font-size: 12px; '
             f'font-weight: 600">AI 生成</span>')
    else:
        t = (f'<span style="padding: 0 6px; line-height: 20px; border-radius: 4px; background: {FILL}; color: {T2}; font-size: 12px; '
             f'font-weight: 600">只查收录</span>')
    f = ""
    if kind == "ai" and foot:
        f = (f'<div style="margin-top: 12px; padding-top: 10px; border-top: 1px solid {LINE}; font-size: 11.5px; line-height: 1.7; color: {T3}">'
             f'<div>不判断能不能申报，以原文和主管部门解释为准。</div>'
             f'<div style="display: flex; align-items: center"><span>{DISCLOSURE}</span><span style="margin-left: auto; color: {T2}">投诉举报</span></div></div>')
    return (f'<div style="margin: 12px 12px 0; padding: 12px 14px 12px; background: {CARD}; border-radius: 12px">'
            f'<div style="display: flex">{t}</div><div style="margin-top: 8px">{body}</div>{f}</div>')


def para(text, size=14.5, color=T1, mt=0):
    return f'<div style="margin-top: {mt}px; font-size: {size}px; line-height: 1.7; color: {color}">{text}</div>'


def quote(n, text):
    """问这份通知的原句：编号、原句（灰底）、出处，可以看原文、纠错。"""
    return (f'<div style="margin-top: 10px"><div style="display: flex; gap: 8px">'
            f'<span style="flex-shrink: 0; margin-top: 3px; width: 18px; height: 18px; border-radius: 4px; background: {BLUE_TINT}; color: {BLUE}; '
            f'font-size: 11.5px; font-weight: 600; display: inline-flex; align-items: center; justify-content: center">{n}</span>'
            f'<div style="flex: 1; min-width: 0; padding: 8px 10px; background: {QUOTE_BG}; border-radius: 8px; font-size: 14.5px; line-height: 1.75">{text}</div></div>'
            f'<div style="margin: 6px 0 0 26px; display: flex; align-items: center; gap: 14px; font-size: 12.5px">'
            f'<span style="color: {T3}">出处：正文</span><span style="color: {BLUE}">看原文 ›</span>'
            f'<span style="margin-left: auto; display: inline-flex; align-items: center; gap: 3px; color: {T2}">{icon("flag", 13, T2)}纠错</span></div></div>')


def chat_tail():
    return (f'<div style="padding: 18px 16px 0; display: flex; justify-content: center; gap: 28px; font-size: 13px">'
            f'<span style="display: inline-flex; align-items: center; gap: 3px; color: {T2}">{icon("delete", 14, T2)}删除这条对话</span>'
            f'<span style="color: {BLUE}">联系客服</span></div>'
            f'<div style="padding: 8px 16px 0; text-align: center; font-size: 12px; color: {T2}"><span style="color: {BLUE}">投诉举报</span></div>')


def chat_top():
    return nav("问一问") + notice_bar(nextv=True) + chat_strip()


# #328 第十二节「问一问使用说明」五条（cd036bfc 的候选文字，按 D-065 在 PR-2 合并前定稿）。
# 卡片里放全文，不是摘要（#328：第一次用先看全文、点"同意并开始"才能发；Codex 10/24 指出原型原来只画了四句摘要）
CHAT_TERMS = [
    ("三个技能做什么",
     "找通知、查名单在我们收录的政府文件和名单里查，不用 AI 模型。问这份通知由 AI 从这份通知的原文（正文和能读到的附件）里，"
     "把和你的问题有关的句子原样摘出来，每句都能点开原文核对；它不替你判断能不能申报。"),
    ("你打的字交给谁",
     "三个技能里你打的字，先在我们的服务器上查有没有第 3 条说的号码，没被拦下的再交给微信做内容安全检查，同时提供你的微信用户标识"
     "（openid：微信分给这个小程序的一串编号，不是你的微信号或手机号；微信的检查接口要求带上）。用“问这份通知”时，这一问还会和这份通知里的相关段落一起，"
     "经腾讯云开发交给已备案的大模型处理；接着问时，这条对话里前两次“问这份通知”的问题和上一答摘的原句也一起交过去，好让它明白你指的是哪句。"
     "交给模型的都不带你的微信用户标识。找通知、查名单里打的字不交给模型。"),
    ("请不要写的",
     "找通知、问这份通知里有统一社会信用代码、手机号、身份证号、银行卡号这类号码的，我们直接拦下，不发出、不保存。企业名称我们认不全、拦不住，"
     "写进“问这份通知”就会交给模型，所以请不要写；问这份通知也用不着写企业名称，直接问“要满足哪些条件”就行。查名单本来就要输入企业全称或统一社会信用代码，"
     "照常查询、保存，不交给模型；手机号、身份证号、银行卡号照样拦下。"),
    ("保存多久、怎么删",
     "你打的字和回答作为对话保存 30 天，到期删除；你可以随时在对话页或“我的记录”里删除，也可以按《隐私保护指引》第五节请我们删除；注销账号时一起删除；"
     "删除或到期以后，数据库备份里的副本也会在最长 30 天内随备份清除。被拦下的字不保存。我们还记下你同意本说明的版本和时间，以及你每天用“问这份通知”的次数"
     "（保留本月和上个月的）。你问过的问题和同意记录，可以在“我的 → 我的数据”里查看。"),
    ("和《隐私保护指引》的关系",
     "2026-12 版以前的《隐私保护指引》写的时候还没有问一问。它开头的重点提示、一·14（AI 功能）和四·2（微信内容安全检查、AI 模型调用）里的这几句，"
     "说的是解读、对照和纠错，不适用于问一问：“AI 功能只把政府公开通知的段落发给模型”“发给模型的只有政府公开通知正文的相关段落”“输入保存 7 天”"
     "“内容安全检查涉及微信用户标识和纠错内容”。问一问处理哪些信息、交给谁、存多久、怎么删，以本说明第 2–4 条为准。本说明没写的（你有哪些权利、怎么联系我们等），"
     "照《隐私保护指引》。"),
]


def consent_card():
    items = "".join(f'<div style="margin-top: 10px; display: flex; gap: 6px; font-size: 13.5px; line-height: 1.75"><span style="flex-shrink: 0; color: {T3}">{i + 1}.</span>'
                    f'<span><b style="font-weight: 600">{h}：</b>{t}</span></div>' for i, (h, t) in enumerate(CHAT_TERMS))
    return (f'<div style="margin: 10px 12px 0; padding: 16px; background: {CARD}; border-radius: 12px; box-shadow: inset 0 0 0 1px {BLUE_TINT}">'
            f'<div style="font-size: 16px; font-weight: 600; line-height: 1.5">问一问使用说明</div>'
            f'<div style="margin-top: 2px; font-size: 12.5px; color: {T2}">《隐私保护指引》的补充，只管“问一问”；第一次用，看完点“同意并开始”才能发</div>{items}'
            f'<div style="margin-top: 12px; font-size: 13px; color: {BLUE}">查看《隐私保护指引》 ›</div>{btn_primary("同意并开始", 14)}</div>')


def chat_consent():
    top = chat_top() + consent_card() + skills("找通知", ask_off=True, ask_note=NO_DOC)
    return phone(top, input_bar("找通知", can_send=False))


def find_answer():
    rows = "".join(doc_row(d, "问这份通知" if d["ai"]["interpret"] else None, last=i == len(FIND) - 1, role=True, inapp=True) for i, d in enumerate(FIND))
    # agy 第 2 轮：重庆那行没有按钮也没说为什么。行上照旧不写原因（D-075），列出的有不能问的才在最后加一句
    cant = any(not d["ai"]["interpret"] for d in FIND)
    note = para("没有“问这份通知”按钮的，是现在不能问的通知（比如还没有拿到完整正文），点标题能看到原因。", 12.5, T2, 8) if cant else ""
    body = (para("按这些条件找的：类型 申报通知 · 发布 近 30 天 · 标题里有 5G工厂", 13.5, T2)
            + para(f"找到 {DATA['find']['total']} 份，按发布日期从新到旧：", 14.5, T1, 4)
            + f'<div style="margin-top: 2px">{rows}</div>' + note)
    return answer("find", body)


def chat_find():
    top = chat_top() + skills("找通知", ask_off=True, ask_note=NO_DOC) + user_msg("找通知", "找近 30 天的 5G 工厂申报") + find_answer() + chat_tail()
    return phone(top, input_bar("找通知"))


# 走查后改的固定说法（比 #328 第十二节第 3 条多改了几处，见设计说明第四节）：
# 开头写明可能没摘全；"看清是谁的时间"只在摘到的原句里有两个以上日期，或有审核、报送、推荐、送达这类环节时才加；
# 一个日期都没摘到时说一句；没有附件的写"这份通知没有附件"
READ_BODY = "读了：正文（这份通知没有附件）。"
# Grok 第 4 轮：#328 原来的写法举了"区县、纸质材料"，原句里没有这些环节时会看岔，改成不举具体环节
TIME_NOTE = "这几句里的时间不一定是同一件事的，请看清每句写的是谁、做什么的时间。"
NO_DATE = "摘到的原句里没有写具体日期，请看原文。"


def ask_answers():
    a1 = (para("摘出了原文里和你问的有关的 2 句（可能没摘全，以原文为准）：") + quote(1, Q_T1) + quote(2, Q_T2)
          + para(TIME_NOTE, 13, T2, 10) + para(READ_BODY, 13, T2, 4))
    a2 = (para("我们不替你判断能不能报（以主管部门解释为准）。下面摘出了原文里和这个问题有关的 5 句（可能没摘全），请逐句对照你们的情况：")
          + quote(1, Q_C0) + quote(2, Q_C1) + quote(3, Q_C2) + quote(4, Q_C3) + quote(5, Q_C4) + para(READ_BODY, 13, T2, 10))
    return (user_msg("问这份通知", "企业申报截止是哪天？") + answer("ai", a1)
            + user_msg("问这份通知", "我们是企业自主研发的项目，能报吗？") + answer("ai", a2))


def chat_ask():
    top = chat_top() + doc_card() + skills("问这份通知") + ask_answers() + chat_tail()
    return phone(top, input_bar("问这份通知", n=3))


# 名单原文里企业所在的那一行：10/24 从新疆工信厅原网页的附件（.doc）核对（收录接口现在只给"按名称对上"，没有这一行，
# 站内闭环第 4 项「名单站内可读、命中定位到页和行」做好以后由接口给）。团队成员一列是个人姓名，不显示
LOOKUP_ROW = {"file": "附件《2026年第五届“天山杯”新疆APP暨人工智能创新应用大赛获奖名单》", "section": "智慧民生服务赛道",
              "cells": [("序号", "6"), ("项目名称", "丝路智服"), ("参赛单位", "中国电信股份有限公司新疆分公司"), ("奖项", "三等奖")],
              "hidden": "团队成员一列是个人姓名，不显示。"}


def evidence_block(ev):
    cells = "".join(f'<div style="display: flex; gap: 8px; margin-top: 3px"><span style="flex-shrink: 0; width: 56px; color: {T3}">{h}</span><span>{v}</span></div>'
                    for h, v in ev["cells"])
    return (f'<div style="margin-top: 8px; padding: 10px 12px; background: {FILL}; border-radius: 8px; font-size: 12.5px; line-height: 1.6">'
            f'<div style="color: {T2}">名单原文里的这一行（{ev["file"]} · {ev["section"]}）</div>'
            f'<div style="margin-top: 4px; color: {T1}">{cells}</div>'
            f'<div style="margin-top: 6px; font-size: 11.5px; color: {T3}">{ev["hidden"]}</div>'
            f'<div style="margin-top: 6px; color: {BLUE}">在名单原文里看这一行 ›</div></div>')


def list_row(d, evidence=None):
    m = d.get("matched") or {}
    return (f'<div style="margin-top: 8px; padding: 12px; border: 1px solid {LINE2}; border-radius: 10px">'
            f'<div style="display: flex; flex-wrap: wrap; align-items: center; gap: 6px">{tag("名单")}'
            f'<span style="font-size: 12px; color: {T2}">{d["region"]}</span>{status_tag(d["status"])}</div>'
            f'<div style="margin-top: 7px; font-size: 15px; font-weight: 600; line-height: 1.55">{d["title"]}</div>'
            f'<div style="margin-top: 6px; font-size: 12.5px; line-height: 1.6; color: {T2}">{d["issuer"]}</div>'
            f'<div style="margin-top: 2px; display: flex; align-items: center; font-size: 12.5px; color: {T2}">'
            f'<span style="white-space: nowrap">{d["date"]} 发布</span><span style="margin-left: auto; color: {BLUE}; white-space: nowrap">查看详情 ›</span></div>'
            f'<div style="margin-top: 4px; font-size: 12px; color: {T3}">按名称对上：{m.get("name", "")}</div>'
            f'{evidence_block(evidence) if evidence else ""}</div>')


def coverage_line():
    """收录范围：Grok 走查时"最早的发布于 2026 年"看不懂，改成写明从几月起、更早的没收录。"""
    cov = LOOKUP["coverage"]
    return f"收录的名单共 {cov['lists']} 份，最早的是 {cov['since'][:4]} 年 {int(cov['since'][5:7])} 月发布的，更早的没有收录。"


def chat_lists():
    a1 = para("要查哪家企业？请输入企业全称或统一社会信用代码。")
    a2 = (para(f"按“{LOOKUP['query']}”查的。在收录的名单里查到 {len(LOOKUP['items'])} 份：") + "".join(list_row(d, LOOKUP_ROW) for d in LOOKUP["items"])
          + para("不按年份筛，收录里对上的名单都列出来。名单性质按标题判断，以主管部门公布为准。", 12.5, T2, 10)
          + para(coverage_line(), 12.5, T2, 2))
    top = (chat_top() + skills("查名单", ask_off=True, ask_note=NO_DOC)
           + user_msg("查名单", "我们公司去年上过什么名单？") + answer("lists", a1)
           + user_msg("查名单", LOOKUP["query"]) + answer("lists", a2) + chat_tail())
    return phone(top, input_bar("查名单"))


def chat_states():
    def off(text, retry=False):
        r = f'<span style="margin-left: 8px; color: {BLUE}">重试</span>' if retry else ""
        return (f'<div style="margin: 8px 12px 0; padding: 10px 14px; background: {CARD}; border-radius: 12px; font-size: 13px; line-height: 1.7; color: {T2}">'
                f'{text}{r}</div>')
    top = (f'<div style="padding: 20px 16px 0; font-size: 16px; font-weight: 600">问一问：用不了、没答上的时候</div>'
           f'<div style="padding: 4px 16px 0; font-size: 12.5px; line-height: 1.65; color: {T2}">放在一起看，实际一次只出现一种。所有情况都不写"暂未开放""以后会有"。</div>'
           + state_label('"问这份通知"用不了：按钮变灰，原因直接写在三个按钮下面（不用先点一下）')
           + off("没选通知：要先选一份通知：用“找通知”找到后，点那一行的“问这份通知”。")
           + off("5 次用完：问这份通知今天的 5 次用完了，明天 0 点恢复；找通知、查名单照常能用。")
           + off("问满 20 句：今天已经问了 20 句（每天最多问 20 句，找没找到都算，和“还剩几次”分开算），问这份通知明天 0 点恢复；找通知、查名单照常能用。")
           # #328 第十二节原话：按钮变灰时的重试只是再问一次后台能不能处理，不重发问题（和发出以后"没做成"的重试不同，agy、Grok 10/24 都看岔过）
           + off("后台处理不了：AI 暂时不可用，请稍后再试；找通知、查名单照常能用。（点“重试”只是再问一次后台能不能处理，不算次数）", retry=True)
           + off("这份不能做：这份文件已下架，问这份通知用不了；可以点上面的“换一份通知”，或用“找通知”找别的。")
           + state_label("发送被拦下（不保存、不算次数，字留在输入框里）")
           # #328 第十二节分两种说法：找通知、查名单不交给模型，不能说"会发给 AI 模型"（agy 10/24）
           + off("问这份通知里：你输入的内容里像有手机号。这一项会把你打的字发给 AI 模型，请删掉后再发。这次不算次数。")
           + off("找通知、查名单里：你输入的内容里像有手机号，请删掉后再发。")
           + state_label("发出以后没做成（不算次数）")
           # #328 第十二节：全站满了、AI 暂时不可用时不给"重试"（同解读页 NO_RETRY_CODES），画能重试的"处理超时了"（Codex 10/24）
           + answer("sys", para("这次没做成：处理超时了。这次不算次数。", 14) + f'<div style="margin-top: 6px; font-size: 13px; color: {BLUE}">重试</div>')
           + state_label("正在找")
           + answer("sys", para("正在原文里找相关的原句…一般 10–30 秒。可以先离开，之后在 AI 首页“我的 AI 记录”里打开这条对话接着看。", 14, T2))
           + state_label("问时间，摘到的原句里没有日期", "问：什么时候公布结果？")
           + answer("ai", para("摘出了原文里和你问的有关的 1 句（可能没摘全，以原文为准）：", 14) + quote(1, Q_PUB)
                    + para(NO_DATE, 13, T2, 10) + para(READ_BODY, 13, T2, 4))
           + state_label("没找到", "问：能给多少补贴？")
           + answer("ai", para("这份通知已读的内容里没有写。") + para(f"{READ_BODY}可以换个说法再问，或点“看原文”自己找。这次不算次数。", 13, T2, 4)
                    + f'<div style="margin-top: 8px; font-size: 12.5px; color: {BLUE}">看原文 ›</div>')
           + state_label("查名单没查到", f"问：{NONE_LOOKUP['query']}（10/22 线上真实结果）")
           + answer("lists", para(f"按“{NONE_LOOKUP['query']}”查的。按这个名称，收录的名单里没查到。只代表我们没收录到，不代表没上过；改过名、分公司请分别查；以主管部门公布为准。")
                    + para(coverage_line(), 12.5, T2, 6))
           + state_label("范围外", "问：帮我写一份申报书")
           + answer("ai", para("这个问题不在“问这份通知”能答的范围里：这里只从这份通知的原文里找相关原句，不代写材料、不判断能不能申报，"
                               "也不答和这份通知无关的问题。想找别的通知，点“找通知”。这次不算次数。"))
           + state_label("要问清楚", "问：第 3 句是什么意思？（上一个回答只列了 2 句）")
           + answer("ai", para("上一个回答里没有第 3 句（只列了 2 句）。你指的是哪一句？可以把那句的几个字打出来再问。这次不算次数。")))
    return sheet(top)


# ---------- 两组画面 ----------
NOW = [
    ("当前1_AI首页", "当前 1", "AI 首页", lambda: ai_home(False),
     "底栏多了第 5 格「AI」，放在中间（只对白名单里的你显示）。两项能力各一行、写今天还剩几次，下面是刚才看的通知、近 7 天能解读的申报通知、我的 AI 记录和联系客服。"
     "两项能力那两行现在点不了，只能点「去找一份通知」；模型和备案号还没配，页底不显示那一行。",
     ["底栏 5 格", "两行点不了", "近 7 天：线上 58 份里最新 5 份", "记录是示例"]),
    ("当前2_通知详情", "当前 2", "通知详情（画到 AI 卡片）", lambda: detail(False),
     "这份通知有截止时间，手机第一屏大约只到截止时间那一块，「要点」下面的卡片要往下滑才看到，所以标题卡里有一行「这份通知可以 AI 解读 ↓」，点了滚到卡片；卡片位置不动（你 10/17 在手机上看过定的）。下面的正文、原文出处没变。",
     ["标题卡提示", "卡片在要点下面", "湖北科技厅（10/20）"]),
    ("当前3_卡片不能解读时", "当前 3", "卡片在不能解读时", card_states,
     "每份通知同一个位置都有这张卡片，做不了就写原因和下一步。前三种是线上真实的通知；「正文超过 2.4 万字」那种现在没有按钮。",
     ["写原因", "给下一步", "超长那种缺按钮"]),
    ("当前4_AI解读结果", "当前 4", "AI 解读结果", result_page,
     "顶部一直有「AI 生成：仅供参考……」；原句按申报对象、支持方式、申报条件、申报时间、申报方式、材料清单分好，同一句只出现一次、标「也属于」；"
     "每句下面「这句摘错了」。最下面是条款对照和联系客服。",
     ["原句逐字取自正文", "分类是示意", "没有接着问"]),
]

NEXT = [
    ("下一版1_AI首页", "下一版 1", "AI 首页：三项", lambda: ai_home(True),
     "加第三行「问一问」：找通知、查名单，或就一份通知提问（AI 只摘原句）；次数只算「问这份通知」，找通知、查名单不限次。三行都能点（第 0 步第 1 处，另一会话在做）。"
     "刚才看的通知加「问这份通知 ›」；我的 AI 记录里也列对话（没选通知的那条写最近一问，是查名单的写明「查名单」）；页底公示模型和备案号；顶部出处说明条画成「每条都有出处」（全站组件，跟 #349 定）。列表行的「复制原文链接」照 #349 换成「查看详情」（10/23 起不让用户复制网址去浏览器）。宁夏那份原网站撤稿了，照 #347 不再列在能解读的通知里。",
     ["问一问", "三行能点", "记录里有对话", "公示占位"]),
    ("下一版2_通知详情", "下一版 2", "通知详情：卡片加「问这份通知」", lambda: detail(True),
     "卡片还在「要点」下面，「开始解读」下面多一个「问这份通知」，和「也可以做条款对照」并排。标题卡那一行提示不变。底部照 #349：主按钮「站内阅读」，「导出」改叫「结果」（结果先在小程序里看）；正文照 #345 去掉了网页上的导航和页脚。",
     ["卡片位置不动", "问这份通知"]),
    ("下一版3_问一问第一次用", "下一版 3", "问一问：第一次用", chat_consent,
     "从 AI 首页「问一问」进来，没带通知，技能默认「找通知」。不管从哪进来，第一次用都先看「问一问使用说明」五条全文（#328 第十二节，《隐私保护指引》的正式补充，候选文字按 D-065 定稿）、点「同意并开始」才能发（服务端记下同意的版本）。"
     "页顶写明只有「问这份通知」的回答是 AI 生成的。没有相机、没有拍照入口。",
     ["使用说明", "同意并开始", "没有相机"]),
    ("下一版4_找通知", "下一版 4", "问一问：找通知", chat_find,
     "不用 AI，按规则从一句话里取条件，交给现有搜索；第一句写「按这些条件找的」。截止照「截止」页的写法标出是谁的截止（D-078 第五节）。"
     "能解读的那一行才有「问这份通知」（重庆那份没有完整正文，没有）。卡片标「只查收录」，不标 AI 生成。",
     ["只查收录", "不算次数", "线上真实 5 份"]),
    ("下一版5_问这份通知", "下一版 5", "问一问：问这份通知", chat_ask,
     "从详情卡片「问这份通知」进来，通知卡片固定在顶部。回答只摘原句、不写结论：开头写「摘出了……N 句（可能没摘全）」；问能不能报时先说不替你判断；"
     "写明读了哪些；每句能看原文、纠错；末尾不判断能不能申报、模型和备案号、投诉举报。",
     ["只摘原句", "可能没摘全", "读了：正文", "摘哪几句是示意"]),
    ("下一版6_查名单", "下一版 6", "问一问：查名单", chat_lists,
     "不用 AI，按企业全称或信用代码精确比对，只在收录的政府公开名单里查，说明只写查什么、列什么，不写信贷、逾期、失信这类字（10/23 查名单被认成企业征信；agy、Grok 都提否定句也会被读成征信）。没写企业（「我们公司」）先问是哪家；查到的列名单名称、发布机关、日期和名单性质，写明不按年份筛、收录从哪个月开始。查到以后直接给出名单原文里企业所在的那一行（所在附件、赛道、表头），还能在名单原文里接着看（站内闭环第 4 项「名单站内可读、命中定位」，还没做；Codex 10/24 提）。"
     "线上 10/22 真实查得到的一家；那一行是 10/24 从新疆工信厅原网页的附件核对的，团队成员是个人姓名，不显示。",
     ["只查收录", "只查政府公开名单", "名单里那一行", "可以输企业名称"]),
    ("下一版7_结果页接着问", "下一版 7", "AI 解读结果：滑到最下面", result_bottom,
     "结果下面、「用的时候遇到问题？」上面加一张「接着问这份通知」，进对话页并带上这份通知。条款对照的结果页也加同一张。页底公示模型和备案号。",
     ["接着问这份通知", "公示占位"]),
    ("下一版8_各种情况", "下一版 8", "问一问：用不了、没答上的时候", chat_states,
     "每种都写原因、扣没扣次数、下一步；「问这份通知」用不了时原因直接写出来，不用先点一下；没找到写「这份通知已读的内容里没有写」，不说「政策没有规定」。「正在找」「没做成」是系统提示，不标「AI 生成」；模型答的（没找到、范围外、要问清楚）标「AI 生成」，末尾写模型和备案号。",
     ["不扣次数", "没读到不等于没规定", "查名单没查到"]),
]


def static_page(fn):
    """单屏预览：本机无头 Chrome 截图用；页面底色用品红，量高度时好找画面边界。"""
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{FONTS_LINK}'
            f'<style>body{{margin:0;background:#FF00FF}}</style></head><body>{fn()}</body></html>')


if __name__ == "__main__":
    import board  # 看板页面的文字和样式放在 board.py

    out_board = Path(sys.argv[1])
    st_dir = Path(sys.argv[2])
    st_dir.mkdir(parents=True, exist_ok=True)
    out_board.parent.mkdir(parents=True, exist_ok=True)
    out_board.write_text(board.page(NOW, NEXT, DATA), encoding="utf-8")
    for sid, label, title, fn, intent, chips in NOW + NEXT:
        (st_dir / f"{sid}.html").write_text(static_page(fn), encoding="utf-8")
        print(sid, title)
