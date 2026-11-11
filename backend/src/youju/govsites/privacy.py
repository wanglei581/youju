"""全国覆盖公开文件里不写个人姓名：栏目清单的"备注"样例、抽查证据的"最新1–3"。

这些文件放在公开仓库里，样例标题只用来证明一个栏目读到了什么，不需要原标题。是个人事项的
（D-097 ② 的个人事项词：任免、任命、记功、招聘、工伤……，再加劳动能力鉴定、公告送达、宅基地、
见义勇为），或者标题里写着人名的（"××同志""××等 3 名""××、××等 15 户"），整条换成 MASK。
11/9 地市一组和第 1 批核对发现：运城人社的劳动能力鉴定送达公告、宅基地许可公布、任免通知的
姓名都写进了公开的栏目清单和证据表。
"""

import re

MASK = "（个人事项，标题略）"
_EXTRA_WORDS = ("劳动能力", "送达", "宅基地", "见义勇为")
_H = "[一-鿿]"
# 人名的几种写法：××同志、××等 3 名/户、××等同志、××等职务、××、××同志、任命××为
_NAME_CONTEXT = re.compile(
    rf"({_H}{{2,3}})(?=同志|先生|女士)"
    rf"|({_H}{{2,3}})(?=等(?:[\d一二三四五六七八九十两百]+[名人位户]|同志|职务))"
    rf"|({_H}{{2,3}})(?=、{_H}{{2,3}}(?:同志|等))"
    rf"|(?<=任命|聘任|聘用|授予|推荐)({_H}{{2,3}})(?=为|同志|职务)"
)


def public_title(title: str) -> str:
    """写进公开文件的样例标题：个人事项或写着人名的换成 MASK，其余照原样。"""
    from youju.api.v1 import personal_matter
    from youju.crawler.detail import _name_like

    if personal_matter(title) or any(w in title for w in _EXTRA_WORDS):
        return MASK
    for m in _NAME_CONTEXT.finditer(title):
        if any(g and _name_like(g) for g in m.groups()):
            return MASK
    return title
