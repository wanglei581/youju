"""全国覆盖公开文件里不写个人姓名。

这些文件放在公开仓库里：
- 栏目清单、栏目抽样试读的"备注"不写样例标题（report.column_rows）：样例只用来证明栏目读到了
  东西，"页面日期数""最新日期样例"两列已经够；导入来源时"备注"还会写进来源备注。靠猜认人名
  总有漏网的写法（11/11 独立复核在区县行里又找出 26 个），不写最稳。
- 抽查证据的"最新1–3"要给复核的人看标题，所以照写，但个人事项或写着人名的整条换成 MASK
  （public_title）。宁可多挡：只是证据里的样例，挡了还有日期和网址。
"""

import re

MASK = "（标题略）"
# D-097 ② 的个人事项词以外，再挡这些：劳动能力鉴定、公告送达、宅基地、见义勇为、证件遗失作废、
# 辞去职务（11/9–11/11 在公开文件里见过写着姓名的写法）。"征地补偿"不当词：征地补偿标准、办法
# 是普通政策；写着人名的征地补偿决定靠下面的人名写法认
_EXTRA_WORDS = (
    "劳动能力",
    "送达",
    "宅基地",
    "见义勇为",
    "遗失声明",
    "遗失公告",
    "作废公告",
    "作废声明",
    "辞去",
)
# 工伤、病残一类的个人事项文书（"关于××停工留薪期确认书的公告""关于对参保职工××病残津贴领取资格的
# 公示""××伤残等级鉴定结论"，11/12 地市一组报）：标题常直接写人名，有的姓氏不常见、有的以"路""林"
# 这类字结尾、有的连写几个人，靠猜人名会漏。同样的词也出现在待遇标准、管理办法这类普通政策里，不当
# 词挡：有这些词、又是公示、公告、确认书、鉴定结论这类文书的，整条挡（不看有没有人名）
_INJURY_WORDS = ("停工留薪", "病残", "工亡", "伤残", "供养亲属")
_INJURY_PAPERS = (
    *("公示", "公告", "确认书", "决定书", "决定", "通知书", "告知书", "鉴定书", "认定书", "结论"),
    *("名单", "批复", "答复"),
)
_H = "[一-鿿]"
# 人名的几种写法：××同志、××等 3 名/户、××等同志、××等职务、××、××同志、任命××为、
# 关于对××的、开头就是人名的"××（、××）批前公示"（只认开头：项目的"规划许可批前公示"不挡）、
# 关于××代表《…》的答复、标题末尾" - ××"的署名
_NAME_CONTEXT = re.compile(
    rf"({_H}{{2,3}})(?=同志|先生|女士)"
    rf"|({_H}{{2,3}})(?=等(?:[\d一二三四五六七八九十两百]+[名人位户]|同志|职务))"
    rf"|({_H}{{2,3}})(?=、{_H}{{2,3}}(?:同志|等))"
    rf"|(?<=任命|聘任|聘用|授予|推荐)({_H}{{2,3}})(?=为|同志|职务)"
    rf"|^[\s·•>》\-—]*(?:{_H}{{2,3}}、)*({_H}{{2,3}})(?=批前公示)"
    rf"|(?<=关于对)({_H}{{2,3}})(?=的|、|等)"
    rf"|(?<=关于)({_H}{{2,3}})(?=代表)"
    rf"|(?<=\s[-—－–]\s)({_H}{{2,3}})\s*$"
)
# 顿号连着三个以上的短词里有两个以上像人名的（"关于对××、××、××的征地补偿安置决定"）；
# 以这些字结尾的是品类、地名、机构，不算人名（"成长型、龙头型、创新型""车用汽、柴油"
# "秦淮河、石臼湖""羊毛、毛条"）
_SEQUENCE = re.compile(rf"{_H}{{2,3}}(?:、{_H}{{2,3}}){{2,}}")
# 写着人名的补偿安置决定（"关于××的征地补偿安置决定""关于××、××房屋征收补偿决定"）：征地补偿
# 标准、办法是普通政策，不当词挡，只认"关于 + 人名 + …补偿/安置… + 决定/协议"
_COMPENSATION = re.compile(
    rf"关于((?:{_H}{{2,3}}、)*{_H}{{2,3}})[^，。]{{0,12}}(?:补偿|安置)[^，。]{{0,6}}(?:决定|协议)"
)
_NOT_NAMES = ("房屋", "房产", "住房", "厂房")  # 以这些词开头的不是人名（"房屋征收补偿决定"）
# 括号里只有像人名的词（"作废公告（张三、李四）""送达公告（王五）""名单的公示（赵六、钱七）"）。
# 政策标题括号里常见、又以常见姓氏的字开头的词不算（11/12 拿 1.2 万个缓存页面的链接文字核对过：
# "（成交）""（修订版）""（文字版）""（盖章）"……）；括号里的地名（"（浦口）"）照样挡，挡了还有
# 日期和网址
_PARENS = re.compile(rf"[（(]((?:{_H}{{2,3}}、)*{_H}{{2,3}})[）)]")
_PAREN_WORDS = {
    *("修订", "修正", "修改", "修编", "解读", "简体", "繁体", "范本", "公示", "公告", "通告"),
    *("图解", "文字", "简版", "简易", "简介", "成交", "盖章", "高级", "施工", "康复", "车辆"),
    *("房车", "商户", "商贸", "华东", "游泳", "黄色"),
}
_PAREN_ENDS = tuple("版稿案制段片篇网式")  # "（修订稿）""（安徽段）""（荆楚网）""（封闭式）"
_NOT_NAME_END = tuple(
    "型油类业品化性气水电料费税率险款价区县市镇村省局厅委办部站所院校园河湖江苑条"
)

# 新闻标题里的人名（11/12 #499 复核时在证据表里见过，上面几种写法都认不出）。
# ① 职务加人名："局长某某现场接听""党组书记、局长某某接听""代区长某某接待来访群众"
#   "省政协副主席某某来滁调研""某某市长致全市人民的公开信"。职务词前后紧跟像人名的 2–3 个字就挡，
#   不看后文（"接待""来""致"……写法太多）。"委员""代表""常委"不算职务词（"常务委员会"
#   "人大代表建议"太常见）
_ROLE = re.compile(
    "|".join(
        sorted(
            (
                *("书记", "省长", "市长", "县长", "区长", "州长", "旗长", "盟长", "镇长", "乡长"),
                *("局长", "主任", "主席", "部长", "厅长", "处长", "科长", "院长", "校长", "董事长"),
                *("总经理", "秘书长", "行长", "社长", "所长", "队长", "站长", "署长", "司长"),
                *("关长", "会长", "理事长", "检察长", "庭长", "政委", "总理", "委员长"),
            ),
            key=len,
            reverse=True,
        )
    )
)
# 职务前后紧挨着的词、新闻动词前面的词，按"宁可多挡"认人名（_name_ish）：常见姓氏开头、不是常用词、
# 不以地名的字（市县区……）结尾就算（"沙市""安区"不算；三个字的前两个字也一样："沙市财""安区人"），
# 名字里有"会""江""科"这类字的照样算（"局长王会""局长王江""王科出席"）。不看姓氏的（开头或标点后跟
# 出席、主持这类新闻动词的）不是常见姓氏开头时，以机构的字结尾的也不算（"市委主持""市政府与"）。
# 不为"长江""长沙"一类地名开例外（"局长江某某"也是人名）：地名连着"长"字的标题（"宜宾市长江流域"
# "城区长沙湾"）会多挡，挡了还有日期和网址。职务后面接"单位""质量"的不是人名（"副理事长单位"
# "市长质量奖"）。人名在职务前面的，要在标题开头或标点后面（"第三季度局长接待日"不挡）
_OPEN = '、，,：:；;（(“"《—- 　'
_PLACE_ENDS = tuple("市县区省州镇乡村旗盟")
_NOT_ROLE_NAMES = ("单位", "质量")
_ORG_ENDS = tuple("委府办局厅院部司处室署会团社校所站队组科")
# 这些字不会出现在人名里，含有的不是人名（"我市……""各地……""关于我市长期……"的"关于我"）；
# 下面这些词，候选正好就是这个词才不算（"关于宣布……""全市出席……""本次……"），三个字的不按
# 开头否决（"全力某出席"照样挡，宁可多挡）
_NOT_NAME_CHARS = frozenset("我各该此据在与把")
_NOT_NAME_WORDS = frozenset(
    (
        *("关于", "对于", "国家", "中央", "全市", "全省", "全国", "全县", "全区", "全州", "全镇"),
        *("全乡", "全村", "全体", "全面", "全力", "全年", "全民", "全球", "全部", "全新", "全域"),
        *("全程", "全会", "全员", "本市", "本省", "本县", "本区", "本局", "本次", "本年", "本期"),
        *("为了", "从严", "从快", "从业", "将于", "经济", "经营"),
    )
)
# ② 领导人、官员的新闻："某某出席……""某某主持召开……""某某签署……""某某同某国总统会谈"
#   "某某到某县督导调研""某某总书记重要讲话"。"像人名"按常见姓氏认，不常见的姓认不出：开头或标点
#   后面跟出席、主持这类只有人才做的新闻动词的，不看姓氏；"宣布""签署"（"正式宣布""双方签署"）、
#   总书记前面（"牢记总书记嘱托"）的要看姓氏，现任中央领导另有名单
_OPEN_RE = f"(?:^|[{re.escape(_OPEN)}])"
_TOP = re.compile(rf"({_H}{{2,3}})(?=总书记|总理|委员长)")
_NOT_TOP_END = tuple("着的了和与及在把将对向")  # "循着总书记足迹"
_LEAD = re.compile(
    rf"{_OPEN_RE}({_H}{{2,3}})(?=出席|主持|会见|会谈|致信|致电|回信|看望|慰问|视察|寄语)"
)
_LEAD_WEAK = re.compile(rf"{_OPEN_RE}({_H}{{2,3}})(?=宣布|签署)")
_PAIR = re.compile(rf"{_OPEN_RE}({_H}{{2,3}})(?=同|与)")
_PAIR_WORDS = ("会谈", "通电话", "会见", "互致")
_TRIP = re.compile(rf"{_OPEN_RE}({_H}{{2,3}})(?=到|赴|带队|率队|率团)")
_TRIP_WORDS = ("调研", "考察", "督导", "督查", "检查", "走访", "慰问", "看望", "暗访")
# 中央领导的姓名：转载的中央新闻、"某某思想"的学习材料里最常见，写法太多（"……作出重要指示"
# "……：在……座谈会上的讲话""……法治思想"），单列；换届时更新
_LEADERS = ("习近平", "李强", "赵乐际", "王沪宁", "蔡奇", "丁薛祥", "李希", "韩正", "李克强")


def _named(title: str) -> bool:
    from youju.crawler.detail import _name_like

    for m in _NAME_CONTEXT.finditer(title):
        if any(g and _name_like(g) for g in m.groups()):
            return True
    for m in _PARENS.finditer(title):
        parts = m.group(1).split("、")
        if all(
            _name_like(w) and w not in _PAREN_WORDS and not w.endswith(_NOT_NAME_END + _PAREN_ENDS)
            for w in parts
        ):
            return True
    for m in _COMPENSATION.finditer(title):
        if any(_name_like(w) and not w.startswith(_NOT_NAMES) for w in m.group(1).split("、")):
            return True
    for m in _SEQUENCE.finditer(title):
        items = m.group(0).split("、")
        names = [w for w in items if _name_like(w) and not w.endswith(_NOT_NAME_END)]
        if len(names) >= 2:
            return True
    return False


def _name_ish(word: str, *, any_surname: bool = False) -> bool:
    """职务旁、新闻动词前的词像不像人名（宁可多挡）：常见姓氏开头、不是常用词或地名、不以地名的字结尾、
    不以"关于""我"这类开头。any_surname：不看姓氏（不常见的姓认不出）。"""
    from youju.crawler import detail

    return (
        2 <= len(word) <= 3
        and re.fullmatch(f"{_H}+", word) is not None
        and (
            word[0] in detail._SURNAMES
            or word.startswith(detail._COMPOUND_SURNAMES)
            or (
                any_surname
                and not word.endswith(_ORG_ENDS)
                and not (len(word) == 3 and word[:2].endswith(_ORG_ENDS))
            )
        )
        and word not in detail._COMMON_WORDS
        and word not in detail._TITLES
        and word not in detail._PLACE_NAMES
        and not word.startswith(_NOT_ROLE_NAMES)
        and not word.endswith(_PLACE_ENDS)
        and not (len(word) == 3 and word[:2].endswith(_PLACE_ENDS))
        and not set(word) & _NOT_NAME_CHARS
        and word not in _NOT_NAME_WORDS
    )


def _news_named(title: str) -> bool:
    """新闻标题里写着领导人、官员姓名的（职务加人名、某某出席……、某某到某县调研……）。"""
    if any(name in title for name in _LEADERS):
        return True
    for m in _ROLE.finditer(title):
        rest = title[m.end() :]
        if any(_name_ish(rest[:k]) for k in (2, 3)):
            return True
        i = m.start()
        for k in (2, 3):
            j = i - k
            if j >= 0 and (j == 0 or title[j - 1] in _OPEN) and _name_ish(title[j:i]):
                return True
    for m in _TOP.finditer(title):
        if not m.group(1).endswith(_NOT_TOP_END) and _name_ish(m.group(1)):
            return True
    if any(_name_ish(m.group(1), any_surname=True) for m in _LEAD.finditer(title)):
        return True
    if any(_name_ish(m.group(1)) for m in _LEAD_WEAK.finditer(title)):
        return True
    for m in _PAIR.finditer(title):
        rest = title[m.end() :]
        if _name_ish(m.group(1), any_surname=True) and any(w in rest for w in _PAIR_WORDS):
            return True
    for m in _TRIP.finditer(title):
        if _name_ish(m.group(1)) and any(w in title[m.end() :] for w in _TRIP_WORDS):
            return True
    return False


def public_title(title: str) -> str:
    """写进公开证据的样例标题：个人事项或写着人名的换成 MASK，其余照原样。"""
    from youju.api.v1 import personal_matter

    injury = any(w in title for w in _INJURY_WORDS) and any(w in title for w in _INJURY_PAPERS)
    if (
        personal_matter(title)
        or any(w in title for w in _EXTRA_WORDS)
        or injury
        or _named(title)
        or _news_named(title)
    ):
        return MASK
    return title
