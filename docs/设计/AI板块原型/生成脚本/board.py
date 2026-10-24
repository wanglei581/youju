"""AI 板块原型的看板页面（给创始人看）：说明在上，两组手机画面按顺序排开，后面是发现、走查和数据出处。
画面本身是小程序的浅色界面，不跟主题变；看板的底色和文字跟随浅色、深色主题。"""

from ds import FONTS_LINK

# 和 10/17 对话版设计稿（#278）比，哪些做法改了（推进计划 #336 第二节）
CHANGES = [
    ("首页搜索框下面一条「AI 助手」，不进底栏", "底栏多了第 5 格「AI」，放在中间（D-075）", "底栏有「AI」"),
    ("一个对话页包办所有事（解读、对照都在对话里）", "AI 首页三项并列：AI 解读、条款对照、问一问；对话就是「问一问」（D-078）", "只有前两项"),
    ("详情卡片推荐放截止时间下面", "留在「要点」下面，标题卡加一行「这份通知可以 AI 解读 ↓」（你 10/17 在手机上看过定的，D-075）", "和定的一样"),
    ("回答先一句概括再贴原句", "第一期只摘原句，不写结论（D-078）", "—"),
    ("输入框左边 ＋ 能拍照、拍营业执照，还能联网查", "第一期不放相机；图片第二期、联网第三期（D-078）", "—"),
]

# 下一版比现在多了什么（按页）
ADDED = [
    ("AI 首页", "第三行「问一问」；三行能点（点了先选一份通知）；刚才看的通知加「问这份通知 ›」；记录里列对话；页底公示模型和备案号"),
    ("通知详情", "「要点」下面的卡片在「开始解读」下面加「问这份通知」，卡片位置不动"),
    ("问一问对话页（新页面）", "三个技能：找通知、问这份通知、查名单；第一次用先同意四句说明；回答只摘原句；没有相机"),
    ("解读、对照结果页", "最下面加「接着问这份通知」"),
]

# 画的时候发现的（10/22），处理去向写在后面
FINDINGS = [
    ("#328 找通知的两句示例，10/22 线上都查不到",
     "「找广东今年的专精特新申报通知」「找四川近 30 天的科技项目申报」按开发设计的规则查，线上今天都是 0 份；第一次用的人点了示例就看到「没找到」。"
     "原型换成查得到的两句（近 30 天 5G 工厂 5 份、宁夏近 7 天 3 份）。示例写死在页面上会过时，建议按收录里最近有的地区现取，上线前用线上数据逐个核。",
     "转给 #328 第 4 轮"),
    ("#328 取关键词的规则漏了「通知」二字",
     "停用词里没有「通知」，类型只认第一个命中的词（「申报」），所以「……专精特新申报通知」会剩下关键词「专精特新通知」，按标题永远找不到。请在规则里写明把类型词（通知、公告、公示……）也去掉。",
     "转给 #328 第 4 轮"),
    ("#328 找通知的截止没写是谁的",
     "D-078 第五节要求找通知照「截止」页的写法标出「企业申报截止」「区县报送截止」；#328 让找通知用列表行组件显示，这个组件只写「10月30日截止」。原型按 D-078 画，#328 要定是给列表行加上，还是找通知单独写。",
     "转给 #328 第 4 轮"),
    ("走查后改的说法（问一问）",
     "回答开头写明「可能没摘全」；「看清是谁的时间」只在摘到两个以上日期或审核、报送环节时加，一个日期都没摘到时说一句；没有附件的写「这份通知没有附件」；「问这份通知」用不了时原因直接写出来、给下一步；"
     "输入框下只写还剩几次，20 句用满时再说；三项的次数各算各的；查名单写不按年份筛、收录从几月起；页顶写只有问这份通知的回答是 AI 生成；行上按钮统一叫「问这份通知」。原型已经这样画。",
     "转给 #328 第十二节第 3 条"),
    ("解读结果页「正文里没找到」那句，读附件以后要跟着改",
     "现在写「可能写在附件里，也可能原文没写」；附件读入第二步做完以后，没有附件的照实写「这份通知没有附件」，读了附件的写「正文和附件里都没找到」（下一版 7 已这样画）。",
     "转给附件读入第二步"),
    ("当前：AI 首页两行点不了",
     "只能点下面的「去找一份通知」。第 0 步第 1 处（另一会话在做），下一版画成能点。",
     "第 0 步在做"),
    ("当前：正文超过 2.4 万字那种，卡片上没有按钮",
     "只写「请先看要点和原文」，没有按钮。#349（10/23 最新）这一种也还没给。",
     "开放前走查时补；建议和其他几种一样给「站内阅读正文」，转给 #349"),
    ("原来选的主例子，原网站当天撤稿了，收录里还当作正常的",
     "宁夏科技厅《技术交易补助》那份（10/21 发）10/22 下午原网页显示「已撤稿或删除」，通知列表里也没了；收录里它还能 AI 解读，也还在 AI 首页「近 7 天能解读的申报通知」里（当前 1 照线上原样画）。"
     "链接检查每周一次、连续两次打不开才标，撤稿到发现最长一两周。主例子换成湖北科技厅那份（原网页核过）。",
     "#347 接着做了：近 30 天的通知每天查，失效的不再给 AI 解读"),
    ("湖北科技厅的正文带了网页上的字",
     "收录的正文开头带「当前位置：首页>科技动态>通知公告」、标题、发布时间，末尾带「欢迎关注湖北科技公众号」「相关文档」等，详情页正文和 AI 读到的都有（当前 2 的正文开头能看到）。抽正文时该去掉。",
     "#351（另一会话）、#345（Codex）在做；下一版 2 按去掉以后的画"),
    ("当前：我的 AI 记录里，同一份通知解读几次，几行一模一样",
     "记录只写日期（「2026-10-21 · 已完成」），同一天同一份通知的几条分不出来。可以写到几点几分，或者合成一条。",
     "第 0 步第 3 处一起看"),
    ("顶部出处说明条还写「每条附原文链接」",
     "agy、Grok 10/23 都提：正文、附件都在小程序里读以后，这句容易被看成要去外链、复制网址。下一版画成「第三方整理 · 非政府平台 · 每条都有出处」（产品原来页脚的说法）。全站组件，#349 现在没改。",
     "转给 #349 定，原型跟着改"),
    ("「正在找」「没做成」是系统提示，不该标「AI 生成」",
     "agy 10/23 提：这两种不是 AI 生成的，标了是误标。下一版不标；「没找到」「范围外」「要问清楚」是模型判断后的回答，标「AI 生成」、末尾写模型和备案号（#328 第十二节「卡片末尾」）。",
     "转给 #328 写明"),
    ("查名单查到以后，在小程序里看不到企业在名单里的那一行",
     "Codex 10/24 提：现在只有「按名称对上」和「查看详情」，名单的站内阅读页又只说「名单仅提供已提取的企业查询结果」，审核员照原路走还是看不到那一行（10/23 退回附图就是这条路）。"
     "下一版 6 直接给出名单原文里的这一行（所在附件、赛道、表头；10/24 从新疆工信厅原附件核对，团队成员是个人姓名不显示），再给「在名单原文里看这一行」。",
     "等站内闭环第 4 项（名单站内可读、命中定位）"),
    ("#328 还有两处跟 10/23 以后的做法不一样",
     "出处是附件、打不开的「复制附件链接」：10/23 以后不能用复制网址兜底，照 #349 进站内阅读页、写明读不了的原因；页面底部「第三方整理 · 非政府平台 · 投诉举报」照 #339 只留「投诉举报」（原型已这样画）。",
     "转给 #328"),
]

# 第一次用的人走一遍、审核和合规挑错（材料只有界面文字和公开通知，发出前查过没有公司信息和密钥）
WALK = {
    "intro": "Grok 扮第一次用的企业经办人走 6 个任务、再扮申报顾问看名字和截图风险；agy 扮小程序审核人员和生成式 AI 合规审查员，后几轮也扮经办人走 6 个任务。最后一轮两位都是「剩余问题：没有」。每轮的意见都对照 D-075、D-078 和 #328 核过，采纳的已改进画面。",
    "rows": [
        ("Grok · 第 1 轮（经办人 6 个任务）",
         "10 条：「追问」像会给结论；次数和 20 句的说法分在三处；河北那行看不出 10 月 30 日是谁的截止；"
         "「请逐句对照」没说可能不全；「最早的发布于 2026 年」看不懂；查名单没查到的样子没画；没找到没有下一步；"
         "「5 次用完」没说是哪一项；「没选通知的提问」认不出是哪次；「回来接着看」没说从哪回来",
         "都改了：首页说明改成「找通知、查名单；或就一份通知提问，AI 只摘原句」；输入框下面一句写清；截止标出是谁的（D-078 第五节）；"
         "开头写「可能没摘全」；收录从几月起、不按年份筛；补了查名单没查到（线上真实结果）；没找到给「换个说法再问、看原文」；"
         "记录写最近一问；写明去「我的 AI 记录」接着看"),
        ("Grok · 第 1 轮（申报顾问）",
         "同一个动作三个叫法（问这份通知、问这份、接着问这份通知）；固定说法会被读成已经找全；没有日期也提醒「看清是谁的时间」；"
         "「读了：正文」看不出有没有附件；找通知的「10月30日截止」截图会被当成确定的截止",
         "前四条改了（行上按钮统一叫「问这份通知」，「接着问这份通知」是 D-078 定的叫法、保留）；截止那条不改：D-061 定的显示规则，机器摘录带「机器摘录·未经人工核对」和原句"),
        ("agy · 第 1 轮（审核和合规）",
         "问一问页顶的「AI 生成」在找通知、查名单时也挂着，会误标；AI 首页那条也建议去掉；公示、不判断能不能申报、个人信息、不出现「暂未开放」都符合",
         "问一问页顶改成「问这份通知的回答由 AI 生成 · 以原文为准」；AI 首页那条不动：D-075 定的显式标识"),
        ("Grok · 第 2 轮（复核）",
         "第 1 轮 10 条里 7 条改好，其余随 agy 第 2 轮一起改；新提 4 条：没找到说「看原文」却没有链接；「已下架」没有下一步；问截止只摘到「常年受理」、没说原句里没有日期；20 句没说和 5 次分开算",
         "4 条都改了（原句里没有日期时程序加一句「摘到的原句里没有写具体日期」，只说原句的事实）；要回答写「这不是截止日」、补外省能不能报，是结论或要 AI 判断，D-078 不让写"),
        ("agy · 第 2 轮（经办人 6 个任务）",
         "任务 2、3、6 顺利。5 条：三行都写「还剩 5 次」看不出各算各的；灰掉的「问这份通知」看不出为什么；重庆那行没按钮也没说为什么；「5 次」和「20 句」放在一起绕；记录里「（没选通知）」生硬",
         "都改了：三行下面加「三项的次数各算各的」；没选通知的说明挪到按钮正下面；找通知结果最后加一句为什么有的没有按钮；输入框下只写 5 次，20 句用满时再说；记录写「问一问 · 最近一问：……」"),
        ("agy · 第 3 轮（经办人 6 个任务）",
         "6 个任务都顺利；剩余问题：没有",
         "—"),
        ("Grok · 第 3 轮（经办人兼顾问 6 个任务）",
         "6 个任务都顺利；1 条：「后台处理不了」没说点「重试」算不算次数，用户会不敢点",
         "改了：写「点“重试”不算次数」"),
        ("agy · 第 4 轮（换主例子以后）",
         "6 个任务都顺利；1 条：「对成立时间有什么要求？」这个例子对湖北那份不适用，点了会答「没找到」",
         "改了：例子是所有通知共用的，换成每份都答得上的「要满足哪些条件？」"),
        ("Grok · 第 4 轮（换主例子以后）",
         "4 条：「看清是谁的时间」那句举的区县、纸质材料湖北这份里没有，会看岔；下一版结果页「可能写在附件里」对没附件的不对；按钮变灰的「暂时不可用」看不出发出后失败扣没扣；记录里「最近一问」应是后来查名单那句",
         "都改了：时间那句不举具体环节；下一版写「这份通知没有附件」；补画「发出以后没做成」；记录写最近一问"),
        ("agy · 第 5 轮（最后确认）",
         "6 个任务都顺利；剩余问题：没有",
         "—"),
        ("Grok · 第 5 轮（最后确认）",
         "6 个任务都顺利；剩余问题：没有",
         "—"),
        ("Grok · 10/23 外跳和征信用词排查",
         "#338 9 处：原型把「复制原文链接」当主按钮或兜底（列表行、详情底部、卡片下一步）；来源做成可点的外链；湖北正文混进网页上的字；查名单说明像查信用；出处说明条「每条附原文链接」",
         "下一版画了的都改了：列表行「查看详情」、详情「站内阅读」、正文去掉网页上的字（照 #349、#345）；卡片不能解读时的下一步没画下一版，#349 改成「站内阅读正文」；查名单的说明改了（下两行又改了一次）。当前版本照线上原样画、加了说明；看板底下的来源链接是给你核对用的，不在小程序里；出处说明条跟 #349 定"),
        ("agy · 10/23 合规（只看下一版）",
         "10 条：①出处说明条「每条附原文链接」；②没找到的「看原文」像外跳；③查名单说明带「信贷、逾期、失信」；④查名单收信用代码像企业信息查询；⑤记录里查名单那条挂在「我的 AI 记录」；"
         "⑥页顶 AI 条在找通知、查名单时也在；⑦「正在找」「没做成」标了 AI 生成；⑧「范围外」「要问清楚」缺末尾公示；⑨卡片缺「不判断能不能申报」；⑩第一次用页没公示模型",
         "改 5 条：①出处说明条画成「每条都有出处」（转 #349）；③查名单说明不写信贷、逾期、失信；⑤记录写明「查名单」；⑦系统提示不标 AI 生成；⑧范围外、要问清楚补末尾公示（转 #328）。"
         "不改 4 条：②「看原文」是在小程序里打开原文（main 现在就是，Grok 同轮也说没有外跳）；⑥页顶那条已写明只指问这份通知的回答（第 1 轮按 agy 改过）；"
         "⑨卡片是入口：「不判断能不能申报」在 AI 首页顶部和每条回答末尾都有，解读结果页在条款对照卡片里写「不判断能否申报」（#339 刚按你的意思去掉重复提示）；⑩之后每条回答末尾和 AI 首页都有公示。④是产品的事，留给你（不急）"),
        ("Grok · 10/23 全面走查（外跳、征信、导流 + 第一次用的人 6 个任务）",
         "外跳、导流：没有；6 个任务都顺利。1 条：查名单说明里的「信贷、逾期、失信」和「查这家企业出现过的记录」会被读成征信",
         "改了（和 agy 同一条）：只写在政府公开名单里按企业全称或信用代码核对、列出名单名称、发布机关和日期"),
        ("Codex · 10/24 对照 #328 开发设计和 #349 代码（gpt-6-sol）",
         "3 条：下一版 AI 首页还列着已撤稿的宁夏那份、还给「AI 解读」；第一次用只画了四句摘要，#328 要的是「问一问使用说明」五条全文；查名单查到以后看不到企业在名单原文里的那一行",
         "都改了：宁夏那份照 #347 不列；第一次用换成五条全文；查名单下面给出名单原文里的那一行（等站内闭环第 4 项）"),
        ("Grok · 10/24 设计说明和画面对不对得上",
         "12 条：2 条是 Codex 那轮已改的（撤稿那份）或材料里链接换了占位造成的；其余 10 条是说明写得不准：「不出现信用一类的字」和「统一社会信用代码」字面矛盾、引文和画面不是同一句；"
         "下一版 7 只画了解读结果页；「正文和附件里都没找到」括号挂错；推荐截止在卡片上是「截止未核对」；卡片下一步没画下一版；底栏「第 5 格」其实在中间；"
         "下一版 1 有两处没交代；第 2 轮的例子是宁夏那份；第七节「没有要你定的」和后面矛盾；agy 那行没按条对上",
         "说明都改了（画面不用改）"),
        ("Codex · 10/24 最后复查（gpt-6-sol）",
         "上一轮 3 处改得对；没有要离开小程序或复制网址的地方。1 条：「没做成」画的是「AI 暂时不可用」配「重试」，#328 规定这种不给重试",
         "改了：画能重试的「处理超时了」；#328 那行的例子转给 #328"),
        ("agy · 10/24 最后一轮合规（只看下一版）",
         "5 条：①AI 首页还列着撤稿的宁夏通知；②「2026-12 版以前的《隐私保护指引》」是未来的版本；③使用说明写「我的记录」、AI 首页写「我的 AI 记录」；④结果页顶部没写「不判断能不能申报」；⑤拦手机号的提示说「会发给 AI 模型」，在找通知、查名单里不对",
         "改 ⑤（照 #328 分两种说法）。不改：①列表第一行是宁夏另一份，原网页还在，撤稿那份已去掉；②问一问上线时指引升到 2026-12 版，这句随那一版上；③「我的记录」是线上页面的名字；④结果页是线上现有的样子，这句在条款对照卡片、AI 首页和每条回答末尾都有"),
        ("Grok · 10/24 最后一轮（外跳、征信、导流 + 第一次用的人 6 个任务 + 使用说明好不好懂）",
         "外跳、征信、导流：没有；6 个任务（含在小程序里核对名单原文那一行）都顺利。3 条：①使用说明第 5 条引的旧指引四句原话会被当成问一问的规则；②没找到写「这次不算次数」、同屏又写 20 句「找没找到都算」；③拦手机号的提示没写是哪一项",
         "③和 agy ⑤ 同一处，已改。①是 #328 待定稿的正式说明，原型照原文画，转给 #328 定稿时改。②不改：「次数」全站都指「今天还剩几次」，20 句那句自己写了分开算"),
        ("Grok · 10/24 第二遍核对设计说明和画面",
         "8 条：撤稿那份引错条号；第 7 条也是给 #349 的；开头没分清第 5、6 处；第 2 轮那行「已改」「还差」混在一句；下一版 1 只剩 4 份没交代；输入框下面不只写次数；agy ⑨ 的理由和画面不符；下一版 7 少写页底公示",
         "说明都改了（画面不用改）"),
    ],
}

SOURCES_NOTE = ("通知、名单、截止和原句都是 10/22 从线上只读接口取回的真实收录（借开发者工具在模拟器里调用，只取公开字段，存在生成脚本旁边的 "
                "data_1022.json）。解读和问答回答里摘哪几句、分到哪一类是示意，原句都逐字取自收录的正文；「我的 AI 记录」是示例，日期用 [ ] 占位；"
                "模型名称和备案号等网信办答复后填。")


def shot(sid, label, title, fn, intent, chips):
    tags = "".join(f'<span class="chip">{c}</span>' for c in chips)
    return (f'<figure class="shot" id="{sid}"><figcaption><div class="shot-no">{label}</div><h3>{title}</h3>'
            f'<p>{intent}</p><div class="chips">{tags}</div></figcaption>'
            f'<div class="frame-wrap"><div class="frame">{fn()}</div></div></figure>')


def sources(data):
    seen, items = set(), []

    def add(d, note=""):
        if d["url"] in seen:
            return
        seen.add(d["url"])
        items.append(f'<li><a href="{d["url"]}" target="_blank" rel="noopener">{d["issuer"]} · {d["title"]}（{d["date"]}）</a>{note}</li>')

    add(data["doc70955"], "：主例子（详情、解读结果、问这份通知；10/22 打开原网页核过）")
    for d in data["recent"]["items"]:
        add(d, "：10/22 下午原网站撤稿（收录里还在）" if d["id"] == data.get("withdrawn", {}).get("id") else "")
    for d in data["find"]["items"]:
        add(d)
    add(data["kind_example"]["items"][0], "：不能解读（资讯与指南）")
    for d in data["lookup"]["items"]:
        add(d, "：查名单例子")
    return "".join(items)


def page(now, nxt, data, walk=WALK):
    changes = "".join(f'<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>' for a, b, c in CHANGES)
    added = "".join(f'<li><b>{a}</b><span>{b}</span></li>' for a, b in ADDED)
    finds = "".join(f'<li><b>{a}</b><span>{b}</span><em>{c}</em></li>' for a, b, c in FINDINGS)
    walk_html = ""
    if walk:
        rows = "".join(f'<tr><td class="c-who">{a}</td><td>{b}</td><td>{c}</td></tr>' for a, b, c in walk["rows"])
        walk_html = (f'<section><h2>第一次用的人走一遍</h2><p class="sub">{walk["intro"]}</p>'
                     f'<div class="table-wrap"><table><thead><tr><th>谁</th><th>提的问题</th><th>怎么处理</th></tr></thead><tbody>{rows}</tbody></table></div></section>')
    now_shots = "".join(shot(*s) for s in now)
    next_shots = "".join(shot(*s) for s in nxt)
    return f"""<title>有据 AI 板块原型</title>
<meta name="claude:viewport-fit-sides" content="cover">
{FONTS_LINK.replace("&amp;", "&")}
<style>
/* 评审板：说明在上，手机画面按顺序排开；画面本身是小程序的浅色界面，不随主题变，外框和文字跟随主题 */
:root {{
  --bg: #ECEFF4; --surface: #FFFFFF; --ink: #1B2130; --muted: #566072; --faint: #6B7484; --line: #D8DDE6;
  --accent: #2A5CE0; --accent-tint: #E3EBFF; --frame: #FFFFFF; --warn: #B4540B; --shadow: 0 1px 2px rgba(20, 30, 60, 0.06), 0 12px 32px rgba(20, 30, 60, 0.10);
  --font: 'Noto Sans SC', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #0E1116; --surface: #161A21; --ink: #E6E9EF; --muted: #A2AABA; --faint: #8C95A5; --line: #2A303B;
  --accent: #82A5FF; --accent-tint: #1B2847; --frame: #2A303B; --warn: #F0A15E; --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 12px 32px rgba(0,0,0,0.35); color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #0E1116; --surface: #161A21; --ink: #E6E9EF; --muted: #A2AABA; --faint: #8C95A5; --line: #2A303B;
  --accent: #82A5FF; --accent-tint: #1B2847; --frame: #2A303B; --warn: #F0A15E; --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 12px 32px rgba(0,0,0,0.35); color-scheme: dark; }}
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
h2 small {{ margin-left: 10px; font-size: 14px; font-weight: 400; color: var(--faint); }}
.sub {{ margin: 6px 0 0; color: var(--muted); font-size: 14.5px; max-width: 52em; }}
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
.c-who {{ font-weight: 600; white-space: nowrap; }}
ul.list {{ margin: 16px 0 0; padding: 0; list-style: none; display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; }}
.list li {{ background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; display: flex; flex-direction: column; gap: 3px; font-size: 14px; min-width: 0; }}
.list li b {{ font-size: 15px; }}
.list li span {{ color: var(--muted); }}
.list li em {{ font-style: normal; font-size: 12.5px; color: var(--warn); }}
.sources {{ margin: 12px 0 0; padding-left: 1.2em; font-size: 13.5px; color: var(--muted); }}
.sources a {{ color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }}
.sources a:hover, .sources a:focus-visible {{ text-decoration: underline; }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }}
.foot {{ margin-top: 40px; font-size: 13px; color: var(--faint); overflow-wrap: anywhere; }}
@media (max-width: 860px) {{ h1 {{ font-size: 28px; }} }}
@media (max-width: 440px) {{ .screens {{ grid-template-columns: minmax(0, 1fr); }} }}
</style>
<div class="wrap">
<header>
<div class="eyebrow">有据 · 原型 · 2026-10-22</div>
<h1>AI 板块原型</h1>
<p class="lede">按你 10/19 定的 D-075、D-078 重画。上面一组是你手上体验版现在的样子，下面一组是「问一问」第一期做完以后的样子。10/17 的对话版设计稿（#278）改成历史稿。</p>
<div class="pills"><span class="pill"><b>只改原型和文档</b>，没有改代码、没有动线上</span>
<span class="pill">通知和名单是 10/22 线上真实收录的</span><span class="pill">[ ] 是还没有的内容：模型名称、备案号、记录日期</span>
<span class="pill">当前版本的样子在开发者工具里对过</span></div>
</header>

<section>
<h2>和 10/17 那版比，改了什么</h2>
<p class="sub">10/17 的对话版是方向稿；10/19 你定了 D-075、D-078，有几处做法改了，原型一直没跟着改，所以看着和手上的版本不像一个样子。</p>
<div class="table-wrap"><table><thead><tr><th>10/17 原型</th><th>现在定的（10/19）</th><th>你手上的版本</th></tr></thead><tbody>{changes}</tbody></table></div>
</section>

<section>
<h2>当前版本<small>体验版，只对你一个人开着</small></h2>
<p class="sub">照小程序现在的代码画，样子在开发者工具里一页页对过；页脚和详情页两处说明照 10/22 晚上合并的 #339 改过（出处说明只在顶部写一次）。10/23 审核退回的就是查名单 →「复制原文链接」→ 去浏览器这条路；这一组照线上原样画，复制链接还在，下一版都改成在小程序里看。</p>
<div class="screens">{now_shots}</div>
</section>

<section>
<h2>下一版<small>问一问第一期（D-078）</small></h2>
<p class="sub">界面文字照开发设计 #328 第十二节。问一问第一期只摘原句、不写结论，没有相机；图片是第二期，联网是第三期。AI 首页三行能点是推进计划第 0 步第 1 处。10/23 起查询、正文、附件、结果都在小程序里完成：列表行「查看详情」、详情「站内阅读」、「导出」改叫「结果」照 #349，正文去掉网页上的字照 #345，原文链接失效的通知不再给 AI 解读照 #347。</p>
<div class="screens">{next_shots}</div>
<ul class="list">{added}</ul>
</section>

<section>
<h2>画的时候发现的</h2>
<ul class="list">{finds}</ul>
</section>

{walk_html}

<section>
<h2>画面里用到的真实数据</h2>
<p class="sub">{SOURCES_NOTE}</p>
<ul class="sources">{sources(data)}</ul>
</section>
<p class="foot">说明文档：docs/设计/AI板块原型_当前版本和下一版_2026-10-22.md · 生成脚本：docs/设计/AI板块原型/生成脚本/ai_board.py · 效果图：docs/设计/AI板块原型/效果图/</p>
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
