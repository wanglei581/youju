"""按 C 精修版规范生成原型 9 个页面：画布用的 .dc.html，和按默认状态展开的效果图 HTML。"""

import sys
from pathlib import Path

from ds import (BG, BLUE, BLUE_G1, BLUE_G2, BLUE_TINT, CARD, FILL, G_TINT, G_TXT, GREEN, LINE, LINE2, O_TINT, ORANGE, RED, R_TINT, T1, T2, T3, capsule, card, dc_page, doc_row, icon, js, navbar, section_title, static_preview, tabbar, tag_type, notice_bar, load_more, meta_line, deadline, copy_id, copy_bubble, with_copy)

DC_DIR = Path(sys.argv[1])
ST_DIR = Path(sys.argv[2])

D = {
    "ln": dict(kind="申报通知", region="辽宁 · 省级", deadline="10月18日截止", days=21,
               title="辽宁省工业和信息化厅关于组织开展2026年三季度辽宁省制造业固定资产贷款贴息及担保补贴项目申报工作的通知",
               issuer="辽宁省工业和信息化厅", date="2026-09-24", url="https://gxt.ln.gov.cn/gxt/tztg/2026092416504833875/index.shtml"),
    "sh": dict(kind="名单", status="公示", region="上海 · 省级", title="关于上海市2026年第六批拟入库科技型中小企业名单的公示",
               issuer="上海市科学技术委员会", date="2026-09-24",
               url="https://stcsm.sh.gov.cn/zwgk/tzgs/gsgg/zhgsgg/20260924/76934379206c4e5dbfac5fff53d0b257.html"),
    "jx": dict(kind="申报通知", region="江西 · 省级", deadline="10月23日 17:00 截止", days=26,
               title="关于组织申报2026年度江西省技术创新中心的通知", issuer="江西省科学技术厅", date="2026-09-24",
               url="https://kjt.jiangxi.gov.cn/jxskxjst/col/col27029/content/content_2103050611282489344.html"),
    "gw": dict(kind="政策", region="国家", title="国务院办公厅关于印发《“高效办成一件事”2026年度第二批重点事项清单》的通知",
               issuer="国务院办公厅", date="2026-09-23", url="https://www.gov.cn/zhengce/content/202609/content_7081880.htm"),
    "bj": dict(kind="名单", status="正式公布", region="北京 · 省级", title="北京市经济和信息化局关于公布2026年新创建北京市企业技术中心名单的通知",
               issuer="北京市经济和信息化局", date="2026-09-24", url="https://jxj.beijing.gov.cn/jxdt/tzgg/202609/t20260924_4879912.html"),
    "hb": dict(kind="申报通知", region="湖北 · 省级", deadline="10月26日 17:00 截止", days=29,
               title="省科技厅关于组织申报2026年部分省级技术创新计划项目的通知", issuer="湖北省科学技术厅", date="2026-09-23",
               url="https://kjt.hubei.gov.cn/kjdt/tzgg/202609/t20260922_6020453.shtml"),
    "gd": dict(kind="政策", region="广东 · 省级", title="广东省工业和信息化厅 中国建设银行广东省分行印发产业培育“善建智造”综合金融服务方案的通知",
               issuer="广东省工业和信息化厅", date="2026-09-22", url="https://gdii.gd.gov.cn/zwgk/tzgg1011/content/post_4959711.html"),
    "sx": dict(kind="名单", status="正式公布", region="山西 · 省级", title="山西省工业和信息化厅关于公布2026年专精特新中小企业名单的通知",
               issuer="山西省工业和信息化厅", date="2026-09-22", url="https://gxt.shanxi.gov.cn/xwdt/tzgg/202609/t20260922_10225637.shtml"),
}
pages = {}


def add(name, title, height, body, js_body="return {};", defaults=None, bg=BG):
    pages[name] = dict(title=title, height=height, body=body, js=js_body, defaults=defaults or {}, bg=bg)


# ---------- 首页 ----------
def tab_styles(prefix, keys, active, gap_color=BLUE):
    on = f"display: inline-flex; flex-direction: column; justify-content: flex-end; align-items: center; gap: 9px; height: 46px; padding: 0; background: none; border: 0; font-family: inherit; font-size: 15px; font-weight: 600; color: {T1}; cursor: pointer"
    off = on.replace(f"font-weight: 600; color: {T1}", f"font-weight: 400; color: {T2}")
    bar_on, bar_off = f"width: 16px; height: 3px; border-radius: 2px; background: {gap_color}", "width: 16px; height: 3px; border-radius: 2px; background: transparent"
    return on, off, bar_on, bar_off


ON, OFF, BAR_ON, BAR_OFF = tab_styles("t", [], "")
home_top = f'''<div style="height: 184px; background: linear-gradient(180deg, {BLUE_G1} 0%, {BLUE_G2} 100%); color: #FFFFFF; padding: 0 16px; box-sizing: border-box; position: relative">
<div style="height: 48px; display: flex; align-items: center; gap: 10px">
<span style="font-weight: 700; font-size: 20px; letter-spacing: 1px">有据</span>
<span style="font-size: 11.5px; color: rgba(255,255,255,0.88); padding: 1px 6px; border: 1px solid rgba(255,255,255,0.4); border-radius: 4px">第三方整理 · 非政府平台</span>
{capsule(True)}
</div>
<div role="tablist" aria-label="搜索范围" style="height: 36px; display: flex; align-items: stretch; gap: 22px; font-size: 15px">
<span style="font-weight: 700; display: inline-flex; flex-direction: column; justify-content: center; align-items: center; gap: 4px">查申报<span style="width: 18px; height: 3px; border-radius: 2px; background: #FFFFFF"></span></span>
<span style="display: inline-flex; align-items: center; padding-bottom: 7px; color: rgba(255,255,255,0.8)">查名单</span>
<span style="display: inline-flex; align-items: center; padding-bottom: 7px; color: rgba(255,255,255,0.8)">查政策</span>
<span style="display: inline-flex; align-items: center; padding-bottom: 7px; color: rgba(255,255,255,0.8)">查文号</span>
</div>
<a href="Search.dc.html" aria-label="搜索" style="margin-top: 6px; height: 44px; display: flex; align-items: center; gap: 8px; padding: 0 4px 0 14px; background: #FFFFFF; border-radius: 22px; box-shadow: 0 4px 14px rgba(20, 45, 120, 0.18)">
{icon("search", 18, T3, 2)}
<span style="flex-grow: 1; color: {T3}; font-size: 14px">搜申报通知，如“高新技术企业”</span>
<span style="height: 36px; padding: 0 18px; display: inline-flex; align-items: center; background: {BLUE}; color: #FFFFFF; border-radius: 18px; font-size: 14px; font-weight: 500">搜索</span>
</a>
<div style="margin-top: 10px; display: flex; gap: 14px; font-size: 12px; white-space: nowrap">
<span style="color: rgba(255,255,255,0.68)">大家在查</span><a href="Search.dc.html" style="color: rgba(255,255,255,0.95)">高新技术企业</a><a href="Search.dc.html" style="color: rgba(255,255,255,0.95)">专精特新</a><a href="Search.dc.html" style="color: rgba(255,255,255,0.95)">科技型中小企业</a>
</div>
</div>'''


def entry(label, href, ic, g1, g2, plain=False):
    tile = (f"background: {FILL}" if plain else f"background: linear-gradient(145deg, {g1}, {g2})")
    color = T2 if plain else "#FFFFFF"
    return (f'<a href="{href}" style="display: flex; flex-direction: column; align-items: center; gap: 6px; color: {T1}">'
            f'<span style="width: 42px; height: 42px; border-radius: 13px; {tile}; display: flex; align-items: center; justify-content: center">'
            f'{icon(ic, 22, color, 2)}</span><span style="font-size: 12.5px">{label}</span></a>')


home_entries = (f'<nav aria-label="快捷入口" style="margin: -14px 12px 0; position: relative; padding: 14px 4px 12px; background: {CARD}; border-radius: 12px; '
                f'box-shadow: 0 2px 10px rgba(29, 33, 41, 0.05); display: grid; grid-template-columns: repeat(5, minmax(0, 1fr))">'
                + entry("查申报", "Calendar.dc.html", "apply", "#5B8CFF", BLUE)
                + entry("查名单", "Lists.dc.html", "list", "#FFAB5C", ORANGE)
                + entry("查政策", "Search.dc.html", "policy", "#3FD0A6", GREEN)
                + entry("申报日历", "Calendar.dc.html", "calendar", "#5B8CFF", BLUE)
                + entry("全部", "Search.dc.html", "grid", "", "", plain=True)
                + "</nav>")

home_summary = (f'<a href="Search.dc.html" style="margin: 10px 12px 0; height: 58px; padding: 0 16px; box-sizing: border-box; display: flex; align-items: center; gap: 12px; background: {CARD}; border-radius: 12px; color: {T1}">'
                f'<div style="flex-shrink: 0"><div style="font-size: 12px; color: {T2}; line-height: 1.3">本周新增</div>'
                f'<div style="font-size: 20px; font-weight: 700; line-height: 1.25; color: {BLUE}">158<span style="font-size: 12px; font-weight: 500; color: {T2}"> 份</span></div></div>'
                f'<div style="width: 1px; height: 28px; background: {LINE}"></div>'
                f'<div style="flex-grow: 1; font-size: 12.5px; line-height: 1.6; color: {T2}">申报 <b style="color: {T1}">25</b> · 名单与公示 <b style="color: {T1}">70</b> · 其他 <b style="color: {T1}">63</b><br>来自 28 个省和国家部委 · 每条都有出处</div>'
                f'{icon("chev", 16, T3, 2)}</a>')

home_tabs = (f'<div role="tablist" aria-label="按类型看" style="height: 46px; display: flex; align-items: stretch; gap: 20px; border-bottom: 1px solid {LINE}">'
             + "".join(f'<button type="button" role="tab" onClick="{{{{pick{k}}}}}" style="{{{{tab{k}}}}}">{label}<span style="{{{{bar{k}}}}}"></span></button>'
                       for k, label in [("All", "最新"), ("Apply", "申报通知"), ("List", "名单与公示"), ("Policy", "政策")])
             + "</div>")
home_order = [("ln", "Apply", "Detail.dc.html"), ("sh", "List", None), ("jx", "Apply", None), ("gw", "Policy", None),
              ("bj", "List", None), ("hb", "Apply", None), ("gd", "Policy", None), ("sx", "List", None)]
rows = []
for i, (key, cat, href) in enumerate(home_order):
    rows.append(f'<sc-if value="{{{{show{cat}}}}}" hint-placeholder-val="{{{{ true }}}}">{doc_row(D[key], last=False, href=href)}</sc-if>')
home_mine = (f'<a href="Follow.dc.html" style="margin: 10px 12px 0; height: 48px; padding: 0 14px; box-sizing: border-box; display: flex; align-items: center; gap: 10px; background: {BLUE_TINT}; border-radius: 12px; color: {T1}">'
             f'{icon("bell", 18, BLUE, 1.9)}<span style="flex-grow: 1; font-size: 14px">你关注的条件本周有 <b style="color: {BLUE}">10</b> 份新文件</span>'
             f'<span style="font-size: 13px; color: {BLUE}">去看看</span>{icon("chev", 14, BLUE, 2)}</a>')
home_list = card(home_tabs + "".join(rows)
                 + f'<a href="Search.dc.html" style="display: flex; align-items: center; justify-content: center; gap: 4px; height: 48px; color: {BLUE}; font-size: 14px">查看本周全部 158 份 {icon("chev", 14, BLUE, 2)}</a>')
home_footer = (f'<div style="margin: 14px 12px 0; padding: 12px 14px; display: flex; gap: 10px; background: {CARD}; border-radius: 12px">'
               f'{icon("shield", 18, T2, 1.8)}<div style="font-size: 12px; line-height: 1.5; color: {T2}">数据来自 72 个政府网站栏目，覆盖国家部委和 28 个省，每条都附原文链接。'
               f'有据是第三方整理的信息工具，不是政府官方平台，也不代办申报。'
               f'<a href="Me.dc.html" style="display: block; margin-top: 6px">覆盖说明 ›</a></div></div>')
home_js = ("const tab = (this.state && this.state.tab) || 'all';\n"
           f"    const on = {js(ON)}, off = {js(OFF)}, barOn = {js(BAR_ON)}, barOff = {js(BAR_OFF)};\n"
           "    const t = (k) => tab === k;\n"
           "    return {\n"
           "      showApply: t('all') || t('apply'), showList: t('all') || t('list'), showPolicy: t('all') || t('policy'),\n"
           "      tabAll: t('all') ? on : off, barAll: t('all') ? barOn : barOff,\n"
           "      tabApply: t('apply') ? on : off, barApply: t('apply') ? barOn : barOff,\n"
           "      tabList: t('list') ? on : off, barList: t('list') ? barOn : barOff,\n"
           "      tabPolicy: t('policy') ? on : off, barPolicy: t('policy') ? barOn : barOff,\n"
           "      pickAll: () => this.setState({ tab: 'all' }), pickApply: () => this.setState({ tab: 'apply' }),\n"
           "      pickList: () => this.setState({ tab: 'list' }), pickPolicy: () => this.setState({ tab: 'policy' }),\n"
           "    };")
home_defaults = dict(showApply=True, showList=True, showPolicy=True, tabAll=ON, barAll=BAR_ON, tabApply=OFF, barApply=BAR_OFF,
                     tabList=OFF, barList=BAR_OFF, tabPolicy=OFF, barPolicy=BAR_OFF)
add("Main", "有据 · 首页", 1790, home_top + home_entries + home_summary + home_mine + home_list + home_footer + '<div style="height: 100px"></div>' + tabbar("首页"),
    home_js, home_defaults)

# ---------- 搜索结果 ----------
Q = "科技型中小企业"
search_docs = [
    dict(kind="名单", status="正式公布", region="新疆 · 第4批", title="关于2026年第4批入库科技型中小企业名单的公告", issuer="新疆维吾尔自治区科学技术厅", date="2026-09-24",
         url="https://kjt.xinjiang.gov.cn/kjt/c100264/202609/0f846fea177a4092bc752abde9676f1c.shtml"),
    dict(kind="名单", status="公示", region="山东 · 第4批", title="山东省2026年第4批拟入库科技型中小企业名单公示", issuer="山东省科学技术厅", date="2026-09-24",
         url="http://kjt.shandong.gov.cn/art/2026/9/24/art_13360_10330190.html"),
    dict(kind="名单", status="公示", region="上海 · 第六批", title="关于上海市2026年第六批拟入库科技型中小企业名单的公示", issuer="上海市科学技术委员会", date="2026-09-24",
         url=D["sh"]["url"]),
    dict(kind="名单", status="正式公布", region="贵州 · 第三批", title="贵州省2026年第三批入库科技型中小企业名单公告", issuer="贵州省科学技术厅", date="2026-09-23",
         url="http://kjt.guizhou.gov.cn/xwzx/tzgg_73876/202609/t20260923_90935041.html"),
    dict(kind="名单", status="公示", region="安徽 · 第3批", title="关于安徽省2026年第3批拟入库科技型中小企业名单的公示", issuer="安徽省科学技术厅", date="2026-09-23",
         url="https://kjt.ah.gov.cn/kjzx/tzgg/123510901.html"),
    dict(kind="名单", status="公示", region="内蒙古 · 第七批", title="关于公示内蒙古自治区2026年第七批拟入库科技型中小企业名单的通知", issuer="内蒙古自治区科学技术厅", date="2026-09-23",
         url="https://kjt.nmg.gov.cn/kjdt/tzgg/202609/t20260923_2958019.html"),
    dict(kind="名单", status="公示", region="天津 · 第五批", title="天津市2026年第五批拟入库科技型中小企业名单公示", issuer="天津市科学技术局", date="2026-09-20",
         url="https://kxjs.tj.gov.cn/ZWGK4143/TZGG2079/202609/t20260920_7378027.html"),
]
hl = f'<span style="color: {BLUE}">{Q}</span>'
search_rows = "".join(doc_row(d, last=(i == len(search_docs) - 1), title_html=d["title"].replace(Q, hl)) for i, d in enumerate(search_docs))
chip = lambda t, on=False: ('<a href="Filter.dc.html" style="height: 30px; padding: 0 12px; display: inline-flex; align-items: center; gap: 2px; border-radius: 15px; font-size: 13px; '
                            + (f'background: {BLUE_TINT}; color: {BLUE}; border: 1px solid {BLUE}' if on else f'background: {FILL}; color: {T1}; border: 1px solid {FILL}')
                            + f'">{t}{icon("down", 13, BLUE if on else T2, 2.2)}</a>')
search_body = (navbar("搜索") + notice_bar()
    + f'<div style="background: {CARD}; padding: 8px 16px 0">'
    + f'<div style="display: flex; align-items: center; gap: 12px"><form role="search" style="flex-grow: 1; height: 38px; display: flex; align-items: center; gap: 8px; padding: 0 6px 0 12px; background: {FILL}; border-radius: 19px">'
    + f'{icon("search", 17, T2, 2)}<input type="search" aria-label="搜索关键词" value="{Q}" style="flex-grow: 1; min-width: 0; height: 36px; border: 0; outline: none; background: transparent; font-family: inherit; font-size: 15px; color: {T1}">'
    + f'<button type="button" aria-label="清空" style="width: 30px; height: 30px; display: flex; align-items: center; justify-content: center; background: none; border: 0; cursor: pointer"><span style="width: 16px; height: 16px; border-radius: 8px; background: #C9CDD4; display: flex; align-items: center; justify-content: center">{icon("close", 10, "#FFFFFF", 3)}</span></button></form>'
    + f'<a href="Main.dc.html" style="font-size: 15px; color: {T1}">取消</a></div>'
    + '<div role="tablist" style="height: 44px; display: flex; align-items: stretch; gap: 22px; margin-top: 4px; font-size: 15px">'
    + f'<span style="display: inline-flex; flex-direction: column; justify-content: flex-end; align-items: center; gap: 8px; font-weight: 600">全部 35<span style="width: 16px; height: 3px; border-radius: 2px; background: {BLUE}"></span></span>'
    + f'<span style="display: inline-flex; align-items: center; padding-top: 3px; color: {T2}">申报通知</span><span style="display: inline-flex; align-items: center; padding-top: 3px; color: {T2}">名单与公示</span><span style="display: inline-flex; align-items: center; padding-top: 3px; color: {T2}">政策</span></div></div>'
    + f'<div style="padding: 10px 12px 0; display: flex; gap: 8px; align-items: center">{chip("地区")}{chip("近 30 天", True)}{chip("状态")}'
    + f'<a href="Filter.dc.html" style="margin-left: auto; display: inline-flex; align-items: center; gap: 4px; font-size: 13px; color: {T1}">{icon("filter", 17, T1, 1.8)}筛选</a></div>'
    + f'<div style="padding: 10px 16px 0; display: flex; align-items: baseline; justify-content: space-between; font-size: 12.5px; color: {T2}"><span>近 30 天 <b style="color: {T1}">35</b> 份 · 不限时间共 59 份</span><span style="display: inline-flex; gap: 14px"><span>按发布时间 ↓</span><a href="Files.dc.html" style="color: {BLUE}">选择</a></span></div>'
    + f'<div style="margin: 8px 12px 0; padding: 10px 12px; display: flex; align-items: center; gap: 10px; background: {BLUE_TINT}; border-radius: 10px">'
    + f'{icon("bell", 18, BLUE, 1.9)}<span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px"><span style="font-size: 13px; font-weight: 500; color: {T1}">关注这个搜索</span><span style="font-size: 12px; color: {T2}">含已选条件，有新文件时提醒我</span></span>'
    + f'<a href="Follow.dc.html" style="flex-shrink: 0; height: 28px; padding: 0 14px; display: inline-flex; align-items: center; background: {BLUE}; color: #FFFFFF; border-radius: 14px; font-size: 13px; font-weight: 500; white-space: nowrap">关注</a></div>'
    + card(search_rows + load_more(7, 35))
    + f'<a href="Me.dc.html" style="display: block; margin: 14px 0 0; text-align: center; font-size: 13px; color: {T2}">没找到想要的？<span style="color: {BLUE}">告诉我们</span></a>'
    + '<div style="height: 40px"></div>')
add("Search", "有据 · 搜索结果", 1350, search_body)

# ---------- 筛选 ----------
groups = [
    ("region", "地区", "可多选", ["不限地区", "国家部委", "北京", "天津", "河北", "上海", "江苏", "安徽", "山东", "广东", "四川", "更多省份"], ["不限地区"], "广西、甘肃暂未覆盖；浙江正在接入"),
    ("type", "类型", "可多选", ["申报通知", "名单与公示", "政策文件", "其他"], [], ""),
    ("status", "名单状态", "可多选", ["公示", "正式公布"], [], ""),
    ("time", "发布时间", "单选", ["近 7 天", "近 30 天", "今年", "不限"], ["近 30 天"], ""),
    ("dept", "发文部门", "可多选", ["科技部门", "工信部门", "其他部门"], [], ""),
]
CHIP_ON = f"height: 34px; padding: 0 14px; border-radius: 17px; font-family: inherit; font-size: 13px; cursor: pointer; background: {BLUE_TINT}; color: {BLUE}; border: 1px solid {BLUE}; font-weight: 500"
CHIP_OFF = f"height: 34px; padding: 0 14px; border-radius: 17px; font-family: inherit; font-size: 13px; cursor: pointer; background: {FILL}; color: {T1}; border: 1px solid {FILL}; font-weight: 400"
fbody = []
fdefaults = {}
for gi, (gk, gt, hint, opts, sel, note) in enumerate(groups):
    chips = []
    for oi, o in enumerate(opts):
        k = f"{gk}{oi}"
        chips.append(f'<button type="button" aria-pressed="{{{{p_{k}}}}}" onClick="{{{{k_{k}}}}}" style="{{{{c_{k}}}}}">{o}</button>')
        fdefaults[f"c_{k}"] = CHIP_ON if o in sel else CHIP_OFF
        fdefaults[f"p_{k}"] = "true" if o in sel else "false"
    note_html = f'<div style="margin-top: 8px; font-size: 12px; color: {T3}">{note}</div>' if note else ""
    fbody.append(f'<div style="padding: 14px 0 4px"><div style="display: flex; align-items: baseline; gap: 8px"><span style="font-size: 15px; font-weight: 600">{gt}</span>'
                 f'<span style="font-size: 12px; color: {T3}">{hint}</span></div><div style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px">{"".join(chips)}</div>{note_html}</div>')
filter_js_groups = js([[gk, opts, sel, gk == "time"] for gk, gt, hint, opts, sel, note in groups])
filter_js = ("const st = this.state || {};\n"
             f"    const G = {filter_js_groups};\n"
             f"    const on = {js(CHIP_ON)}, off = {js(CHIP_OFF)};\n"
             "    const init = {}; G.forEach(([k, o, s]) => { init[k] = s; });\n"
             "    const sel = st.sel || init;\n"
             "    const out = {};\n"
             "    G.forEach(([k, opts, _s, single]) => {\n"
             "      opts.forEach((o, i) => {\n"
             "        const isOn = sel[k].indexOf(o) >= 0;\n"
             "        out['c_' + k + i] = isOn ? on : off;\n"
             "        out['p_' + k + i] = isOn ? 'true' : 'false';\n"
             "        out['k_' + k + i] = () => {\n"
             "          let next;\n"
             "          if (single) next = [o];\n"
             "          else if (k === 'region') next = o === '不限地区' ? ['不限地区'] : (isOn ? sel[k].filter((x) => x !== o) : sel[k].filter((x) => x !== '不限地区').concat(o));\n"
             "          else next = isOn ? sel[k].filter((x) => x !== o) : sel[k].concat(o);\n"
             "          if (k === 'region' && next.length === 0) next = ['不限地区'];\n"
             "          this.setState({ sel: { ...sel, [k]: next } });\n"
             "        };\n"
             "      });\n"
             "    });\n"
             "    out.reset = () => this.setState({ sel: { region: ['不限地区'], type: [], status: [], time: ['不限'], dept: [] } });\n"
             "    return out;")
filter_body = (f'<div aria-hidden="true">{navbar("搜索")}{notice_bar()}<div style="background: {CARD}; padding: 8px 16px 12px"><div style="height: 38px; display: flex; align-items: center; padding: 0 12px; background: {FILL}; border-radius: 19px; font-size: 15px">{Q}</div></div>'
               f'{card(doc_row(search_docs[0]) + doc_row(search_docs[1], last=True))}</div>'
               '<div aria-hidden="true" style="position: absolute; inset: 0; background: rgba(15, 20, 35, 0.5)"></div>'
               f'<section role="dialog" aria-modal="true" aria-labelledby="ft" style="position: absolute; left: 0; right: 0; bottom: 0; height: 690px; box-sizing: border-box; background: {CARD}; border-radius: 16px 16px 0 0; display: flex; flex-direction: column">'
               f'<div style="height: 54px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; position: relative; border-bottom: 1px solid {LINE}">'
               f'<h2 id="ft" style="margin: 0; font-size: 17px; font-weight: 600">筛选</h2>'
               f'<a href="Search.dc.html" aria-label="关闭" style="position: absolute; right: 8px; top: 7px; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center">{icon("close", 20, T1, 2)}</a></div>'
               f'<div style="flex-grow: 1; overflow-y: auto; padding: 0 16px">{"".join(fbody)}</div>'
               f'<div style="flex-shrink: 0; padding: 10px 16px 30px; display: grid; grid-template-columns: 1fr 2fr; gap: 10px; border-top: 1px solid {LINE}">'
               f'<button type="button" onClick="{{{{reset}}}}" style="height: 44px; background: {CARD}; border: 1px solid {LINE2}; border-radius: 22px; font-family: inherit; font-size: 15px; color: {T1}; cursor: pointer">重置</button>'
               f'<a href="Search.dc.html" style="height: 44px; display: flex; align-items: center; justify-content: center; background: {BLUE}; border-radius: 22px; font-size: 15px; font-weight: 500; color: #FFFFFF">查看 35 份结果</a></div></section>')
add("Filter", "有据 · 筛选", 844, filter_body, filter_js, fdefaults)

# ---------- 文件详情 ----------
ln = D["ln"]
kv = lambda k, v: (f'<div style="display: flex; gap: 12px; padding: 11px 0; border-bottom: 1px solid {LINE}"><div style="width: 64px; flex-shrink: 0; font-size: 13px; color: {T3}">{k}</div>'
                   f'<div style="font-size: 14px; line-height: 1.6">{v}</div></div>')
attach = lambda badge, bg, fg, name: (f'<div style="display: flex; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid {LINE}; color: {T1}">'
                                      f'<span style="width: 36px; height: 36px; flex-shrink: 0; border-radius: 8px; background: {bg}; color: {fg}; font-size: 10px; font-weight: 700; display: flex; align-items: center; justify-content: center">{badge}</span>'
                                      f'<span style="flex-grow: 1; font-size: 14px; line-height: 1.5">{name}</span></div>')
REM_ON = f"margin-top: 12px; width: 100%; height: 42px; display: flex; align-items: center; justify-content: center; gap: 6px; border-radius: 21px; font-family: inherit; font-size: 14px; font-weight: 500; cursor: pointer; background: {FILL}; color: {T2}; border: 1px solid {FILL}"
REM_OFF = f"margin-top: 12px; width: 100%; height: 42px; display: flex; align-items: center; justify-content: center; gap: 6px; border-radius: 21px; font-family: inherit; font-size: 14px; font-weight: 500; cursor: pointer; background: {CARD}; color: {BLUE}; border: 1px solid {BLUE}"
SRC_CID, BAR_CID, ATT_CID = copy_id(ln["url"]), copy_id(ln["url"]), copy_id(ln["url"])
detail_body = (navbar("文件详情") + notice_bar()
    + card(f'<div style="padding: 16px 0">'
           f'<div style="display: flex; gap: 6px">{tag_type("申报通知")}{tag_type("资金支持")}<span style="font-size: 12px; color: {T2}; line-height: 20px">辽宁 · 省级</span></div>'
           f'<h1 style="margin: 10px 0 0; font-size: 20px; line-height: 30px; font-weight: 700">{ln["title"]}</h1>'
           f'<div style="margin-top: 10px; font-size: 13px; color: {T2}">{ln["issuer"]} · {ln["date"]} 发布</div></div>')
    + card(f'<div style="padding: 16px 0">'
           f'<div style="display: flex; align-items: flex-end; justify-content: space-between"><div><div style="font-size: 12px; color: {T2}">企业申报截止</div>'
           f'<div style="margin-top: 2px; display: flex; align-items: baseline; gap: 8px"><span style="font-size: 24px; font-weight: 700; line-height: 1.25">2026年10月18日</span><span style="font-size: 13px; color: {T2}">周日</span></div></div>'
           f'<span style="height: 26px; padding: 0 10px; display: inline-flex; align-items: center; border-radius: 13px; background: {BLUE_TINT}; color: {BLUE}; font-size: 13px; font-weight: 600">剩 21 天</span></div>'
           f'<div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid {LINE}; font-size: 13px; line-height: 1.7; color: {T2}">市级推荐截止 10月20日 周二<br>原文：逾期申报平台自动关闭</div>'
           f'<button type="button" onClick="{{{{toggleRemind}}}}" style="{{{{remindStyle}}}}">{icon("bell", 17, "currentColor", 1.9)}<span>{{{{remindLabel}}}}</span></button></div>')
    + card('<div style="padding: 6px 0 4px">' + section_title("要点")
           + kv("发文机关", ln["issuer"]) + kv("文号", "辽工信投资〔2026〕130号") + kv("申报方向", "产业结构优化升级项目；智改数转和绿色低碳发展项目等")
           + kv("申报对象", "制造业企业（以 GB/T 4754-2017 为准，含软件和信息技术服务业）") + kv("适用地区", "辽宁省").replace(f"border-bottom: 1px solid {LINE}", "", 1) + "</div>")
    + card('<div style="padding: 6px 0 16px">' + section_title("原文摘录", f'<span style="font-size: 12px; color: {T3}">二、申报条件</span>')
           + f'<blockquote style="margin: 6px 0 0; padding: 12px 14px; background: #F7F8FA; border-radius: 8px; font-size: 14px; line-height: 1.8; color: {T1}">“（一）申报单位原则上应为制造业企业（以GB/T 4754-2017《国民经济行业分类》为准，含软件和信息技术服务业）。（二）财务管理机构和制度健全，纳税记录良好。（三）诚信守法，近三年未发生重大（含）以上安全、环保、质量事故（事件），未被纳入严重失信主体名单。”</blockquote></div>')
    + card('<div style="padding: 6px 0 2px">' + section_title("附件 2")
           + attach("PDF", R_TINT, RED, "附件1 辽宁省制造业固定资产贷款贴息及担保补贴实施细则（2026年）")
           + attach("XLSX", G_TINT, G_TXT, "附件2 银行贷款贴息项目审核情况汇总表")
           + f'<div style="position: relative; display: flex; align-items: center; padding: 11px 0 12px; font-size: 12.5px; color: {T2}"><span>附件在原文页面下载</span>'
           + f'<button type="button" onClick="{{{{copy_{ATT_CID}}}}}" style="margin-left: auto; display: inline-flex; align-items: center; gap: 3px; padding: 0; background: none; border: 0; font-family: inherit; font-size: 12.5px; color: {BLUE}; white-space: nowrap; cursor: pointer">{icon("copy", 14, BLUE, 1.9)}复制原文链接</button>{copy_bubble(ATT_CID)}</div></div>')
    + card(f'<div style="padding: 14px 0">'
           f'<div style="display: flex; align-items: center; justify-content: space-between"><div><div style="font-size: 12px; color: {T3}">原文出处</div><div style="margin-top: 2px; font-size: 14px">辽宁省工业和信息化厅网站</div></div>'
           f'<span style="position: relative"><button type="button" onClick="{{{{copy_{SRC_CID}}}}}" style="display: inline-flex; align-items: center; gap: 4px; padding: 0; background: none; border: 0; font-family: inherit; font-size: 14px; color: {BLUE}; cursor: pointer">{icon("copy", 15, BLUE, 2)}复制原文链接</button>{copy_bubble(SRC_CID)}</span></div>'
           f'<p style="margin: 10px 0 0; font-size: 12px; line-height: 1.7; color: {T2}">原文网址 gxt.ln.gov.cn。小程序内不能直接打开政府网站，复制链接后在浏览器打开。以上要点由有据按原文逐条整理，以原文为准，2026-09-26 核对。申报请在主管部门的申报系统办理；有据是第三方整理的信息工具，不是政府平台，不提供填报入口。发现不一致？<a href="Me.dc.html" style="white-space: nowrap">纠错</a></p></div>')
    + '<div style="height: 110px"></div>'
    + f'<div style="position: absolute; left: 0; right: 0; bottom: 0; height: 90px; box-sizing: border-box; padding: 8px 16px 30px; background: {CARD}; border-top: 1px solid {LINE}; display: flex; align-items: center; gap: 4px">'
    + "".join(f'<a href="{h}" style="width: 50px; height: 48px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1px; font-size: 11px; color: {T2}">{icon(i, 22, T2, 1.8)}<span>{t}</span></a>'
              for t, i, h in [("收藏", "star", "Me.dc.html"), ("导出", "download", "Files.dc.html"), ("纠错", "flag", "Me.dc.html")])
    + f'<span style="margin-left: auto; position: relative">{copy_bubble(BAR_CID, "right: 0; bottom: calc(100% + 8px)")}<button type="button" onClick="{{{{copy_{BAR_CID}}}}}" style="height: 44px; padding: 0 22px; display: flex; align-items: center; gap: 6px; background: {BLUE}; color: #FFFFFF; border: 0; border-radius: 22px; font-family: inherit; font-size: 15px; font-weight: 500; cursor: pointer">{icon("copy", 16, "#FFFFFF", 2)}复制原文链接</button></div>')
detail_js = ("const on = !!(this.state && this.state.remind);\n"
             f"    return {{ remindLabel: on ? '已订阅：截止前 3 天用微信提醒你一次' : '订阅截止提醒', remindStyle: on ? {js(REM_ON)} : {js(REM_OFF)},\n"
             "      toggleRemind: () => this.setState({ remind: !on }) };")
add("Detail", "有据 · 文件详情", 1600, detail_body, detail_js, dict(remindLabel="订阅截止提醒", remindStyle=REM_OFF))

# ---------- 申报日历 ----------
DUE = [
    dict(id="nx", day=16, date="10月16日", week="周五", time="", days=19, region="宁夏 · 科技厅", kind="申报通知",
         title="自治区科技厅关于组织申报2027年度自治区科技金融补助的通知", what="第一批推荐截止", link=False),
    dict(id="ln", day=18, date="10月18日", week="周日", time="", days=21, region="辽宁 · 工信厅", kind="申报通知",
         title=D["ln"]["title"], what="企业申报截止（原文：逾期申报平台自动关闭）· 市级推荐 10月20日", link=True),
    dict(id="jx", day=23, date="10月23日", week="周五", time="17:00", days=26, region="江西 · 科技厅", kind="申报通知",
         title=D["jx"]["title"], what="依托单位网上提交截止 · 推荐部门审核 10月29日 17:00", link=False),
    dict(id="hb", day=26, date="10月26日", week="周一", time="17:00", days=29, region="湖北 · 科技厅", kind="申报通知",
         title=D["hb"]["title"], what="申报单位网上申报截止 · 推荐单位审核 10月28日 17:00", link=False),
    dict(id="gz", day=29, date="10月29日", week="周四", time="18:00", days=32, region="贵州 · 科技厅", kind="申报通知",
         title="省科技厅关于发布2026年决策科学项目申报指南的通知", what="主管部门系统网上填报截止 · 9月29日 9:00 起开放", link=False),
]
SEG_ON = f"height: 32px; border: 0; border-radius: 16px; font-family: inherit; font-size: 13px; cursor: pointer; background: {BLUE}; color: #FFFFFF; font-weight: 500"
SEG_OFF = f"height: 32px; border: 0; border-radius: 16px; font-family: inherit; font-size: 13px; cursor: pointer; background: transparent; color: {T2}; font-weight: 400"
BELL_ON = f"width: 36px; height: 36px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; border-radius: 18px; cursor: pointer; background: {BLUE}; color: #FFFFFF; border: 1px solid {BLUE}"
BELL_OFF = f"width: 36px; height: 36px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; border-radius: 18px; cursor: pointer; background: {CARD}; color: {T2}; border: 1px solid {LINE2}"
CELL_OFF = "height: 40px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; padding: 0; border: 0; border-radius: 10px; background: transparent; font-family: inherit; cursor: pointer"
CELL_ON = CELL_OFF + f"; box-shadow: inset 0 0 0 2px {BLUE}"
ranges = [("7", "7 天内"), ("14", "14 天内"), ("30", "30 天内"), ("All", "全部")]
limits = {"7": 7, "14": 14, "30": 30, "All": 9999}
seg = (f'<div role="tablist" style="margin: 12px 12px 0; padding: 3px; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 3px; background: {CARD}; border-radius: 19px">'
       + "".join(f'<button type="button" role="tab" onClick="{{{{pick{k}}}}}" style="{{{{seg{k}}}}}">{label} {sum(1 for d in DUE if d["days"] <= limits[k])}</button>' for k, label in ranges)
       + "</div>")
cells = ['<span></span>'] * 3
due_days = {d["day"]: d for d in DUE}
for day in range(1, 32):
    wk = (3 + day - 1) % 7 >= 5
    d = due_days.get(day)
    num_color = T1 if d else (T3 if wk else T2)
    weight = 700 if d else 400
    dot = f'<span style="width: 5px; height: 5px; border-radius: 3px; background: {BLUE if d else "transparent"}"></span>'
    if d:
        cells.append(f'<button type="button" aria-label="10月{day}日有截止事项" aria-pressed="{{{{cp_{day}}}}}" onClick="{{{{sel_{day}}}}}" style="{{{{cell_{day}}}}}">'
                     f'<span style="font-size: 14px; font-weight: {weight}; color: {num_color}">{day}</span>{dot}</button>')
    else:
        cells.append(f'<div style="height: 40px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px">'
                     f'<span style="font-size: 14px; font-weight: {weight}; color: {num_color}">{day}</span>{dot}</div>')
cells.append("<span></span>")
month = card(f'<div style="padding: 14px 0 10px"><div style="display: flex; align-items: baseline; justify-content: space-between; padding-bottom: 8px">'
             f'<span style="font-size: 16px; font-weight: 600">2026年10月</span><span style="font-size: 12px; color: {T3}">蓝点：有申报截止，点日期只看当天</span></div>'
             f'<div style="display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); text-align: center; font-size: 12px; color: {T3}; padding-bottom: 4px">'
             + "".join(f"<span>{w}</span>" for w in "一二三四五六日") + '</div>'
             f'<div style="display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 2px">{"".join(cells)}</div></div>')
items = []
for d in DUE:
    title = (f'<a href="Detail.dc.html" style="color: {T1}">{d["title"]}</a>' if d["link"] else d["title"])
    items.append(f'<sc-if value="{{{{show_{d["id"]}}}}}" hint-placeholder-val="{{{{ true }}}}">'
                 f'<div style="margin: 10px 12px 0; padding: 14px 16px; background: {CARD}; border-radius: 12px">'
                 f'<div style="display: flex; align-items: center; gap: 8px"><span style="font-size: 16px; font-weight: 700">{d["date"]}</span>'
                 f'<span style="font-size: 13px; color: {T2}">{d["week"]} {d["time"]}</span>'
                 f'<span style="margin-left: auto; height: 22px; padding: 0 8px; display: inline-flex; align-items: center; border-radius: 11px; background: {BLUE_TINT}; color: {BLUE}; font-size: 12px; font-weight: 600">剩 {d["days"]} 天</span></div>'
                 f'<div style="margin-top: 10px; display: flex; gap: 12px"><div style="flex-grow: 1; min-width: 0">'
                 f'<div style="display: flex; gap: 6px; align-items: center">{tag_type(d["kind"])}<span style="font-size: 12px; color: {T2}">{d["region"]}</span></div>'
                 f'<div style="margin-top: 6px; font-size: 15px; font-weight: 600; line-height: 1.55">{title}</div>'
                 f'<div style="margin-top: 5px; font-size: 12.5px; line-height: 1.6; color: {T2}">{d["what"]}</div></div>'
                 f'<button type="button" aria-label="设截止提醒" aria-pressed="{{{{pr_{d["id"]}}}}}" onClick="{{{{tg_{d["id"]}}}}}" style="{{{{bell_{d["id"]}}}}}">{icon("bell", 17, "currentColor", 1.9)}</button></div></div></sc-if>')
empty = (f'<sc-if value="{{{{isEmpty}}}}" hint-placeholder-val="{{{{ false }}}}"><div style="margin: 10px 12px 0; padding: 28px 16px; text-align: center; background: {CARD}; border-radius: 12px">'
         f'<div style="font-size: 16px; font-weight: 600">{{{{emptyTitle}}}}</div><div style="margin-top: 6px; font-size: 13px; color: {T2}">最近的截止在 10月16日，还有 19 天</div>'
         f'<button type="button" onClick="{{{{pick30}}}}" style="margin-top: 14px; height: 36px; padding: 0 18px; background: {BLUE}; color: #FFFFFF; border: 0; border-radius: 18px; font-family: inherit; font-size: 14px; cursor: pointer">看 30 天内的 4 项</button></div></sc-if>')
cal_body = (navbar("申报日历") + notice_bar() + seg
    + f'<div style="margin: 10px 16px 0; display: flex; align-items: center; gap: 14px; font-size: 13px"><span>全部地区 ▾</span><span>全部行业 ▾</span><span style="margin-left: auto; font-size: 12px; color: {T3}">今天 9月27日</span></div>'
    + month
    + f'<sc-if value="{{{{hasSel}}}}" hint-placeholder-val="{{{{ false }}}}"><div style="margin: 12px 16px 0; display: flex; align-items: center; font-size: 13px; color: {T2}">'
    + f'<span>只看 <b style="color: {T1}">{{{{selLabel}}}}</b> 截止的事项</span><button type="button" onClick="{{{{clearSel}}}}" style="margin-left: auto; padding: 0; border: 0; background: none; font-family: inherit; font-size: 13px; color: {BLUE}; cursor: pointer">取消选择</button></div></sc-if>'
    + empty + "".join(items)
    + f'<p style="margin: 14px 16px 0; font-size: 12px; line-height: 1.7; color: {T3}">截止时间逐条取自原文。一个通知有多个截止时间时，按申报单位的截止时间排列，其余写在下方。申报请在主管部门的系统办理，有据不提供填报入口。</p>'
    + '<div style="height: 30px"></div>')
cal_js = ("const st = this.state || {};\n"
          "    const range = st.range || '30';\n"
          "    const sel = st.sel || 0;\n"
          "    const rem = st.rem || {};\n"
          f"    const DUE = {js([[d['id'], d['days'], d['day'], d['date']] for d in DUE])};\n"
          "    const limit = { '7': 7, '14': 14, '30': 30, All: 9999 }[range];\n"
          f"    const segOn = {js(SEG_ON)}, segOff = {js(SEG_OFF)}, bellOn = {js(BELL_ON)}, bellOff = {js(BELL_OFF)}, cellOn = {js(CELL_ON)}, cellOff = {js(CELL_OFF)};\n"
          "    const out = { isEmpty: !sel && DUE.every(([, d]) => d > limit), hasSel: !!sel };\n"
          "    out.selLabel = sel ? (DUE.find((x) => x[2] === sel) || [])[3] : '';\n"
          "    out.clearSel = () => this.setState({ sel: 0 });\n"
          "    out.emptyTitle = '未来 ' + { '7': '7 天', '14': '14 天', '30': '30 天', All: '' }[range] + '没有截止的申报事项';\n"
          "    ['7', '14', '30', 'All'].forEach((k) => { out['seg' + k] = k === range ? segOn : segOff; out['pick' + k] = () => this.setState({ range: k, sel: 0 }); });\n"
          "    DUE.forEach(([id, d, day]) => {\n"
          "      const on = !!rem[id];\n"
          "      out['show_' + id] = sel ? day === sel : d <= limit;\n"
          "      out['cell_' + day] = day === sel ? cellOn : cellOff;\n"
          "      out['cp_' + day] = day === sel ? 'true' : 'false';\n"
          "      out['sel_' + day] = () => this.setState({ sel: day === sel ? 0 : day });\n"
          "      out['bell_' + id] = on ? bellOn : bellOff;\n"
          "      out['pr_' + id] = on ? 'true' : 'false';\n"
          "      out['tg_' + id] = () => this.setState({ rem: { ...rem, [id]: !on } });\n"
          "    });\n"
          "    return out;")
cal_defaults = {"isEmpty": False, "emptyTitle": "", "hasSel": False, "selLabel": "", "seg7": SEG_OFF, "seg14": SEG_OFF, "seg30": SEG_ON, "segAll": SEG_OFF}
for d in DUE:
    cal_defaults.update({f"show_{d['id']}": d["days"] <= 30, f"bell_{d['id']}": BELL_OFF, f"pr_{d['id']}": "false",
                         f"cell_{d['day']}": CELL_OFF, f"cp_{d['day']}": "false"})
add("Calendar", "有据 · 申报日历", 1345, cal_body, cal_js, cal_defaults)

# ---------- 查名单 ----------
list_docs = [
    D["bj"], D["sh"],
    dict(kind="名单", status="正式公布", region="西藏 · 省级", title="关于公布西藏自治区2026年度绿色制造名单的通知", issuer="西藏自治区经济和信息化厅", date="2026-09-24",
         url="http://jxt.xizang.gov.cn/xzweb/detail?id=3808&channelId=9&categoryId=69"),
    dict(kind="名单", status="公示", region="新疆 · 省级", title="2026年度自治区级绿色工厂拟定名单公示", issuer="新疆维吾尔自治区工业和信息化厅", date="2026-09-23",
         url="https://gxt.xinjiang.gov.cn/gxt/tzgg/202609/fc11014f78974fa7a37ee1677fcdc2e0.shtml"),
    D["sx"],
    dict(kind="名单", status="公示", region="河南 · 省级", title="关于河南省2026年度第三批拟更名和重大变化高新技术企业的公示", issuer="河南省科学技术厅", date="2026-09-22",
         url="http://kjt.henan.gov.cn/2026/09-22/3431626.html"),
]
lchip = lambda t, on=False: ('<span style="height: 30px; padding: 0 12px; display: inline-flex; align-items: center; border-radius: 15px; font-size: 13px; white-space: nowrap; '
                             + (f'background: {BLUE_TINT}; color: {BLUE}; font-weight: 500' if on else f'background: {FILL}; color: {T1}') + f'">{t}</span>')
lists_body = (f'<div style="height: 250px; background: linear-gradient(180deg, {BLUE_G1} 0%, {BLUE_G2} 100%); color: #FFFFFF; padding: 0 16px; box-sizing: border-box; position: relative">'
    + f'<div style="height: 48px; display: flex; align-items: center; justify-content: center; position: relative"><a href="Main.dc.html" aria-label="返回" style="position: absolute; left: -10px; top: 4px; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center">{icon("back", 22, "#FFFFFF", 2)}</a>'
    + f'<span style="font-size: 17px; font-weight: 600">查名单</span>{capsule(True).replace("right: 10px", "right: -6px")}</div>'
    + '<div style="margin-top: 10px; font-size: 20px; font-weight: 700">这家企业上过哪些名单？</div>'
    + '<div style="margin-top: 2px; font-size: 12.5px; color: rgba(255,255,255,0.85)">高新技术企业、专精特新、科技型中小企业等，公示和正式公布分开标注</div>'
    + notice_bar(True).replace("justify-content: center", "justify-content: flex-start")
    + '<form role="search" style="margin-top: 14px; height: 44px; display: flex; align-items: center; gap: 8px; padding: 0 4px 0 14px; background: #FFFFFF; border-radius: 22px; box-shadow: 0 4px 14px rgba(20, 45, 120, 0.18)">'
    + f'{icon("search", 18, T3, 2)}<input type="search" aria-label="企业或机构全称" placeholder="输入企业全称或统一社会信用代码" style="flex-grow: 1; min-width: 0; height: 40px; border: 0; outline: none; background: transparent; font-family: inherit; font-size: 14px; color: {T1}">'
    + f'<button type="submit" style="height: 36px; padding: 0 18px; background: {BLUE}; color: #FFFFFF; border: 0; border-radius: 18px; font-family: inherit; font-size: 14px; font-weight: 500; cursor: pointer">查询</button></form>'
    + '<div style="margin-top: 10px; font-size: 12.5px; color: rgba(255,255,255,0.9)">批量比对：粘贴多个企业全称，一次查完 · 即将上线</div></div>'
    + f'<p style="margin: 10px 16px 0; font-size: 12px; line-height: 1.6; color: {T3}">按企业全称逐行比对名单原文；名称相近的企业分开列出，请核对统一社会信用代码。只查企业和机构，不查个人。</p>'
    + card(section_title("最近发布的名单", f'<span style="font-size: 12px; color: {T3}">本周 70 份</span>')
           + f'<div style="display: flex; gap: 8px; padding: 6px 0 10px; overflow: hidden">{lchip("全部", True)}{lchip("科技型中小企业")}{lchip("专精特新")}{lchip("更多 ▾")}</div>'
           + f'<div style="border-top: 1px solid {LINE}">' + "".join(doc_row(d, last=(i == len(list_docs) - 1)) for i, d in enumerate(list_docs)) + load_more(6, 70) + "</div>")
    + '<div style="height: 30px"></div>')
add("Lists", "有据 · 查名单", 1240, lists_body)

# ---------- 关注 ----------
SW_ON = f"width: 50px; height: 30px; flex-shrink: 0; position: relative; border: 0; border-radius: 15px; padding: 0; cursor: pointer; background: {BLUE}"
SW_OFF = "width: 50px; height: 30px; flex-shrink: 0; position: relative; border: 0; border-radius: 15px; padding: 0; cursor: pointer; background: #C9CDD4"
KNOB_ON = "position: absolute; top: 3px; left: 23px; width: 24px; height: 24px; border-radius: 12px; background: #FFFFFF; box-shadow: 0 1px 3px rgba(0,0,0,0.2)"
KNOB_OFF = KNOB_ON.replace("left: 23px", "left: 3px")
sub = lambda name, desc, n, ic, g1, g2: (f'<a href="Search.dc.html" style="display: flex; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid {LINE}; color: {T1}">'
                                         f'<span style="width: 38px; height: 38px; flex-shrink: 0; border-radius: 11px; background: linear-gradient(145deg, {g1}, {g2}); display: flex; align-items: center; justify-content: center">{icon(ic, 20, "#FFFFFF", 2)}</span>'
                                         f'<div style="flex-grow: 1; min-width: 0"><div style="font-size: 15px; font-weight: 600">{name}</div><div style="margin-top: 1px; font-size: 12px; color: {T2}">{desc}</div></div>'
                                         f'<span style="height: 22px; padding: 0 8px; display: inline-flex; align-items: center; border-radius: 11px; background: {BLUE_TINT}; color: {BLUE}; font-size: 12px; font-weight: 600">本周 {n}</span>'
                                         f'{icon("chev", 16, T3, 2)}</a>')
change = lambda label, d, href=None, last=False: (f'<div style="padding: 12px 0; {"" if last else f"border-bottom: 1px solid {LINE}"}"><div style="display: flex; gap: 6px; align-items: center">{tag_type(d["kind"])}<span style="font-size: 12px; color: {T2}">{label}</span>'
                                      + (deadline(d["deadline"], d["days"]) if d.get("deadline") else "")
                                      + '</div>'
                                      + (f'<a href="{href}" style="display: block; margin-top: 6px; font-size: 15px; font-weight: 600; line-height: 1.55; color: {T1}">{d["title"]}</a>' if href
                                         else f'<div style="margin-top: 6px; font-size: 15px; font-weight: 600; line-height: 1.55">{d["title"]}</div>')
                                      + meta_line(d))
bj_hi = dict(kind="名单", title="关于北京市2026年第四批高新技术企业更名的通知", issuer="北京市科学技术委员会、中关村科技园区管理委员会", date="2026-09-22",
             url="https://kw.beijing.gov.cn/zwgk/tzgg/202609/t20260922_4876631.html")
hn = dict(kind="申报通知", title="关于组织开展2026年第二批专精特新中小企业申报和复核工作的通知", issuer="海南省工业和信息化厅", date="2026-09-21",
          url="https://iitb.hainan.gov.cn/iitb/tzgg/202609/5e951788f1024a86a684fb8aa528ab77.shtml")
follow_body = (navbar("关注", back=None) + notice_bar()
    + f'<div style="margin: 8px 12px 0; padding: 8px 12px; background: {BLUE_TINT}; border-radius: 8px; font-size: 12px; color: {T2}">订阅和导出记录暂存在本机，<a href="Me.dc.html">登录</a>后可同步到其他设备</div>'
    + f'<div style="margin: 12px 12px 0; padding: 16px; border-radius: 12px; background: linear-gradient(135deg, {BLUE_G1}, {BLUE_G2}); color: #FFFFFF">'
    + '<div style="font-size: 13px; color: rgba(255,255,255,0.85)">本周（9月21日起）你关注的条件有</div>'
    + '<div style="margin-top: 4px; display: flex; align-items: baseline; gap: 6px"><span style="font-size: 34px; font-weight: 700; line-height: 1.1">10</span><span style="font-size: 15px">份新文件</span>'
    + '<a href="Search.dc.html" style="margin-left: auto; height: 28px; padding: 0 12px; display: inline-flex; align-items: center; border-radius: 14px; background: rgba(255,255,255,0.18); color: #FFFFFF; font-size: 13px">全部查看</a></div></div>'
    + card(section_title("我的订阅 4", '<a href="Search.dc.html" style="font-size: 13px">管理</a>')
           + sub("专精特新", "全国 · 申报通知、名单与公示", 6, "list", "#FFAB5C", ORANGE)
           + sub("高新技术企业", "全国 · 申报通知、名单与公示", 2, "list", "#FFAB5C", ORANGE)
           + sub("辽宁 · 工信部门", "申报通知", 1, "apply", "#5B8CFF", BLUE)
           + sub("山东 · 科技型中小企业", "名单与公示", 1, "list", "#FFAB5C", ORANGE).replace(f"border-bottom: 1px solid {LINE}; color", "color", 1)
           + f'<a href="Filter.dc.html" style="margin: 4px 0 14px; height: 40px; display: flex; align-items: center; justify-content: center; gap: 6px; border: 1px dashed {BLUE}; border-radius: 20px; font-size: 14px; color: {BLUE}">{icon("plus", 16, BLUE, 2)}按 地区 × 主题 × 类型 添加</a>')
    + card(section_title("本周变化")
           + change("辽宁 · 工信部门", D["ln"], "Detail.dc.html") + change("专精特新", D["sx"]) + change("高新技术企业", bj_hi)
           + change("专精特新", hn, last=True) + load_more(4, 10))
    + card(f'<div style="padding: 14px 0; display: flex; align-items: center; gap: 12px"><div style="flex-grow: 1"><div style="font-size: 15px; font-weight: 600">微信提醒</div>'
           f'<div style="margin-top: 2px; font-size: 12px; line-height: 1.6; color: {T2}">按次订阅：每订阅一次，有新文件时用微信提醒你一次</div></div>'
           f'<button type="button" aria-pressed="{{{{subPressed}}}}" onClick="{{{{toggleSub}}}}" style="{{{{subStyle}}}}">{{{{subLabel}}}}</button></div>')
    + '<div style="height: 100px"></div>' + tabbar("关注"))
SUB_OFF = f"height: 32px; padding: 0 14px; flex-shrink: 0; border-radius: 16px; font-family: inherit; font-size: 13px; font-weight: 500; cursor: pointer; background: {BLUE}; color: #FFFFFF; border: 1px solid {BLUE}"
SUB_ON = f"height: 32px; padding: 0 14px; flex-shrink: 0; border-radius: 16px; font-family: inherit; font-size: 13px; font-weight: 500; cursor: pointer; background: {CARD}; color: {T2}; border: 1px solid {LINE2}"
follow_js = ("const on = !!(this.state && this.state.sub);\n"
             f"    return {{ subLabel: on ? '已订阅 1 次' : '订阅一次', subPressed: on ? 'true' : 'false', subStyle: on ? {js(SUB_ON)} : {js(SUB_OFF)},\n"
             "      toggleSub: () => this.setState({ sub: !on }) };")
add("Follow", "有据 · 关注", 1430, follow_body, follow_js, dict(subLabel="订阅一次", subPressed="false", subStyle=SUB_OFF))

# ---------- 文件 ----------
def frow(badge, bg, fg, name, meta, last=False, expired=False):
    btn = f"height: 28px; padding: 0 12px; border-radius: 14px; border: 1px solid {LINE2}; background: {CARD}; font-family: inherit; font-size: 12.5px; color: {T1}; cursor: pointer"
    if expired:
        actions = (f'<span style="height: 20px; padding: 0 6px; display: inline-flex; align-items: center; border-radius: 4px; background: {FILL}; color: {T3}; font-size: 12px">已失效</span>'
                   f'<button type="button" style="{btn}; border-color: {BLUE}; color: {BLUE}">重新导出</button>')
    else:
        actions = f'<button type="button" style="{btn}">发到电脑</button><button type="button" style="{btn}">转发给好友</button>'
    border = "" if last else f"border-bottom: 1px solid {LINE};"
    op = "opacity: 0.6;" if expired else ""
    return (f'<div style="padding: 14px 0; display: flex; gap: 12px; {border}">'
            f'<span style="width: 40px; height: 40px; flex-shrink: 0; border-radius: 10px; background: {bg}; color: {fg}; font-size: 10px; font-weight: 700; display: flex; align-items: center; justify-content: center; {op}">{badge}</span>'
            f'<div style="flex-grow: 1; min-width: 0"><div style="font-size: 15px; font-weight: 600; {op}">{name}</div><div style="margin-top: 2px; font-size: 12.5px; color: {T2}">{meta}</div>'
            f'<div style="margin-top: 8px; display: flex; align-items: center; gap: 8px">{actions}</div></div></div>')


files_body = (navbar("文件", back=None) + notice_bar()
    + f'<div style="margin: 8px 12px 0; padding: 8px 12px; background: {BLUE_TINT}; border-radius: 8px; font-size: 12px; color: {T2}">导出记录暂存在本机，<a href="Me.dc.html">登录</a>后可同步到其他设备</div>'
    + f'<div style="margin: 12px 16px 0; font-size: 12.5px; line-height: 1.7; color: {T2}">导出的表格和文档都在这里。每份都带原文链接和数据说明（来源网站、核对日期、以原文为准），可以直接转给同事或客户。</div>'
    + card(section_title("导出记录 3")
           + frow("XLSX", G_TINT, G_TXT, "科技型中小企业 · 近 30 天", "35 份 · Excel · 21 KB · 今天 10:42")
           + frow("XLSX", G_TINT, G_TXT, "本周新增 · 全部类型", "158 份 · Excel · 64 KB · 今天 08:30")
           + frow("DOCX", BLUE_TINT, BLUE, "辽宁贷款贴息及担保补贴申报要点", "1 份 · Word · 18 KB · 9月19日", last=True, expired=True))
    + f'<p style="margin: 14px 16px 0; font-size: 12px; line-height: 1.7; color: {T3}">在搜索结果里点"选择"，或在文件详情里点"导出"，勾选需要的文件即可生成。</p>'
    + tabbar("文件"))
add("Files", "有据 · 文件", 900, files_body)

# ---------- 我的 ----------
fn = lambda label, ic, bg, fg, href: (f'<a href="{href}" style="display: flex; flex-direction: column; align-items: center; gap: 6px; color: {T1}">'
                                      f'<span style="width: 40px; height: 40px; border-radius: 12px; background: {bg}; display: flex; align-items: center; justify-content: center">{icon(ic, 21, fg, 1.9)}</span>'
                                      f'<span style="font-size: 12.5px">{label}</span></a>')
mrow = lambda label, ic, href, last=False: (f'<a href="{href}" style="display: flex; align-items: center; gap: 12px; padding: 14px 0; {"" if last else f"border-bottom: 1px solid {LINE};"} color: {T1}">'
                                            f'{icon(ic, 20, T2, 1.8)}<span style="flex-grow: 1; font-size: 15px">{label}</span>{icon("chev", 16, T3, 2)}</a>')
me_body = (f'<div style="height: 210px; background: linear-gradient(180deg, {BLUE_G1} 0%, {BLUE_G2} 100%); color: #FFFFFF; padding: 0 16px; box-sizing: border-box; position: relative">'
    + f'<div style="height: 48px; display: flex; align-items: center; justify-content: center; position: relative"><span style="font-size: 17px; font-weight: 600">我的</span>{capsule(True).replace("right: 10px", "right: -6px")}</div>'
    + notice_bar(True)
    + '<div style="margin-top: 10px; display: flex; align-items: center; gap: 14px">'
    + f'<span style="width: 56px; height: 56px; flex-shrink: 0; border-radius: 28px; background: rgba(255,255,255,0.2); display: flex; align-items: center; justify-content: center">{icon("user", 28, "#FFFFFF", 1.8)}</span>'
    + '<div style="flex-grow: 1"><div style="font-size: 18px; font-weight: 600">未登录</div><div style="margin-top: 2px; font-size: 12px; color: rgba(255,255,255,0.85)">记录暂存在本机，登录后可同步到其他设备</div></div>'
    + '<button type="button" aria-disabled="{{loginDisabled}}" onClick="{{tryLogin}}" style="{{loginStyle}}">微信登录</button></div>'
    + '<div style="margin-top: 12px; position: relative; display: flex; align-items: center; font-size: 11.5px; color: rgba(255,255,255,0.85)">'
    + '<button type="button" role="checkbox" aria-checked="{{agreedAria}}" onClick="{{toggleAgree}}" style="display: inline-flex; align-items: center; gap: 6px; padding: 0; border: 0; background: none; font-family: inherit; font-size: 11.5px; color: rgba(255,255,255,0.85); cursor: pointer">'
    + f'<span style="{{{{boxStyle}}}}">{icon("check", 12, "currentColor", 2.8)}</span>我已阅读并同意</button>'
    + '<a href="Me.dc.html" style="color: #FFFFFF">《用户协议》</a>和<a href="Me.dc.html" style="color: #FFFFFF">《隐私保护指引》</a>'
    + '<sc-if value="{{nudge}}" hint-placeholder-val="{{ false }}"><span role="alert" style="position: absolute; left: -4px; bottom: calc(100% + 8px); z-index: 5; padding: 6px 10px; border-radius: 6px; background: rgba(29, 33, 41, 0.92); color: #FFFFFF; font-size: 12px; line-height: 1.4; white-space: nowrap">请先勾选同意《用户协议》和《隐私保护指引》</span></sc-if></div></div>'
    + f'<nav aria-label="常用功能" style="margin: -16px 12px 0; position: relative; padding: 16px 4px 14px; background: {CARD}; border-radius: 12px; box-shadow: 0 2px 10px rgba(29, 33, 41, 0.05); display: grid; grid-template-columns: repeat(4, minmax(0, 1fr))">'
    + fn("收藏", "star", O_TINT, ORANGE, "Search.dc.html") + fn("导出记录", "download", G_TINT, GREEN, "Files.dc.html")
    + fn("我的提醒", "bell", BLUE_TINT, BLUE, "Calendar.dc.html") + fn("纠错", "flag", R_TINT, RED, "Me.dc.html") + "</nav>"
    + card(section_title("数据从哪里来")
           + '<div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); padding: 8px 0 12px; text-align: center">'
           + "".join(f'<div style="{"border-left: 1px solid " + LINE if i else ""}"><div style="font-size: 20px; font-weight: 700; color: {BLUE}">{n}</div><div style="font-size: 12px; color: {T2}">{t}</div></div>'
                     for i, (n, t) in enumerate([("72", "政府网站栏目"), ("28", "个省 + 国家部委"), ("1–4", "次 / 天检查")]))
           + f'</div><p style="margin: 0; padding: 10px 0 14px; border-top: 1px solid {LINE}; font-size: 12.5px; line-height: 1.75; color: {T2}">只收录政府网站公开发布的文件，每条都附原文链接。有据是第三方整理的信息工具，不是政府官方平台，也不代办申报。网站拒绝程序访问的，我们尊重、不绕过：广西、甘肃暂未覆盖；浙江正在接入。</p>')
    + card(mrow("申请覆盖：你关心的地区还没有？", "plus", "Me.dc.html") + mrow("用户协议与隐私保护指引", "shield", "Me.dc.html") + mrow("设置", "gear", "Me.dc.html") + mrow("关于有据", "info", "Me.dc.html", last=True))
    + f'<div style="margin-top: 16px; text-align: center; font-size: 12px; color: {T3}">有据 · 第三方整理 · 每条都有出处</div>'
    + '<div style="height: 100px"></div>' + tabbar("我的"))
LOGIN_ON = f"flex-shrink: 0; height: 32px; padding: 0 14px; border: 0; border-radius: 16px; background: #FFFFFF; color: {BLUE}; font-family: inherit; font-size: 14px; font-weight: 600; white-space: nowrap; cursor: pointer"
LOGIN_OFF = LOGIN_ON.replace(f"background: #FFFFFF; color: {BLUE}", "background: rgba(255,255,255,0.28); color: rgba(255,255,255,0.75)").replace("cursor: pointer", "cursor: not-allowed")
BOX_ON = f"width: 16px; height: 16px; flex-shrink: 0; box-sizing: border-box; display: flex; align-items: center; justify-content: center; border: 1.5px solid #FFFFFF; border-radius: 4px; background: #FFFFFF; color: {BLUE}"
BOX_OFF = BOX_ON.replace(f"background: #FFFFFF; color: {BLUE}", "background: transparent; color: transparent")
me_js = ("const st = this.state || {};\n"
         "    const agreed = !!st.agreed;\n"
         f"    return {{ agreedAria: agreed ? 'true' : 'false', boxStyle: agreed ? {js(BOX_ON)} : {js(BOX_OFF)},\n"
         "      toggleAgree: () => this.setState({ agreed: !agreed, nudge: false }),\n"
         f"      loginStyle: agreed ? {js(LOGIN_ON)} : {js(LOGIN_OFF)}, loginDisabled: agreed ? 'false' : 'true',\n"
         "      tryLogin: () => { if (!agreed) this.setState({ nudge: true }); },\n"
         "      nudge: !!st.nudge && !agreed };")
add("Me", "有据 · 我的", 900, me_body, me_js, dict(agreedAria="false", boxStyle=BOX_OFF, loginStyle=LOGIN_OFF, loginDisabled="true", nudge=False))

# ---------- 输出 ----------
DC_DIR.mkdir(parents=True, exist_ok=True)
ST_DIR.mkdir(parents=True, exist_ok=True)
for name, p in pages.items():
    page_js, page_defaults = with_copy(p["js"], p["body"], p["defaults"])
    (DC_DIR / f"{name}.dc.html").write_text(dc_page(p["title"], p["height"], p["body"], page_js, p["bg"]), encoding="utf-8")
    (ST_DIR / f"{name}.html").write_text(static_preview(p["height"], p["body"], page_defaults, p["bg"]), encoding="utf-8")
    print(name, p["height"])
