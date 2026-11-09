"""地市级导航补充规则（地市第 2 批，D-126）：通用导航（links.nav_links、页脚脚本）认不出的几种
地市门户写法。只在官方页面上找，不猜网址。

1. 首页只有一段跳转脚本，地址放在变量里或带条件：按设备分手机版、电脑版（南京），
   http 换 https（徐州），按主机名分站点（益阳）。跟到电脑版、本站的那个地址。
2. "市直部门""政府部门""县区政府"这类下拉菜单由单独的链接脚本生成（安徽各市的 ljgl 脚本、
   黄冈）：脚本标签前面紧挨着的文字就是菜单名。按菜单名挑脚本，不按文件名。
3. 部门列表页的叫法：部门频道、市政府工作机构、机构职能、市政府部门、市直部门、政务公开……
"""

import re
from urllib.parse import urlsplit, urlunsplit

from youju.crawler.urls import normalize_url
from youju.govsites.links import Link, registrable

# —— 1. 首页跳转 ——

_SCRIPT_BODY = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
# 变量赋值成一个字面地址；后面还有"+ 拼接"的不算（只拿得到半截地址）
_VAR = re.compile(
    r"""\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*(["'])([^"'\n]{1,200})\2(?!\s*\+)"""
)
_REDIRECT = re.compile(
    r"""(?:window\.|document\.|self\.|top\.)?location(?:\.href)?\s*=\s*"""
    r"""(?:(["'])([^"'+\n]{1,200})\1|([A-Za-z_$][\w$]*))\s*(?:;|$|\n|\})"""
    r"""|(?:window\.|document\.|self\.|top\.)?location\.replace\(\s*"""
    r"""(?:(["'])([^"'+\n]{1,200})\4|([A-Za-z_$][\w$]*))\s*\)""",
    re.M,
)
# 手机版的写法：变量名或网址里的 m、wap、mobile、phone、h5、sjb（手机版）、yd（移动端）
_MOBILE_NAME = re.compile(r"^(?:m|yd|wap|mobile|phone|h5|sj|sjb|mob)(?:_?url|_?path)?$", re.I)
_MOBILE_URL = re.compile(
    r"(?:^|[/._-])(?:m|wap|mobile|phone|h5|yd|sj)(?:[/._-]|$)|sjb|shouji", re.I
)
# 跳转语句紧挨着的 if 条件（益阳：host=="www.yiyang.gov.cn"；停用的跳转：'9103'=='9102'）
_IF_COND = re.compile(r"""if\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*\{?\s*$""")
_HOST_LITERAL = re.compile(r"""["']([a-z0-9.-]+\.[a-z]{2,})["']""", re.I)
_LITERAL_CMP = re.compile(r"""^\s*(["'])(.*?)\1\s*(===?|!==?)\s*(["'])(.*?)\4\s*$""")
# 按设备分手机版、电脑版的页面（只有这种页面才在看不懂的条件分支里挑电脑版）
_DEVICE_PAGE = re.compile(r"navigator\.userAgent|IsPC|isMobile|is_mobile|browserRedirect", re.I)


def _strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return "\n".join(line for line in code.splitlines() if not line.strip().startswith("//"))


def _literal_condition(cond: str) -> bool | None:
    """条件只是两个字面值比较（'9103'=='9102'）：算出真假；别的条件返回 None。"""
    m = _LITERAL_CMP.match(cond)
    if not m:
        return None
    same = m.group(2) == m.group(5)
    return same if m.group(3).startswith("=") else not same


def device_redirect(html: str, base_url: str) -> str | None:
    """只有跳转脚本的首页：要跟去的电脑版、本站地址；认不出返回 None。

    调用方只在页面几乎没有正文时调用（inventory._script_redirect 先判断）。条件分支里的跳转
    只在看得懂时才跟：条件写主机名的，只跟本主机那条；两个字面值比较的照算真假；按设备分版的
    页面挑电脑版；其他看不懂的条件一律不跟。"""
    base_host = (urlsplit(base_url).hostname or "").lower()
    candidates: list[tuple[int, str]] = []
    for script in _SCRIPT_BODY.findall(html or ""):
        code = _strip_comments(script)
        if not code.strip():
            continue
        device_page = bool(_DEVICE_PAGE.search(code))
        # http 换 https：location.href = "https:" + location.href.substring(...)
        if (
            "location.protocol" in code
            and re.search(r"""["']https:["']""", code)
            and re.search(r"location\.href\.substring|location\.host", code)
            and base_url.startswith("http://")
        ):
            parts = urlsplit(base_url)
            return urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))
        variables = {m.group(1): m.group(3) for m in _VAR.finditer(code)}
        for m in _REDIRECT.finditer(code):
            literal = m.group(2) or m.group(5)
            name = m.group(3) or m.group(6)
            target = literal if literal is not None else variables.get(name or "")
            if not target or target.startswith(("javascript:", "#")):
                continue
            cond = _IF_COND.search(code[: m.start()].rstrip())
            hosts: set[str] = set()
            if cond:
                verdict = _literal_condition(cond.group(1))
                if verdict is False:
                    continue  # 停用的跳转
                hosts = {h.lower() for h in _HOST_LITERAL.findall(cond.group(1))}
                if hosts and "!=" in cond.group(1):
                    # host != "某主机"：对这些主机以外才跳
                    if base_host in hosts:
                        continue
                elif hosts and base_host not in hosts:
                    continue  # 只对别的主机生效
                if not hosts and verdict is None and not device_page:
                    continue  # 看不懂的条件
            elif not device_page and literal is not None:
                continue  # 无条件的字面地址由 _script_redirect 的严格规则管，这里不放宽
            mobile = bool(
                (name and _MOBILE_NAME.match(name)) or _MOBILE_URL.search(target.split("?")[0])
            )
            rank = (0 if hosts else 1) + (2 if mobile else 0)
            candidates.append((rank, target))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    rank, target = candidates[0]
    if rank >= 2:
        return None  # 只剩手机版
    full = normalize_url(base_url, target)
    if not full or registrable(full) != registrable(base_url):
        return None
    return full


# —— 2. 带菜单名的链接脚本 ——

_SCRIPT_SRC = re.compile(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["'][^>]*>""", re.I)
# 菜单名：部门、机构、单位、县区等的网站列表（"党群机构"也会挑上，认不认部门由找部门那一步管）
_MENU_LABEL = re.compile(
    r"部门|机构|单位|县区|区县|县（市|县\(市|县市区|市县|市直|管委|园区|政府网站|网站群"
)
_LIB = re.compile(
    r"jquery|swiper|layui|vue|echarts|bootstrap|polyfill|require|md5|crypto|baidu|cnzz|hm\.js|"
    r"wza|jiucuo|conac|share|qrcode|zepto|lazy|/lib|\.min\.js|analytics|common|patch|aiqs",
    re.I,
)


def labeled_scripts(html: str, base_url: str, limit: int = 8) -> list[tuple[str, str]]:
    """标签文字像菜单名的本站链接脚本：[(脚本网址, 菜单名)]，按页面顺序。

    菜单名取脚本标签前面紧挨着的一段文字（去掉注释、行内脚本和标签）。"""
    home = registrable(base_url)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    text = html or ""
    for m in _SCRIPT_SRC.finditer(text):
        src = normalize_url(base_url, m.group(1))
        if not src or src in seen or registrable(src) != home or _LIB.search(src):
            continue
        before = text[max(0, m.start() - 800) : m.start()]
        before = re.sub(r"<!--.*?-->", "|", before, flags=re.S)
        before = re.sub(r"<script\b[^>]*>.*?</script>", "|", before, flags=re.S | re.I)
        segments = [s.strip() for s in re.split(r"<[^>]*>|\|", before) if s.strip()]
        label = segments[-1] if segments else ""
        if len(label) > 16 or not _MENU_LABEL.search(label):
            continue
        seen.add(src)
        out.append((src, label))
        if len(out) >= limit:
            break
    return out


# —— 3. 地市门户上部门列表页的叫法 ——

CITY_NAV_WORDS = (
    "部门频道",
    "工作机构",
    "工作部门",
    "机构职能",
    "政府部门",
    "市直部门",
    "部门导航",
    "部门链接",
    "政府组成部门",
    "政务公开",
)
_NAV_MAX_LEN = 16
# 这两种补充导航页的说明（PageInfo.nav_text）：不能带"部门""机构""单位""市直"等字，
# 免得找部门时加分、顶掉原有导航找到的部门网站（11/9 杭州的部门网站被门户里的部门频道顶掉过）
CITY_NAV_NOTE = "地市补充·列表页"
CITY_SCRIPT_NOTE = "地市补充·菜单脚本"


def city_nav_pages(home, seen: set[str], visit) -> None:
    """地市门户：通用导航词没收的部门列表页走一遍（Inventory._nav_pages 的挂钩）。

    visit(link, 说明) 是 _nav_pages 里的访问函数；说明用中性的 CITY_NAV_NOTE：这些页上的
    部门不加分，不顶掉原有导航找到的部门网站。"""
    for link in city_nav_links(home.links, home.final_url, seen):
        visit(link, CITY_NAV_NOTE)


# 部门链接指向这类页面的导航页，是"机构简介""信息公开指南""部门规范性文件"一类的列表，
# 不是部门网站导航（宜宾门户上按部门分的规范性文件列表 /bmgfxwj/）
_WEAK_TARGET = re.compile(
    r"/info/\d+/\d+\.s?html?$|/content/|zfxxgkzn|xxgkzn|xxgk_tt|zfxxgknb|xxgknb|/nb/|bmnb|"
    r"/jgjj|/jgzn|/jgzz|/zzjg|/zyzz|/ldjj|/ldxx|/bmgfxwj/",
    re.I,
)


def _weak_target(url: str) -> bool:
    from youju.govsites.platform import URL_SHAPES

    parts = urlsplit(url)
    path = parts.path or "/"
    full = path + ("?" + parts.query if parts.query else "")
    if _WEAK_TARGET.search(full):
        return True
    # 文章网址形状算弱链接，但频道页的例外不算（遂宁的部门频道 /dept/<编号>.html）
    return any(p.search(full) and not channel_shape(name, path) for name, p in URL_SHAPES)


def prune_weak_links(page) -> None:
    """同一页上同一个部门网站（同一主机）既有首页链接、又有它的信息公开指南、简介页链接的
    （镇江"市级部门政府信息公开"页：正文是指南页、页脚是首页），去掉指南、简介那条。"""
    from youju.govsites.names import dept_categories

    # 只在同一主机（同一个机构的网站）另有不是简介、指南的链接时才剪：按部门类别剪会把
    # 泸州的"市商务会展局"剪掉、换成同属"商务"类的口岸和物流办公室
    good_hosts: set[str] = set()
    for link in page.links:
        if dept_categories(link.text) and not _weak_target(link.url):
            good_hosts.add((urlsplit(link.url).hostname or "").lower())
    page.links = [
        link
        for link in page.links
        if not (
            dept_categories(link.text)
            and _weak_target(link.url)
            and (urlsplit(link.url).hostname or "").lower() in good_hosts
        )
    ]


def weak_dept_list(page) -> bool:
    """导航页上的部门链接多半指向机构简介文章、信息公开指南、年报页（锦州"机构简介"、
    镇江"市级部门政府信息公开"）：不是部门网站的导航。"""
    from youju.govsites.names import dept_categories

    targets = [link.url for link in page.links if dept_categories(link.text)]
    if len(targets) < 3:
        return False
    return sum(_weak_target(u) for u in targets) * 5 >= len(targets) * 3


def demote_weak_lists(pages: list) -> None:
    """地市门户的导航页：同一部门有首页链接时去掉它的指南、简介链接；部门链接多半是简介文章、
    指南、年报页的，说明改成中性（不给找部门加分），免得顶掉首页上的部门网站（11/9 锦州、镇江）。
    年度报表页不动：它们靠"首页网址"找部门网站。"""
    from youju.govsites.links import REPORT_WORDS

    for p in pages[1:]:
        if REPORT_WORDS.search(p.nav_text or "") or p.doc is None:
            continue
        prune_weak_links(p)
        if p.nav_text not in (CITY_NAV_NOTE, CITY_SCRIPT_NOTE) and weak_dept_list(p):
            p.nav_text = CITY_NAV_NOTE


def city_menu_scripts(home, scripts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """地市门户："市直部门""政府部门"菜单的链接脚本排在页脚脚本前面（_nav_pages 的挂钩）。"""
    if not home.result.text:
        return scripts
    labeled = [
        (src, CITY_SCRIPT_NOTE) for src, _ in labeled_scripts(home.result.text, home.final_url)
    ]
    chosen = {src for src, _ in labeled}
    return labeled + [(s, label) for s, label in scripts if s not in chosen]


def city_nav_links(links: list[Link], page_url: str, skip: set[str], limit: int = 6) -> list[Link]:
    """地市门户上通用导航词没收的部门列表页（同一家网站，文字短）。部门导航在前，政务公开在后。"""
    home = registrable(page_url)
    out: list[Link] = []
    seen = set(skip)
    for link in links:
        if len(link.text) > _NAV_MAX_LEN or link.url in seen:
            continue
        if not any(w in link.text for w in CITY_NAV_WORDS):
            continue
        if registrable(link.url) != home:
            continue
        seen.add(link.url)
        out.append(link)
    out.sort(key=lambda link: "政务公开" in link.text)
    return out[:limit]


# —— 4. http 端口断开连接、https 正常 ——

# 连接层面的失败（服务器没回应、直接断开）：不是网站回了"拒绝"（403、412、521 等照旧记不能抓）
_DROPPED = re.compile(
    r"RemoteProtocolError|Server disconnected|no response line|Connection reset|"
    r"ConnectError|ConnectionRefused|连接被|断开|超时|Timeout",
    re.I,
)


def https_fallback(url: str, status: str, error: str | None) -> str | None:
    """官方导航给的是 http 地址、网站的 http 端口却断开连接或不应答时，同一网站的 https 地址。

    福州、盘锦（断开）、眉山（超时）的门户这样，11/9 实测 https 能打开。只换协议，不换主机、
    路径和标识；https 那边的 robots.txt 由抓取器按新地址重新取、重新判断。网站回了 403、412、
    521 等的不换（那是拒绝，照旧记不能抓）。"""
    if not url.startswith("http://") or status not in ("robots_error", "network_error"):
        return None
    if not _DROPPED.search(error or ""):
        return None
    return "https://" + url[len("http://") :]


# —— 5. 上级网站链到下级门户的某一页 ——

_HOME_NAMES = re.compile(r"^(?:index|default|home|main|portal|sy|shouye)(?:_?pc)?$", re.I)
_PAGE_EXT = re.compile(r"\.(?:s?html?|jsp|aspx?|php|do)$", re.I)


def specific_page(url: str) -> bool:
    """网址是网站里的某一页（年报、栏目、文章），不是首页：目录、index.html 之类不算。

    省网站的信息公开页上链到下级门户的常常是它的"政府信息公开年报"页（11/9 临沂）、
    某篇文章（保定）：拿它当门户首页，首页上的导航和部门就全找错了。"""
    parts = urlsplit(url)
    # 只用于省找地市（地市门户在自己的主机上）：带参数的是门户里的某个频道（淄博 ?open=zfxxgknb
    # 是年报频道）。区县门户常挂在地市平台的参数页上，那里不用这条
    if parts.query:
        return True
    last = (parts.path or "/").rsplit("/", 1)[-1]
    if not last or not _PAGE_EXT.search(last):
        return False
    return not _HOME_NAMES.match(_PAGE_EXT.sub("", last))


# —— 6. 像文章网址、其实是频道页的 ——

# 部门目录：遂宁的部门频道是 /dept/<32 位编号>.html
_DEPT_DIRS = {"dept", "depts", "department", "bm", "bmpd", "bumen", "jgsz", "zfbm", "szfbm"}
_COLUMNS_CHANNEL = re.compile(r"^/columns/[0-9a-f-]{36}/(?:index\.s?html?)?$", re.I)


def channel_shape(shape: str, path: str) -> bool:
    """文章网址形状里两种其实是频道页的写法（_site_like 用）：

    - "32位编号"：上一层目录是部门目录（/dept/<编号>.html，遂宁）；
    - "columns-uuid"：网址到栏目编号目录为止（/columns/<编号>/，石家庄的栏目页，文章在它下面）。"""
    if shape.startswith("32位编号"):
        segments = [s for s in path.split("/") if s]
        return len(segments) >= 2 and segments[-2].lower() in _DEPT_DIRS
    if shape == "columns-uuid":
        return bool(_COLUMNS_CHANNEL.match(path))
    return False
