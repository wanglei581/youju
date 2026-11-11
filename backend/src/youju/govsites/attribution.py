"""归属判断：导航里认出的门户、部门网站是不是这个行政区自己的（不是上级、本省别处、外省的）。

11/8 全国清单里查出：区县门户上只写"财政厅""发展和改革委员会""科技局"的链接常指向市直、省直部门
网站，被当成了区县自己的部门（约 950 格）；门户也有认错的（天津河北区认成河北省政府网站、南通的
区县认成南通市门户）。这里按三样东西判断：
1. 名称：链接文字、网站自己写的网站名（meta SiteName、标题）以谁的地名开头，开头的层级字
   （"省""市"），"厅"（省级部门才叫厅）；
2. 网站标识码：前 6 位是行政区划代码（国办统一分配）；
3. 网址：和上级已认定的部门网站同一个网址，或在上级门户的域名下、不在本地门户的域名下。
都判不出的照常认（不猜），宁可留给人工核对，也不把本地部门错排出去。
"""

import re
from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import urlsplit

from youju.govsites.divisions import Division, DivisionIndex, name_variants
from youju.govsites.links import registrable

_LEVEL_CHARS = "省市州盟县区旗"


def level_prefix(text: str) -> str | None:
    """部门简称开头表示层级的字（"省科技厅"的"省"）；"市场监管局""市监局"的"市"不算。"""
    if (
        len(text) > 2
        and text[0] in _LEVEL_CHARS
        and not text.startswith(("市场", "市监", "区块", "州际"))
    ):
        return text[0]
    return None


def level_chars(div: Division) -> str:
    """本级部门简称开头的字："省科技厅""市科技局""县科技局""区科技局"；县级市自称"市"。"""
    if div.level == "国家级":
        return "国"
    if div.level == "省级":
        return "省区" if div.kind == "自治区" else ("市" if div.kind == "直辖市" else "省")
    if div.level == "市级":
        return {"自治州": "州", "盟": "盟", "地区": "区"}.get(div.kind, "市")
    last = div.name[-1]
    return {"市": "市", "县": "县", "区": "区", "旗": "旗"}.get(last, last)


def own_branch(text: str, div: Division) -> bool:
    """区县里上级部门的派出机构算本区县的部门：如"杭州市生态环境局西湖分局"
    （生态环境在区县一级多是市局的分局）。

    本地名称后面要紧跟"分局""区分局""县分局"这类结尾："岳阳楼分局"不是岳阳县的，
    "井陉矿分局"不是井陉县的。"""
    if div.level != "区县级":
        return False
    shorts = sorted((v for v in name_variants(div.name, div.kind) if len(v) >= 2), key=len)
    for v in shorts:
        i = text.find(v, 2)
        while i >= 0:
            if re.fullmatch(r"(?:区|县|市|旗)?(?:分局|监管局|管理所)", text[i + len(v) :]):
                return True
            i = text.find(v, i + 1)
    return False


class Verdict(NamedTuple):
    kind: str  # 本级 / 上级 / 别处 / 外省 / 不确定
    reason: str = ""
    basis: str = ""  # 名称 / 标识码 / 域名


UNSURE = Verdict("不确定")
NOT_OWN = ("上级", "别处", "外省")


@dataclass(frozen=True)
class Places:
    """一个行政区的名称上下文。写法都是长的在前、至少两个字。"""

    div: Division
    own: tuple[str, ...]  # 本地写法；市、县级不含省简称（"湖北商务厅"不能在应城市加分）
    ancestors: tuple[str, ...]  # 上级（所在地市、省）的写法
    ancestor_codes: frozenset[str]
    others: tuple[str, ...]  # 本省其他行政区（不含本地、上级）
    foreign: tuple[str, ...]  # 外省
    own_chars: str


def _longest_first(names: set[str]) -> tuple[str, ...]:
    return tuple(sorted({n for n in names if len(n) >= 2}, key=lambda s: (-len(s), s)))


def places_for(index: DivisionIndex, div: Division) -> Places:
    if div.level == "国家级":
        provinces = {v for p in index.provinces() for v in name_variants(p.name, p.kind)}
        return Places(
            div,
            _longest_first(name_variants(div.name, div.kind) | {div.province}),
            (),
            frozenset(),
            _longest_first(provinces),
            (),
            level_chars(div),
        )
    prov = index.province_of(div)
    own = set(name_variants(div.name, div.kind))
    chain: list[Division] = []
    code = div.parent_code
    while code and code in index.by_code:
        chain.append(index.by_code[code])
        code = index.by_code[code].parent_code
    anc: set[str] = set()
    for a in chain:
        anc |= name_variants(a.name, a.kind)
    # 和本地写法一模一样的上级写法（吉林省吉林市的"吉林"）不算上级：名称判断时本地优先
    anc -= own
    others: set[str] = set()
    for d in index.descendants(prov.code):
        if d.code != div.code:
            others |= name_variants(d.name, d.kind)
    others -= own | anc
    foreign = {
        v for p in index.provinces() if p.code != prov.code for v in name_variants(p.name, p.kind)
    }
    return Places(
        div,
        _longest_first(own),
        _longest_first(anc),
        frozenset(a.code for a in chain),
        _longest_first(others),
        _longest_first(foreign),
        level_chars(div),
    )


def branch_of_own(text: str, places: Places) -> bool:
    """链接文字说明是本区县的派出分局："杭州市生态环境局西湖分局"，或"西夏区生态环境分局"这样
    以本地名称打头、以"分局"结尾的。"""
    t = clean_name(text)
    if own_branch(t, places.div):
        return True
    if not t.endswith("分局"):
        return False
    # 去掉打头的上级地名再看（"银川市西夏区生态环境分局"）；打头的最长地名要是本地的：
    # "承德市生态环境局双桥分局"去掉"承德市"后不是承德县，不算
    for _ in range(3):
        hit = _best_prefix(t, places)
        if hit is None:
            return False
        if hit[0] == "本级":
            return True
        if hit[0] != "上级":
            return False
        t = t[len(hit[1]) :]
    return False


_LEAD = re.compile(r"^[\s>》›»·•|｜\-—–:：、]+")
_CN_PREFIX = re.compile(r"^中国[（(]([^）)]{2,10})[）)]")
_GRADE_PREFIX = re.compile(r"^(?:国家级|省级)")
_BRACKETS = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_BRANCH_END = re.compile(r"(?:分局|监管局|管理所)$")


def clean_name(text: str) -> str:
    t = _LEAD.sub("", re.sub(r"\s+", "", text or ""))
    t = _CN_PREFIX.sub(r"\1", t)  # "中国（北京）自由贸易试验区"按"北京自由贸易试验区"算
    t = _GRADE_PREFIX.sub("", t)  # "国家级九江经济技术开发区"
    # 括号注释去掉；括号里写的是派出分局（"市生态环境局（西湖分局）"）的保留文字
    return _BRACKETS.sub(
        lambda m: m.group(0)[1:-1] if _BRANCH_END.search(m.group(0)[1:-1]) else "", t
    )


def _best_prefix(t: str, places: Places) -> tuple[str, str] | None:
    """最长的地名前缀和它的类别；一样长时本地 > 上级 > 别处 > 外省。"""
    best: tuple[int, int, str, str] | None = None
    for rank, (cat, names) in enumerate(
        (
            ("本级", places.own),
            ("上级", places.ancestors),
            ("别处", places.others),
            ("外省", places.foreign),
        )
    ):
        for n in names:  # 长的在前：第一个命中的就是这一类里最长的
            if t.startswith(n):
                key = (-len(n), rank, cat, n)
                if best is None or key < best:
                    best = key
                break
    return (best[2], best[3]) if best else None


def name_verdict(text: str, places: Places) -> Verdict:
    """按名称（链接文字或网站名）判断是谁的部门、谁的网站。"""
    t = clean_name(text)
    if not t:
        return UNSURE
    div = places.div
    if branch_of_own(t, places):
        return Verdict("本级", "本地的派出分局", "名称")
    stripped = ""
    for _ in range(3):
        hit = _best_prefix(t, places)
        if hit is None:
            break
        cat, name = hit
        if cat == "本级":
            rest = t[len(name) :]
            provincial = rest.endswith("厅") and not rest.endswith(("办公厅", "大厅"))
            if name in places.foreign and div.level != "省级" and provincial:
                # 本地简称和外省同名（天津河北区的"河北"），后面又是省级才叫的"厅"：
                # "河北工业和信息化厅"是河北省的
                return Verdict("外省", f"名称以外省「{name}」开头、以「厅」结尾", "名称")
            if div.level in ("市级", "区县级") and provincial:
                # 市县没有"厅"：吉林市门户上的"吉林科技厅"是吉林省的
                return Verdict("上级", "「厅」是省级部门的叫法", "名称")
            return Verdict("本级", f"名称以本地「{name}」开头", "名称")
        if cat == "上级":
            # "四川省康定市财政局"：去掉上级地名再看后面是不是本地
            stripped = stripped or name
            t = t[len(name) :]
            if len(t) < 2:
                break
            continue
        if cat == "别处":
            return Verdict("别处", f"名称以本省别处「{name}」开头", "名称")
        return Verdict("外省", f"名称以外省「{name}」开头", "名称")
    if stripped:
        return Verdict("上级", f"名称以上级「{stripped}」开头", "名称")
    lower = "上级" if div.level in ("市级", "区县级") else "别处"
    if t.startswith("自治区") and div.level in ("市级", "区县级"):
        return Verdict("上级", "名称开头的「自治区」不是本级的叫法", "名称")
    prefix = level_prefix(t)
    if prefix:
        if prefix in places.own_chars:
            return Verdict("本级", f"名称开头的「{prefix}」是本级的叫法", "名称")
        return Verdict(lower, f"名称开头的「{prefix}」不是本级的叫法", "名称")
    if div.level in ("市级", "区县级") and t.endswith("厅") and not t.endswith(("办公厅", "大厅")):
        return Verdict("上级", "「厅」是省级部门的叫法", "名称")
    return UNSURE


def _host(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def _code_verdict(site_code: str, places: Places, index: DivisionIndex) -> Verdict:
    """网站标识码（10 位，前 6 位是行政区划代码）说明网站是谁的。

    不是 10 位、或前 6 位不在区划库里的（旧代码、乱写的）不用。"""
    if not re.fullmatch(r"\d{10}", site_code or ""):
        return UNSURE
    code6 = site_code[:6]
    target = index.by_code.get(code6)
    if target is None:
        return UNSURE
    div = places.div
    if code6 == div.code:
        return Verdict("本级", f"网站标识码 {site_code} 是本地的", "标识码")
    if code6 in places.ancestor_codes:
        return Verdict("上级", f"网站标识码 {site_code} 是{target.name}的", "标识码")
    if div.level != "国家级" and code6[:2] == div.code[:2]:
        return Verdict("别处", f"网站标识码 {site_code} 是{target.name}的", "标识码")
    return Verdict("外省", f"网站标识码 {site_code} 是{target.name}的", "标识码")


def site_verdict(
    names: list[str],
    site_code: str,
    urls: list[str],
    places: Places,
    index: DivisionIndex,
    *,
    own_portal: str,
    own_portal_code: str = "",
    upper_portals: list[str] | tuple[str, ...] = (),
) -> Verdict:
    """部门网站抓到后判断它是谁的：本地门户里的频道放行（看最终网址：本门户链接跳到别的网站的
    不算频道）；别的先看网站自己写的名字（SiteName、标题里的几段），说不清时再看标识码、域名。"""
    own_host = _host(own_portal)
    final = next((u for u in reversed(urls) if u), "")
    if own_host and _host(final) == own_host:
        # 本地门户里的部门频道：网页带的是门户全站的网站名、标识码（集安市门户的 SiteName 写着
        # "通化市人民政府"，广东集约化门户首页带省里的标识码），按它们判会把本地部门全排掉
        return UNSURE
    for name in names:
        v = name_verdict(name, places)
        if v.kind != "不确定":
            return v
    if site_code and site_code != own_portal_code:
        v = _code_verdict(site_code, places, index)
        if v.kind != "不确定":
            return v
    own_reg = registrable(own_portal) if own_portal else ""
    upper_regs = {registrable(u) for u in upper_portals if u}
    for u in urls:
        reg = registrable(u) if u else ""
        if reg and reg != own_reg and reg in upper_regs:
            return Verdict("上级", f"网站在上级门户的域名 {reg} 下", "域名")
    return UNSURE


_TITLE_SPLIT = re.compile(r"\s*[-_|｜—–·]+\s*")


def title_names(title: str) -> list[str]:
    """标题按"-""|"等分段（"首页 - 呼和浩特市发展和改革委员会"），每段都可能是网站名。"""
    return [s for s in _TITLE_SPLIT.split((title or "").strip()) if 2 <= len(s) <= 30]


def portal_verdict(
    names: list[str],
    site_code: str,
    url: str,
    places: Places,
    index: DivisionIndex,
    upper_hosts: set[str] | frozenset[str],
) -> Verdict:
    """下级门户抓到后，确认没有认成上级门户或外省网站（只认两种明确的错，别的照常认）。"""
    if _host(url) in upper_hosts:
        return Verdict("上级", "链到的是上级门户", "域名")
    code = _code_verdict(site_code, places, index)
    if code.kind == "外省":
        for name in names:
            v = name_verdict(name, places)
            if v.kind == "外省":
                return Verdict("外省", f"链到的是外省网站（{v.reason}，{code.reason}）", "标识码")
            if v.kind == "本级":
                break
    return UNSURE
