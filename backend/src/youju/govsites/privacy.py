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
_NOT_NAME_END = tuple(
    "型油类业品化性气水电料费税率险款价区县市镇村省局厅委办部站所院校园河湖江苑条"
)


def _named(title: str) -> bool:
    from youju.crawler.detail import _name_like

    for m in _NAME_CONTEXT.finditer(title):
        if any(g and _name_like(g) for g in m.groups()):
            return True
    for m in _SEQUENCE.finditer(title):
        items = m.group(0).split("、")
        names = [w for w in items if _name_like(w) and not w.endswith(_NOT_NAME_END)]
        if len(names) >= 2:
            return True
    return False


def public_title(title: str) -> str:
    """写进公开证据的样例标题：个人事项或写着人名的换成 MASK，其余照原样。"""
    from youju.api.v1 import personal_matter

    if personal_matter(title) or any(w in title for w in _EXTRA_WORDS) or _named(title):
        return MASK
    return title
