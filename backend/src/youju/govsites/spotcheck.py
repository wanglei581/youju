"""覆盖抽查：从覆盖看板"已收录"的格里按固定种子抽 N 格，每格取一份文件，打开原网页核对
链接、标题、发布日期和归属（本市、本部门），留下证据给人逐条看过后签字。

D-126：每省抽查由 Claude 带证据做，创始人复看约 5%；覆盖率按抽查合格的算。

只读公开页面：打开原文走 PoliteFetcher（遵守 robots、同一网站两次请求间隔 3 秒、透明标识，
不绕过任何防护），结果存页面缓存，重跑不重复请求。

用法（backend 目录，数据库读 YOUJU_DATABASE_URL，也就是试抓用的本机库）：
  uv run python -m youju.govsites.spotcheck --board <试点覆盖/省.csv> --out <目录>
"""

import argparse
import csv
import hashlib
import html as htmllib
import re
import unicodedata
from datetime import date
from pathlib import Path

from youju.govsites.divisions import name_variants
from youju.govsites.links import is_gov_url, parse_html, registrable
from youju.govsites.names import dept_categories
from youju.govsites.platform import fingerprint
from youju.govsites.privacy import public_title

DEFAULT_SEED = "2026-11"
OK = "合格"
REVIEW = "待人工看"
FIELDS = (
    "序号",
    "城市",
    "部门",
    "部门全称",
    "栏目网址",
    "文件标题",
    "文件网址",
    "发布日期",
    "来源",
    "打开",
    "标题核对",
    "标题说明",
    "日期核对",
    "日期说明",
    "归属核对",
    "归属说明",
    "程序结论",
    "人工结论",
    "备注",
)


def pick_cells(
    rows: list[dict], level: str = "市级", n: int = 20, seed: str = DEFAULT_SEED
) -> list[dict]:
    """只取这一级"已收录"的格，按种子散列排序取前 n 个：重跑抽到的一样，换种子就换一批。"""
    pool = [r for r in rows if r.get("层级") == level and r.get("状态") == "已收录"]

    def key(r: dict) -> str:
        raw = f"{seed}|{r.get('区划代码', '')}|{r.get('部门', '')}|{r.get('栏目网址', '')}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    return sorted(pool, key=key)[:n]


_TRUNCATED = re.compile(r"(?:\.{3,}|…+|．{3,}|。{3,})\s*$")


def norm_title(text: str) -> str:
    """比标题用：去掉列表页截断的省略号、空白和全半角标点符号，全角字母数字转半角。"""
    t = _TRUNCATED.sub("", (text or "").strip())
    t = unicodedata.normalize("NFKC", t)
    return "".join(ch for ch in t if unicodedata.category(ch)[0] not in "PSZC")


_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def page_text(page_html: str) -> str:
    """网页的文字（去掉注释、脚本、样式和标签，实体转成字符）。注释里常有模板留下的旧日期（潍坊）。"""
    # 只去掉短注释：没闭合好的长注释会把后面的正文一起吞掉（淮安）
    t = re.sub(r"<!--(?:(?!-->).){0,300}?-->", " ", page_html or "", flags=re.S)
    t = _SCRIPT_STYLE.sub(" ", t)
    t = _TAG.sub(" ", t)
    return htmllib.unescape(t)


def _page_title(page_html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", page_html or "", re.S | re.I)
    return htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def _longest_run(needle: str, hay: str) -> int:
    """needle 里在 hay 中原样出现的最长一段有多长（标题一般几十字，逐段试够快）。"""
    for k in range(len(needle), 0, -1):
        for i in range(len(needle) - k + 1):
            if needle[i : i + k] in hay:
                return k
    return 0


# 库里标题混进了列表上的其他文字（漯河、本溪一类平台的悬浮提示："标题：…点击数：…发表时间：…"）
_POLLUTED = re.compile(
    r"^(?:标题|主题)[:：]|点击数[:：]|发表时间[:：]|浏览次数[:：]|阅读次数[:：]|\s日期[:：]\s*\d"
)


def title_check(stored: str, page_html: str) -> tuple[str, str]:
    t = norm_title(stored)
    if not t:
        return "不一致", "库里没有标题"
    if _POLLUTED.search(stored or ""):
        return "不一致", "库里标题混进了列表上的其他文字（点击数、发表时间等）"
    meta = re.search(
        r"""<meta[^>]+name=["']ArticleTitle["'][^>]+content=["']([^"']+)""", page_html or "", re.I
    )
    hay = (
        norm_title(page_text(page_html))
        + norm_title(_page_title(page_html))
        + norm_title(htmllib.unescape(meta.group(1)) if meta else "")
    )
    if t in hay:
        return "一致", ""
    ratio = _longest_run(t, hay) / len(t)
    if ratio >= 0.8:
        return "基本一致", f"原文里最长的相同部分占库里标题的 {ratio:.0%}"
    return "不一致", f"原文里最长的相同部分只占库里标题的 {ratio:.0%}"


_DATE = r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})"
# 发布日期的标签。前面不能紧挨着别的汉字：那是正文里的句子（"询价公告发布日期""公示时间"）
# 单写的"日期""时间"前面也不能是顿号（"一、时间：2026年11月20日"是正文里的活动时间）
_PUB_LABEL = (
    r"(?:(?<![一-龥])"
    r"(?:信息发布日期|信息发布时间|发布日期|发布时间|发表时间|公开日期|信息时间|更新时间|编稿时间)"
    r"|(?<![一-龥、])(?:日期|时间))"
)
# 成文、生成、印发日期：和发布日期常常不是一天，只写了这些的页面单独说明
_DOC_LABEL = r"(?<![一-龥])(?:成文日期|生成日期|印发日期|发文日期)"
_PUB_DATE = re.compile(_PUB_LABEL + r"\s*[:：]?\s*" + _DATE)
_DOC_DATE = re.compile(_DOC_LABEL + r"\s*[:：]?\s*" + _DATE)
# 网页头里的发布时间（宜春、阳江等：页面上的日期由脚本显示，头里写着）
_META_DATE = re.compile(
    r"""<meta[^>]+name=["'](?:PubDate|pubdate|publishdate|PublishDate|firstpublishedtime)["'][^>]+"""
    r"""content=["']\s*""" + _DATE,
    re.I,
)


def _dates(pattern: re.Pattern, text: str) -> list[date]:
    out: list[date] = []
    for m in pattern.finditer(text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d not in out:
            out.append(d)
    return out


def _labeled_dates(text: str) -> list[date]:
    """页面文字里标明是发布日期的日期（测试和旧调用用）。"""
    return _dates(_PUB_DATE, text)


# 标题后面、日期前面出现这些字样时，那个日期不是发布日期
_NOT_PUB = re.compile(
    r"截止|成文|印发|发文|生效|失效|期限|有效期|至|到|开始|结束|报名|申报|时间|日前|之前"
)


def date_forms(d: date) -> list[str]:
    y, m, dd = d.year, d.month, d.day
    return [
        f"{y}-{m:02d}-{dd:02d}",
        f"{y}-{m}-{dd}",
        f"{y}/{m:02d}/{dd:02d}",
        f"{y}/{m}/{dd}",
        f"{y}.{m:02d}.{dd:02d}",
        f"{y}.{m}.{dd}",
        f"{y}年{m}月{dd}日",
        f"{y}年{m:02d}月{dd:02d}日",
    ]


def _title_spans(text: str, title: str, full_only: bool = False) -> list[tuple[int, int]]:
    """标题在页面文字里出现的位置（开头、结尾的原文下标）。整条标题（去掉标点空白）对得上就用
    整条；列表里截断过的标题对不上时用前 10 个字。"""
    full = norm_title(title)
    if len(full) < 6:
        return []
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch):
            if unicodedata.category(c)[0] not in "PSZC":
                chars.append(c)
                index.append(i)
    flat = "".join(chars)
    for key in (full,) if full_only else (full, full[:10]):
        out, start = [], 0
        while (k := flat.find(key, start)) >= 0:
            out.append((index[k], index[k + len(key) - 1] + 1))
            start = k + 1
        if out:
            return out
    return []


def _title_positions(text: str, title: str) -> list[int]:
    return [start for start, _ in _title_spans(text, title)]


# 网址里的年月（"/202608/""/2026/08/""t20260821_"）：页面没写发布日期时，
# 用来判断发文日期是不是发布的那个月
_URL_MONTH = re.compile(
    r"/(20\d{2})[-/]?(0[1-9]|1[0-2])(?:[-/]?\d{2})?/|/t(20\d{2})(0[1-9]|1[0-2])\d{2}_"
)


def _url_month(url: str) -> tuple[int, int] | None:
    m = _URL_MONTH.search(url or "")
    if not m:
        return None
    y, mo = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return int(y), int(mo)


def date_check(
    stored: date | None, page_html: str, title: str = "", url: str = ""
) -> tuple[str, str]:
    """结论：一致 / 页面写的是别的日期 / 原文只标成文日期 / 原文没标发布日期 / 页面上找不到日期。

    依次看：标题后面标注的发布日期（这篇文件自己的；侧栏、相关文章里的日期在别处）→ 标题后面
    紧挨着、没写字样的日期 → 页面上第一个标注的发布日期或网页头的 PubDate → 成文日期（只在页面
    没写发布日期时）。都只从标题结尾往后找：标题里本身带的日期不算。"""
    if stored is None:
        return "页面上找不到日期", "库里没有发布日期"
    # 空白压成一个：标题和日期之间常隔着大段排版用的换行
    text = re.sub(r"\s+", " ", page_text(page_html))
    spans = _title_spans(text, title)
    near: list[date] = []
    for _, end in spans:
        for d in _dates(_PUB_DATE, text[end : end + 300]):
            if d not in near:
                near.append(d)
    if near:
        if stored in near:
            return "一致", ""
        shown = "、".join(d.isoformat() for d in near[:4])
        return "页面写的是别的日期", f"标题后面标的发布日期：{shown}"
    forms = date_forms(stored)
    # 标题下方没写字样的日期只在整条标题对得上时看：只对上前 10 个字时，"结尾"还在标题中间
    for _, end in _title_spans(text, title, full_only=True):
        tail = text[end : end + 80]
        for f in forms:
            m = re.search(rf"(?<!\d){re.escape(f)}(?!\d)", tail)
            # 紧挨在标题后面（30 字以内），中间没有截止、成文、印发、"至"这类字样
            # 后面紧跟"前""起""止"的（"请于10月31日前报送""自11月1日起施行"）也不算
            if (
                m
                and m.start() <= 30
                and not _NOT_PUB.search(tail[: m.start()])
                and not re.match(r"\s*(?:前|起|止|之前|以前|截止)", tail[m.end() :])
            ):
                return "一致", "日期在标题下方（没写发布日期字样）"
    visible = _dates(_PUB_DATE, text)
    written = _dates(_DOC_DATE, text)
    if not visible and stored in written and _url_month(url) == (stored.year, stored.month):
        # 页面没写发布日期：库里是原文的发文/成文日期，网址里的年月也对得上（宿迁）
        return "一致", "原文没写发布日期，和发文/成文日期一样，网址年月也对得上"
    # 网页头的 PubDate 常是系统重新生成页面的时间（济南、赣州比页面上标的晚一两天），
    # 只在页面上没标时用
    published = visible or _dates(_META_DATE, page_html or "")
    if published:
        if stored == published[0]:
            return "一致", ""
        shown = "、".join(d.isoformat() for d in published[:4])
        if stored in published:
            return "页面写的是别的日期", f"库里的日期不是页面上第一个标明的发布日期：{shown}"
        return "页面写的是别的日期", f"页面上标的发布日期：{shown}"
    if stored in written:
        return "一致", "原文没写发布日期，和成文日期一样"
    for form in forms:
        if re.search(rf"(?<!\d){re.escape(form)}(?!\d)", text):
            # 正文里的截止日、会议日期也可能正好是这一天：不算核对过，交给人看
            return "原文没标发布日期", "页面上有这个日期，但没标明是发布日期"
    if written:
        shown = "、".join(d.isoformat() for d in written[:4])
        return "原文只标成文日期", f"原文只写了成文/生成日期 {shown}，没写发布日期"
    return "页面上找不到日期", "页面上没有标明的日期，也找不到库里的日期"


def counties_by_city(path: Path) -> dict[str, frozenset[str]]:
    """地级区划代码 → 它下辖区县的名称写法（全称和至少两个字的简称）。"""
    from youju.govsites.divisions import read_csv

    out: dict[str, set[str]] = {}
    if not path.exists():
        return {}
    for d in read_csv(path):
        if d.level == "区县级" and d.parent_code:
            out.setdefault(d.parent_code, set()).update(
                v for v in name_variants(d.name, d.kind) if len(v) >= 2
            )
    return {code: frozenset(names) for code, names in out.items()}


def city_names(city: str) -> set[str]:
    """看板里城市写简称（南京）或自治州全称；网页上多写全称"南京市"或"恩施州"。"""
    names = {v for v in name_variants(city) if len(v) >= 2}
    if not city.endswith(("州", "盟", "地区", "市")):
        names.add(city + "市")
    return names


# 网站名前后的客套话："欢迎访问焦作市农业农村局网站！""广州市发展和改革委员会网站"
_SITE_NOISE = re.compile(r"^欢迎(?:访问|您)?|(?:门户)?网站[！!]?$|欢迎您[！!]?$|官网$|[！!]$")
# 开发区的网站名常不写"管委会"（"潍坊高新技术产业开发区"），抽查时按名称认
_ZONE = re.compile(r"开发区|高新区|新区|园区|试验区|示范区|保税区|管委会|管理委员会")
# 上级、外省的机关名开头：栏目页网站名以这些开头的不算本市部门
_PROVINCES = (
    "北京|天津|河北|山西|内蒙古|辽宁|吉林|黑龙江|上海|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|"
    "广东|广西|海南|重庆|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆"
)
_UPPER = re.compile(
    rf"^(?:国家|国务院|中华人民共和国|中共中央|全国|省|(?:{_PROVINCES})(?:省|市|自治区)?)"
)
# 文件页上写的发文单位："来源：揭阳市人社局""发布机构：衢州市文化广电旅游局"
_ISSUER = re.compile(
    r"(?:来源|信息来源|发布机构|发文机关|发文单位|发布单位)\s*[:：]\s*([^\s|<>【】]{3,30})"
)


def _site_names(page_html: str) -> list[str]:
    doc = parse_html(page_html)
    out = []
    if doc is not None:
        site = fingerprint(page_html, "", doc, []).site_name
        if site:
            out.append(site)
    title = _page_title(page_html)
    if title:
        out.append(title)
    return out


def _org_parts(names: list[str]) -> list[str]:
    """网站名、标题拆成一段一段的机关名候选（去掉客套话）。"""
    parts = []
    for s in names:
        for part in re.split(r"[-_|·—–>＞:：\s]+", s):
            part = _SITE_NOISE.sub("", _SITE_NOISE.sub("", part.strip()))
            if part:
                parts.append(part)
                # 标题开头的发文机关："广州市发展和改革委员会关于……的通知"
                if "关于" in part:
                    parts.append(part.split("关于", 1)[0])
    return parts


def attribution_check(
    cell: dict,
    column_html: str,
    doc_html: str,
    portal_domain: str = "",
    county_names: frozenset[str] = frozenset(),
    doc_titles: list[str] | None = None,
) -> tuple[str, str]:
    """栏目页和文件页的网站名、标题、发文单位里有没有本市的写法（或栏目在本市门户的域名下），
    能不能归到这个部门。portal_domain：本市门户的本级域名（如 shaoyang.gov.cn）。"""
    city, dept = cell.get("城市", ""), cell.get("部门", "")
    col_names = _site_names(column_html)
    doc_names = _site_names(doc_html) + list(doc_titles or [])
    issuers = [m.group(1) for m in _ISSUER.finditer(page_text(doc_html))][:3]
    names = city_names(city) if city else set()
    texts = col_names + doc_names + issuers
    # 说明写进公开仓库（联网核对表）：网站名、标题里写着人名的整段换成"（标题略）"
    # （govsites/privacy.py）；核对用的还是原文
    where = " / ".join(public_title(t) for t in texts)[:160]
    first_col = (cell.get("栏目网址") or "").split(" ")[0]
    same_domain = bool(portal_domain) and registrable(first_col) == portal_domain
    if not same_domain and (not names or not any(n in s for n in names for s in texts)):
        return "待看", f"网站名、标题和发文单位里没有找到“{city}”：{where or '（都没有）'}"
    if same_domain:
        where = f"栏目在本市门户域名 {portal_domain} 下；" + where
    # 网站名里出现本市下辖区县的名字：可能是区县的网站（扬州、南昌的区县网站在市域名下、
    # 名称也带市名），交给人看
    # 只看栏目页的网站名和标题：文件标题里的"新华社"之类会碰上石家庄新华区这种名字
    # 和本市同名的区县简称（淮安区的"淮安"、南昌县的"南昌"）不算
    named = [
        n for n in county_names if not any(n in c for c in names) and any(n in s for s in col_names)
    ]
    if named:
        return "待看", f"网站名里有区县名“{named[0]}”，可能是区县的网站：{where}"
    if dept == "政府办":
        return "本市本部门", where
    own = sorted(names | {"市"}, key=len, reverse=True)

    def own_org(part: str) -> bool:
        # "市"开头的是本级简称（"市人社局"），"市场监管总局"不是
        return part.startswith(tuple(own)) and not part.startswith("市场")

    def dept_hit(part: str) -> bool:
        return dept in dept_categories(part) or (dept == "开发区" and bool(_ZONE.search(part)))

    for part in _org_parts(col_names):
        if not _UPPER.match(part) and dept_hit(part):
            return "本市本部门", where
    for part in _org_parts(doc_names + issuers):
        if own_org(part) and dept_hit(part):
            return "本市本部门", where
    return "本市", f"认不出是“{dept}”（门户频道或网站名不写部门）：{where}"


# —— 取证（要数据库和页面缓存）——


def _first_list_item(cache, url: str, today: date):
    """库里这一格没有文件时（合并部门共用栏目，文件挂在先抓到的来源下）：重读列表页取第一条带日期的。"""
    from youju.crawler.adapters import api_html, api_refusal, find_list_api
    from youju.crawler.listing import extract_items

    r = cache.get(url)
    if r.status != "ok":
        return None
    final = r.final_url or url
    items = extract_items(r.text, final, today=today)
    if len(items) < 3:
        api = find_list_api(r.text, final)
        if api is not None:
            # 只取页面写明的地址、过读取护栏（D-128）；被拦下的不读
            ar = cache.get(api.url, same_address_only=True)
            ok = ar.status == "ok" and not api_refusal(api, ar)
            body = api_html(api, ar.text) if ok else ""
            via = extract_items(body, api.base_url, today=today) if body else []
            if len(via) > len(items):
                items = via
    return next((i for i in items if i.publish_date), None)


def evidence_for(
    cell: dict,
    session,
    cache,
    today: date,
    portal_domain: str = "",
    county_names: frozenset[str] = frozenset(),
) -> dict:
    from sqlalchemy import select

    from youju.models import Document, Source

    urls = (cell.get("栏目网址") or "").split()
    ev = {f: "" for f in FIELDS}
    ev.update(
        城市=cell.get("城市", ""),
        部门=cell.get("部门", ""),
        部门全称=cell.get("部门全称", ""),
        栏目网址=" ".join(urls),
    )
    if not urls:
        ev.update(程序结论=REVIEW, 打开="看板里这一格没有栏目网址")
        return ev
    candidates: list[tuple[str, str, date | None, str]] = []
    ids = list(session.scalars(select(Source.id).where(Source.url.in_(urls))))
    if ids:
        rows = session.execute(
            select(Document.title, Document.url, Document.publish_date)
            .where(Document.source_id.in_(ids), Document.publish_date.is_not(None))
            .order_by(Document.publish_date.desc(), Document.id.desc())
            .limit(10)
        ).all()
        candidates = [(t, u, d, "库里") for t, u, d in rows]
    if not candidates:
        for url in urls:
            item = _first_list_item(cache, url, today)
            if item is not None:
                candidates = [(item.title, item.url, item.publish_date, "列表页重读")]
                break
    if not candidates:
        ev.update(
            来源="列表页重读", 程序结论=REVIEW, 打开="库里没有文件，列表页也没读出带日期的条目"
        )
        return ev
    # 先挑政府网站上的网页（栏目里转的新华网、公众号外链和附件不核），原文打不开就顺延，最多试 3 份
    skipped: list[str] = []
    chosen = None
    tries = 0
    for title, doc_url, published, origin in candidates:
        if not is_gov_url(doc_url):
            skipped.append("外链")
            continue
        if _ATTACHMENT.search(doc_url.split("?")[0]):
            skipped.append("附件")
            continue
        tries += 1
        r = cache.get(doc_url)
        chosen = (title, doc_url, published, origin, r)
        if r.status == "ok" or tries >= 3:
            break
        skipped.append("打不开")
    if chosen is None:
        title, doc_url, published, origin = candidates[0]
        ev.update(文件标题=title, 文件网址=doc_url, 来源=origin, 程序结论=REVIEW)
        ev.update(
            发布日期=published.isoformat() if published else "", 打开="最近的文件都是外链或附件"
        )
        return ev
    title, doc_url, published, origin, r = chosen
    ev.update(文件标题=title, 文件网址=doc_url, 来源=origin)
    ev["发布日期"] = published.isoformat() if published else ""
    if skipped:
        ev["备注"] = "跳过更新的：" + "、".join(skipped)
    if r.status != "ok":
        ev.update(打开=f"打不开：{(r.error or r.status)[:80]}", 程序结论=REVIEW)
        return ev
    ev["打开"] = "ok" if (r.final_url or doc_url) == doc_url else f"ok（跳到 {r.final_url}）"
    col = cache.get(urls[0])
    ev["标题核对"], ev["标题说明"] = title_check(title, r.text)
    ev["日期核对"], ev["日期说明"] = date_check(published, r.text, title, doc_url)
    # 原文里核对过的标题（"濮阳市市场监督管理局关于……"）也拿来认部门
    confirmed = [title] if ev["标题核对"] in ("一致", "基本一致") else []
    ev["归属核对"], ev["归属说明"] = attribution_check(
        cell, col.text if col.status == "ok" else "", r.text, portal_domain, county_names, confirmed
    )
    passed = (
        ev["标题核对"] in ("一致", "基本一致")
        and ev["日期核对"] == "一致"
        and ev["归属核对"] == "本市本部门"
    )
    ev["程序结论"] = OK if passed else REVIEW
    return ev


_ATTACHMENT = re.compile(r"\.(?:pdf|docx?|xlsx?|wps|ofd|zip|rar|7z|txt|jpe?g|png)$", re.I)


def _md_cell(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ")


def _md_link(text: str, url: str) -> str:
    if not url:
        return _md_cell(text)
    return f"[{_md_cell(text) or '链接'}]({url.replace(' ', '%20').replace(')', '%29')})"


def render_markdown(
    province: str, level: str, population: int, seed: str, rows: list[dict], today: date
) -> str:
    ok = sum(r["程序结论"] == OK for r in rows)
    lines = [
        f"# {province}{level}联网核对（程序取证，{today.isoformat()}）",
        "",
        f"抽样方法：总体是覆盖看板里{province}{level}“已收录”的 {population} 格；按"
        f"“种子 {seed} + 区划代码 + 部门 + 栏目网址”的 SHA-1 从小到大取前 {len(rows)} 格"
        "（重跑抽到的一样）。每格取库里这格栏目下发布日期最新的一份文件（库里没有时重读列表页取"
        "第一条带日期的），打开原网页核对标题、发布日期，并看栏目页和文件页的网站名、标题是不是"
        "本市、本部门。",
    ]
    if population < 20:
        lines.append("")
        lines.append(f"已收录的格不足 20 个，全部 {population} 格都抽了。")
    lines += [
        "",
        "| 序号 | 城市 | 部门 | 栏目 | 文件标题 | 发布日期 | 标题 | 日期 | 归属 | 程序结论 |",
        "|---:|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        first_col = (r["栏目网址"] or "").split(" ")[0]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(r["序号"]),
                    _md_cell(r["城市"]),
                    _md_cell(r["部门"]),
                    _md_link("栏目页", first_col),
                    _md_link(r["文件标题"][:40], r["文件网址"]),
                    _md_cell(r["发布日期"]),
                    _md_cell(r["标题核对"] or r["打开"]),
                    _md_cell(r["日期核对"]),
                    _md_cell(r["归属核对"]),
                    _md_cell(r["程序结论"]),
                ]
            )
            + " |"
        )
    lines += [
        "",
        f"合计：程序判合格 {ok}/{len(rows)}，待人工看 {len(rows) - ok}。"
        "“待人工看”的逐条原因见同名 CSV 的“标题说明”“日期说明”“归属说明”。",
        "",
        "抽查人签字：（待 Claude 逐条看过后填写）",
        "",
        "创始人复看：第 1 条（约 5%）",
        "",
    ]
    return "\n".join(lines)


def write_outputs(
    out: Path,
    province: str,
    level: str,
    population: int,
    seed: str,
    rows: list[dict],
    today: date,
) -> tuple[Path, Path]:
    # 写到 <out>/<省>/联网核对_<级>.csv（docs 里用 地市第二批/，只归地市一组）。
    # 抽查/<级>/<省>.csv 是主线 coverage-sample 的统一证据表，这里不写；核对结论填进它的复核三列
    folder = out / province
    folder.mkdir(parents=True, exist_ok=True)
    # 公开仓库：个人事项或写着人名的标题不写原文（和抽查证据同一个规则，govsites/privacy.py）；
    # 核对用的是库里的原标题，这里只改写出去的
    rows = [
        {**r, "文件标题": public_title(r["文件标题"])} if r.get("文件标题") else r for r in rows
    ]
    # 换种子复验时文件名带上种子，不覆盖原记录
    name = f"联网核对_{level}" + ("" if seed == DEFAULT_SEED else f"_{seed}")
    csv_path = folder / f"{name}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS)
        w.writeheader()
        w.writerows(rows)
    md_path = folder / f"{name}.md"
    md_path.write_text(
        render_markdown(province, level, population, seed, rows, today), encoding="utf-8"
    )
    return csv_path, md_path


def main(argv: list[str] | None = None) -> int:
    from youju.db import session_factory
    from youju.govsites.cli import _fetcher
    from youju.govsites.pagecache import PageCache

    p = argparse.ArgumentParser(description="覆盖抽查：按省抽已收录的格，核对原网页")
    p.add_argument("--board", type=Path, action="append", required=True, help="试点覆盖/<省>.csv")
    p.add_argument("--level", default="市级", choices=("国家级", "省级", "市级", "区县级"))
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--seed", default=DEFAULT_SEED)
    p.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "youju-coverage-city1")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--offline", action="store_true", help="只用页面缓存，不联网")
    p.add_argument(
        "--divisions",
        type=Path,
        default=Path(__file__).resolve().parents[4]
        / "docs"
        / "数据来源"
        / "全国覆盖"
        / "行政区划.csv",
        help="行政区划清单（认区县名用）",
    )
    args = p.parse_args(argv)
    today = date.today()
    county_index = counties_by_city(args.divisions)
    with _fetcher() as fetcher, session_factory()() as session:
        cache = PageCache(args.cache, None if args.offline else fetcher, offline=args.offline)
        for board in args.board:
            with board.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            province = rows[0]["省份"] if rows else board.stem
            population = sum(
                r.get("层级") == args.level and r.get("状态") == "已收录" for r in rows
            )
            picked = pick_cells(rows, args.level, args.n, args.seed)
            # 本市门户的本级域名：政府办那一格的部门网址就是门户
            portals = {
                r.get("区划代码"): registrable(r.get("部门网址") or "")
                for r in rows
                if r.get("部门") == "政府办" and r.get("部门网址")
            }
            evidence = []
            for i, cell in enumerate(picked, start=1):
                domain = portals.get(cell.get("区划代码"), "")
                counties = county_index.get(cell.get("区划代码") or "", frozenset())
                ev = evidence_for(cell, session, cache, today, domain, counties)
                ev["序号"] = i
                evidence.append(ev)
                print(f"{province} {i}/{len(picked)} {ev['城市']} {ev['部门']}：{ev['程序结论']}")
            csv_path, md_path = write_outputs(
                args.out, province, args.level, population, args.seed, evidence, today
            )
            ok = sum(e["程序结论"] == OK for e in evidence)
            print(f"{province}：程序判合格 {ok}/{len(evidence)}；写入 {md_path}、{csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
