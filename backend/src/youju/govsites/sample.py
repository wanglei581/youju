"""每省抽查的证据表（D-122、D-126：我们抽查、附证据，创始人复看约 5%）。

从库里（本机试抓用的临时库）取来源、最后一次检查和读出的条目，每个抽到的栏目一行：部门、栏目、
抓取结果、最新 3 条（标题、日期、网址），再自动核五项——

- 部门：部门全称以本省（国家级不看）名称开头，而且能归到这一格的部门；
- 栏目：是通知公告、政策文件、公示公告三类之一；
- 条目：最后一次检查读出至少 3 条，而且条目有发布日期；
- 新鲜：最新一条在 365 天内（政策文件栏目更新慢，放宽到一年）；
- 本站：最新几条的网址和栏目在同一个网站（可注册域名相同）。

11/9 第 1 批独立复核在自动通过的里查出 13% 有问题，又加了几项（看最新 3 条，两条以上像才算）：

- 标题：像脚本代码、按钮文字（"| AI阅读"）、编号，或者太短；
- 年报：多是政府信息公开年度报告、法治政府建设情况报告一类；
- 上级：省级栏目里多是不带本省名称的国家层面文件（书名号开头的规划、国务院、部委发文），
  会被记成本省发文；
- 附件：条目链到的是 doc、xls、pdf 这类附件，不是文章页；
- 阅读器：条目链到 PDF 阅读页（pdf.js 的 viewer.html?file=…），抓正文会抓成阅读器的界面文字；
- 要闻：多是时政要闻（领导人活动、讲话）；
- 部门（补充）：国家级按部委名核对；市场监管格挂的是知识产权局；
- 重复：同一省里去掉参数后是同一个列表的，后面那个记重复。

地市、区县再加一项（D-129 的个人事项门，看最新 20 条）：

- 个人事项：个人名单类和招聘、考试、任免类标题占 60% 以上（见 personal_list_title），交给人看：
  整栏都是的记"不收"，还有政策、通知的照收（个人事项按 D-097 ② 不给正文）。

都过记"自动通过"，否则记"待人工"并写明哪项没过；最后三列（复核人、复核结论、复核备注）
留给人工复核。复核结论写"通过""不通过""待重试"（这次没抓到）、"不能抓"（按访问控制等不读）
或"不收"（读得对、但不在收录范围，如整栏是招聘公示），后面可以加括号说明（见 final_verdict）。
地市、区县每省另有联网核对抽样，按抽样线判（见 SAMPLE_LINES）。只读库、不联网。
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.govsites.divisions import name_variants
from youju.govsites.links import registrable
from youju.govsites.names import column_kind, dept_categories
from youju.govsites.privacy import public_title
from youju.models import Document, Source, SourceCheck

SAMPLE_FIELDS = (
    "层级",
    "省份",
    "城市",
    "区县",
    "部门",
    "部门全称",
    "栏目名称",
    "栏目类型",
    "栏目URL",
    "来源编号",
    "最后检查",
    "读出条目",
    "名下文件",
    "最新1",
    "最新2",
    "最新3",
    "自动结论",
    "没过的项",
    "复核人",
    "复核结论",
    "复核备注",
)
FRESH_DAYS = 365
KINDS = ("通知公告", "政策文件", "公示公告")

_BAD_TITLE = re.compile(
    r"var\s|function\s*\(|document\.|-->|<[a-z/]|[{}]|AI阅读|阅读全文|查看详情|^\s*\|"
)
_CODE_ONLY = re.compile(r"^[A-Za-z0-9/\-_.（）()]{6,}$")
_REPORT = re.compile(r"年度报告|年报|年度报表|法治政府建设.{0,6}报告")
# 国家层面文件：书名号开头的规划、"中华人民共和国……"、中央和国务院、国家××局、
# ××部（后面跟等、顿号、空格、关于、令、办公厅）
_CENTRAL = re.compile(
    r"^《|^中华人民共和国|^(?:中共中央|国务院)|^国家[^级]{0,12}(?:局|委|部|署|院)"
    r"|^[^省市区县]{2,10}(?:部|总局|委员会)(?:等|、|\s|关于|令|办公厅|办公室)"
)
# 只有规章名、不带"关于"的（"博物馆藏品管理办法""市场监督管理所条例"）：省里的规章一般带省名
_BARE_RULE = re.compile(r"(?:办法|规定|条例|法典|细则|规则|法)$")
_VIEWER = re.compile(r"pdfjs|viewer\.html\?file=", re.I)
_NEWS = re.compile(r"习近平|总书记|李强|赵乐际|王沪宁|蔡奇|丁薛祥|李希|主持召开|重要讲话|会见")
_ATTACH = re.compile(r"\.(?:docx?|xlsx?|pdf|wps|zip|rar|ofd)(?:$|\?)", re.I)
# 国家级的格对应的部委（部门全称里要有这些字）
_MINISTRY = {
    "政府办": r"国务院|中国政府网|中华人民共和国中央人民政府",
    "发改委": r"发展和改革委员会|发展改革委",
    "工信": r"工业和信息化部",
    "科技": r"科学技术部|科技部",
    "商务": r"商务部",
    "财政": r"财政部",
    "人社": r"人力资源和社会保障部|人力资源社会保障部",
    "市场监管": r"市场监督管理总局|市场监管总局",
    "农业农村": r"农业农村部",
    "文旅": r"文化和旅游部",
    "生态环境": r"生态环境部",
    "数据局": r"国家数据局",
}


@dataclass
class SampleRow:
    source: Source
    status: str = ""
    items: int = 0
    documents: int = 0
    latest: list[tuple[str, date | None, str]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return "自动通过" if not self.failed else "待人工"


def _pick(sources: list[Source], size: int | None) -> list[Source]:
    """每省抽 size 个：按网址散列排序取前面的，重跑抽到的一样；不给 size 就全查。"""
    if not size:
        return sources
    by_prov: dict[str, list[Source]] = {}
    for s in sources:
        by_prov.setdefault(s.province, []).append(s)
    out: list[Source] = []
    for group in by_prov.values():
        group.sort(key=lambda s: hashlib.sha1(s.url.encode()).hexdigest())
        out += group[:size]
    return out


# 个人事项门（D-129，方案第三节）：栏目试抓到的最新 20 条里，个人名单类标题（lists/rules.py 的
# PERSON_TITLE：退休、病残、工伤认定、补贴人员、人才名单、任免任职、负责人……，D-091 ③、D-092 不给
# 正文的那套）和招聘、考试、任免类占 60% 以上的，交给人看。不用 PERSONAL_MATTER_WORDS：那是"不给
# 正文"的词表，处罚、失信、社保、职称会把执法公示、政策栏目也算进去；也不认"遴选"（选承保机构、招标
# 代理也叫遴选）。不足 5 条的不判
PERSONAL_SHARE = 0.6
PERSONAL_RECENT = 20
PERSONAL_MIN_ITEMS = 5
_RECRUIT_WORDS = (
    *("招聘", "招录", "录用", "拟聘", "考试", "笔试", "面试", "成绩", "选调"),
    *("任免", "任职", "任前", "免职", "拟任", "任命"),
)


def personal_list_title(title: str) -> bool:
    """标题是个人名单类或招聘、考试、任免类（个人事项门用）。"""
    from youju.lists.rules import PERSON_TITLE

    text = re.sub(r"\s+", "", title or "")
    return any(w in text for w in PERSON_TITLE) or any(w in text for w in _RECRUIT_WORDS)


def personal_share(titles: list[str]) -> float | None:
    """个人事项门：这些标题里个人名单类的占比；不足 PERSONAL_MIN_ITEMS 条时是 None（不判）。"""
    if len(titles) < PERSONAL_MIN_ITEMS:
        return None
    return sum(personal_list_title(t) for t in titles) / len(titles)


def _kind(name: str | None) -> str | None:
    hit = column_kind(name or "")
    return hit[0] if hit else None


# 省级部门全称常只写本级前缀（"省农业农村厅""区商务厅""自治区人社厅""市发展改革委"）
_MUNICIPALITIES = ("北京", "天津", "上海", "重庆")
_REGIONS = ("内蒙古", "广西", "西藏", "宁夏", "新疆")
# 省份全称（"吉林省""广西壮族自治区""北京市"）
_PROVINCE_FULL = re.compile(
    r"^(?:(?:北京|天津|上海|重庆)市"
    r"|(?:河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|海南"
    r"|四川|贵州|云南|陕西|甘肃|青海|台湾)省"
    r"|(?:内蒙古|广西壮族|广西|西藏|宁夏回族|宁夏|新疆维吾尔|新疆)自治区)"
)


def _autonomous(city: str) -> bool:
    """自治州、盟：部门全称常只写本级前缀"州""盟"。地级市以"州"结尾的都是两个字（"苏州""杭州"，
    行政区划清单里 293 个地级市核过，测试钉住），自治州写全称或"恩施州"这样三个字以上的简称。"""
    return city.endswith(("自治州", "盟")) or (city.endswith("州") and len(city) >= 3)


def _city_names(s: Source) -> set[str]:
    names = {v for v in name_variants(s.city) | {s.city} if len(v) >= 2}
    if not (_autonomous(s.city) or s.city.endswith(("地区", "市"))):
        names.add(s.city + "市")  # 地级市在库里写简称（"南京""苏州"），部门全称写"南京市……"
    return names


def counties_by_city(path: Path) -> dict[tuple[str, str], frozenset[str]]:
    """(省份, 城市) → 下辖区县的全称写法（以区、县、市、旗结尾的）。地市部门全称在本市名称后面紧跟
    这些的，是区县的部门（"南京市玄武区……""苏州市常熟市人民政府"）。不用简称："公安县"的"公安"
    会碰上"荆州市公安局"。城市认全称和简称（"吉林市""吉林""恩施州"），来源里怎么写都查得到；
    清单不存在时报错（FileNotFoundError），不静默放过。"""
    from youju.govsites.divisions import read_csv

    divisions = read_csv(path)
    by_parent: dict[str, set[str]] = {}
    for d in divisions:
        if d.level == "区县级" and d.parent_code:
            by_parent.setdefault(d.parent_code, set()).update(
                v
                for v in name_variants(d.name, d.kind) | {d.name}
                if len(v) >= 2 and v.endswith(("区", "县", "市", "旗"))
            )
    out: dict[tuple[str, str], frozenset[str]] = {}
    for d in divisions:
        if d.level == "市级" and d.code in by_parent:
            names = frozenset(by_parent[d.code])
            for v in name_variants(d.name, d.kind) | {d.name, d.city or ""}:
                if len(v) >= 2:
                    out[(d.province, v)] = names
    return out


def _own_prefixes(s: Source) -> set[str]:
    if s.level == "市级" and s.city:
        # 地市部门全称写本市名称（"南京市发展和改革委员会""恩施州农业农村局"）、省名连市名
        # （"江苏省南京市……"），或只写本级前缀（"市大数据局""州农业农村局""盟行政公署"）。只写省名的
        # （"江苏省发展和改革委员会"）是省直部门，挂在地市格上不对，不算（11/11 地市一组报：
        # 原来只认省名，568 个地市栏目里 552 个卡在"部门"）
        city = _city_names(s)
        prov = {v for v in name_variants(s.province) | {s.province} if len(v) >= 2}
        prov |= {s.province + "省", s.province + "自治区"}  # 库里省份写简称（"江苏"）
        own = city | {p + c for p in prov for c in city}
        own.add("市")
        if _autonomous(s.city):  # "苏州""杭州"是地级市，不加"州"
            own.add(s.city[-1])
        return own
    own = {v for v in name_variants(s.province) | {s.province} if len(v) >= 2}
    if s.level == "省级":
        if s.province in _MUNICIPALITIES:
            own.add("市")
        elif s.province in _REGIONS:
            own |= {"自治区", "区"}
        else:
            own.add("省")
    return own


def _dept_ok(s: Source, counties: frozenset[str] = frozenset()) -> bool:
    """部门全称能归到这一格的部门，而且是本省的（以本省名称或本级前缀开头）。

    政府办那一格是门户本身（"××人民政府"）；开发区按地名起名、不带省名，只看能不能归到开发区；
    国家级按部委名核对（"国防科技工业局"不算科技部）；市场监管格挂的知识产权局不算。"""
    name = s.dept_full_name or ""
    if s.province == "国家" and s.dept in _MINISTRY:
        return bool(re.search(_MINISTRY[s.dept], name))
    if s.dept == "市场监管" and "知识产权" in name and "市场监督" not in name:
        return False
    if s.dept == "政府办":
        ok_kind = "人民政府" in name or "办公厅" in name or "政府办" in dept_categories(name)
        if s.level == "市级":
            # 地市门户常写"××市政府门户网站""××政府网""××市政府"，或党政合办"中共××市委
            # ××市人民政府"（只认市级：省级照旧）。下辖区县的政府（"××市××区人民政府"）
            # 不是本市政府办
            ok_kind = (
                ok_kind or bool(re.search(r"政府(?:门户)?网|[市州盟]政府|行政公署", name))
            ) and not re.search(r"(?<!地)[区县旗](?:人民)?政府", name)
            name = name.removeprefix("中共")
    else:
        ok_kind = s.dept in dept_categories(name)
    if not ok_kind:
        return False
    if s.province == "国家" or s.dept == "开发区":
        return True  # 部委、开发区没有省名前缀
    if s.level != "市级" or not s.city:
        return any(name.startswith(v) for v in _own_prefixes(s))
    own = _own_prefixes(s)
    if m := _PROVINCE_FULL.match(name):
        # 以省份全称开头的，后面紧跟本市名称才算本市（"吉林省吉林市……"）；只写省名的是省直部门。
        # 吉林市的"吉林"、青海海南州的"海南"和省名撞上，不先认全称会把"吉林省发展和改革委员会"
        # "海南省……"当成本市的
        if not m.group(0).startswith(s.province):
            return False
        name, own = name[m.end() :], _city_names(s)
    hit = max((v for v in own if name.startswith(v)), key=len, default=None)
    if hit is None:
        return False
    # 本市名称后面紧跟下辖区县全称的是区县的部门（"南京市玄武区……""苏州市常熟市人民政府"）；
    # 只写本级前缀"市"的也看（"市中区……"），自治州下辖的县级市也是（恩施州的"恩施市……"）
    rest = name[len(hit) :]
    return not any(text.startswith(c) for text in (rest, name) for c in counties)


def sample_rows(
    session: Session,
    level: str,
    province: list[str] | None = None,
    size: int | None = None,
    now: datetime | None = None,
    counties: dict[tuple[str, str], frozenset[str]] | None = None,
) -> list[SampleRow]:
    now = now or datetime.now(UTC)
    stmt = select(Source).where(Source.level == level).order_by(Source.province, Source.id)
    if province:
        stmt = stmt.where(Source.province.in_(province))
    picked = _pick(list(session.scalars(stmt)), size)
    rows: list[SampleRow] = []
    for s in picked:
        row = SampleRow(s)
        last = session.execute(
            select(SourceCheck.status, SourceCheck.http_status, SourceCheck.items_found)
            .where(SourceCheck.source_id == s.id)
            .order_by(SourceCheck.started_at.desc(), SourceCheck.id.desc())
            .limit(1)
        ).first()
        if last:
            status, http, items = last
            row.status = f"{status}{f'（HTTP {http}）' if http and status != 'ok' else ''}"
            row.items = items or 0
        row.documents = (
            session.scalar(select(func.count(Document.id)).where(Document.source_id == s.id)) or 0
        )
        docs = session.execute(
            select(Document.title, Document.publish_date, Document.url)
            .where(Document.source_id == s.id)
            .order_by(Document.publish_date.desc(), Document.id.desc())
            .limit(3)
        ).all()
        row.latest = [(t or "", d, u or "") for t, d, u in docs]
        if not _dept_ok(s, (counties or {}).get((s.province, s.city or ""), frozenset())):
            row.failed.append("部门")
        if _kind(s.column_name) not in KINDS:
            row.failed.append("栏目")
        dated = [d for _, d, _ in row.latest if d]
        if row.items < 3 or not dated:
            row.failed.append("条目")
        elif max(dated) < (now - timedelta(days=FRESH_DAYS)).date():
            row.failed.append("新鲜")
        site = registrable(s.url)
        if row.latest and any(registrable(u) != site for _, _, u in row.latest if u):
            row.failed.append("本站")
        row.failed += _content_flags(s, row.latest)
        if s.level in LINE_LEVELS:
            recent = session.scalars(
                select(Document.title)
                .where(Document.source_id == s.id)
                .order_by(Document.publish_date.desc(), Document.id.desc())
                .limit(PERSONAL_RECENT)
            ).all()
            share = personal_share([t or "" for t in recent])
            if share is not None and share >= PERSONAL_SHARE:
                row.failed.append("个人事项")
        rows.append(row)
    _mark_duplicates(rows)
    return rows


def _content_flags(s: Source, latest: list[tuple[str, date | None, str]]) -> list[str]:
    """看最新 3 条的内容像不像这个栏目该有的：两条以上像就记下（标题坏了一条就算）。"""
    titles = [t.strip() for t, _, _ in latest]
    urls = [u for _, _, u in latest]
    flags = []
    if any(_BAD_TITLE.search(t) or _CODE_ONLY.match(t) or len(t) < 6 for t in titles):
        flags.append("标题")
    if sum(bool(_REPORT.search(t)) for t in titles) >= 2:
        flags.append("年报")
    if s.level == "省级":
        own = _own_prefixes(s) - {"省", "市", "区", "自治区"}
        central = sum(
            bool(_CENTRAL.search(t) or ("关于" not in t and _BARE_RULE.search(t)))
            and not any(v in t for v in own)
            for t in titles
        )
        if central >= 2:
            flags.append("上级")
    if sum(bool(_ATTACH.search(u)) for u in urls) >= 2:
        flags.append("附件")
    if any(_VIEWER.search(u) for u in urls):
        flags.append("阅读器")  # 条目链到 PDF 阅读页：抓正文会抓成阅读器的界面文字
    if sum(bool(_NEWS.search(t)) for t in titles) >= 2:
        flags.append("要闻")
    return flags


def _mark_duplicates(rows: list["SampleRow"]) -> None:
    """同一省里同一个列表的，后面那个记"重复"。两种算同一个列表：

    - 一个网址正好是另一个去掉参数的样子（如 list2.shtml 和 list2.shtml?ddtab=true）；
    - 最新几条（至少两条）的网址完全一样。

    参数不同的不一定是同一个列表（安徽的栏目靠 catId 区分），所以不一律去掉参数比。"""
    seen_urls: dict[str, set[str]] = {}
    seen_items: set[tuple[str, tuple[str, ...]]] = set()
    for r in sorted(rows, key=lambda r: r.source.id):
        prov, url = r.source.province, r.source.url.rstrip("/")
        base = url.split("?")[0]
        urls = seen_urls.setdefault(prov, set())
        items = tuple(sorted(u for _, _, u in r.latest if u))
        dup = (base != url and base in urls) or any(u.split("?")[0] == url for u in urls)
        if len(items) >= 2 and (prov, items) in seen_items:
            dup = True
        if dup:
            r.failed.append("重复")
        urls.add(url)
        if len(items) >= 2:
            seen_items.add((prov, items))


def to_csv_row(row: SampleRow) -> list[str]:
    s = row.source

    def item(i: int) -> str:
        if i >= len(row.latest):
            return ""
        title, day, url = row.latest[i]
        # 公开仓库：个人事项或写着人名的标题不写原文
        return f"{day.isoformat() if day else '无日期'} {public_title(title)[:40]} {url}"

    return [
        s.level,
        s.province,
        s.city or "",
        s.district or "",
        s.dept,
        s.dept_full_name or "",
        s.column_name or "",
        _kind(s.column_name) or "",
        s.url,
        str(s.id),
        row.status or "没抓过",
        str(row.items),
        str(row.documents),
        item(0),
        item(1),
        item(2),
        row.verdict,
        "、".join(row.failed),
        "",
        "",
        "",
    ]


def summary_lines(rows: list[SampleRow]) -> list[str]:
    """每省：抽了几个、自动通过几个、待人工几个，以及没过的项各几次。"""
    by_prov: dict[str, list[SampleRow]] = {}
    for r in rows:
        by_prov.setdefault(r.source.province, []).append(r)
    lines = ["省份 | 抽查栏目 | 自动通过 | 待人工 | 没过的项"]
    total = Counter()
    for prov, group in sorted(by_prov.items()):
        ok = sum(1 for r in group if not r.failed)
        why = Counter(f for r in group for f in r.failed)
        lines.append(
            f"{prov} | {len(group)} | {ok} | {len(group) - ok} | "
            + ("、".join(f"{k}×{v}" for k, v in why.most_common()) or "—")
        )
        total.update({"n": len(group), "ok": ok})
    lines.append(f"合计 | {total['n']} | {total['ok']} | {total['n'] - total['ok']} |")
    return lines


# 一省一级算合格：每个栏目都有结论，而且读得准的（通过、不收）占八成以上（待重试、不能抓不算进
# 分母，见 ProvinceResult.rate）。只有通过的栏目进这一批的导入文件；读得准的太少说明这个省有成片的
# 问题，整省先不放。
PASS_RATE = 0.8
# 抽样线（D-129，方案第二节第 3 小节；国家级、省级照旧全检）：联网核对第 1 次每层抽这么多格、不通过
# 不超过这么多格才放。地市一层；区县分两层各自判（县直部门、县政府门户），免得门户好把部门的毛病
# 盖住。没过的修完、全量重跑自动核对，换种子重抽 60 格、不通过不超过 5 格；只看最近一次，不和上一次
# 合并。一层的格比要抽的少时全抽。复核人写"联网核对"（第 1 次）、"联网核对第2次""联网核对第3次"……
SAMPLE_LINES = {("市级", "部门"): (40, 3), ("区县级", "部门"): (40, 3), ("区县级", "门户"): (30, 2)}
REDRAW_LINE = (60, 5)
# 重抽两次（第 3 次）还不过的整省逐格排查，程序不再放。每一次联网核对另记一行在
# 抽查/<层级>/联网核对/<省>.csv（ROUND_FIELDS：层、第几次、种子、日期、凭据），证据表的行重抽时会被
# 盖掉，这份记录只加不改：各次的种子不能重复，重抽要写凭据（改了哪条规则、那类问题清零了）
MAX_ROUNDS = 3
ROUND_FIELDS = ("层", "次数", "种子", "日期", "凭据")
_SEED = re.compile(r"种子\s*[:：=]?\s*([0-9A-Za-z][0-9A-Za-z_.\-]*)")
LINE_LEVELS = frozenset(level for level, _ in SAMPLE_LINES)
_CN_DIGITS = "一二三四五六七八九"
UNKNOWN_ROUND = 999  # 次数认不出：当成超过 MAX_ROUNDS，不放


def _count(text: str) -> int:
    """次数：阿拉伯数字和"二""十一""二十三"这样的中文数字；认不出（含空的、0）是 UNKNOWN_ROUND。"""
    text = text.strip()
    if text.isdigit():
        return int(text) if int(text) > 0 else UNKNOWN_ROUND
    m = re.fullmatch(f"([{_CN_DIGITS}]?)(十?)([{_CN_DIGITS}]?)", text)
    if not m or not text:
        return UNKNOWN_ROUND
    tens, ten, ones = m.groups()
    if not ten:  # 一位数："二"会落在第一组
        digit = tens or ones
        return _CN_DIGITS.index(digit) + 1 if digit and not (tens and ones) else UNKNOWN_ROUND
    return (_CN_DIGITS.index(tens) + 1 if tens else 1) * 10 + (
        _CN_DIGITS.index(ones) + 1 if ones else 0
    )


def _sample_round(who: str) -> int | None:
    """复核人是联网核对的第几次：只写"联网核对"是第 1 次，"联网核对第N次"是第 N 次；后面写了别的、
    次数认不出的是 UNKNOWN_ROUND（不放，不会退回第 1 次）；不是联网核对的行是 None。"""
    who = who.strip()
    if not who.startswith("联网核对"):
        return None
    rest = who.removeprefix("联网核对").strip()
    if not rest:
        return 1
    m = re.fullmatch(r"第\s*(\S+?)\s*次", rest)
    return _count(m.group(1)) if m else UNKNOWN_ROUND


def _layer(level: str, dept: str) -> str:
    return "门户" if level == "区县级" and dept == "政府办" else "部门"


@dataclass
class SampleLine:
    """一层最近一次联网核对：抽到几格有结论（通过、不通过、不收；一格里还有待重试、不能抓、没写结论
    的不算，要补抽）、几格不通过；problem 是种子、次数这类凭据不全的原因。"""

    layer: str
    round: int  # 第几次；0 是还没抽
    judged: int
    failed: int
    need: int
    allow: int
    problem: str = ""

    @property
    def ok(self) -> bool:
        return (
            self.round > 0
            and not self.problem
            and self.judged >= self.need
            and self.failed <= self.allow
        )

    def text(self) -> str:
        if not self.round:
            return f"{self.layer}还没有联网核对（要抽 {self.need} 格）"
        text = (
            f"{self.layer}联网核对第 {self.round} 次：抽 {self.judged} 格（要 {self.need}），"
            f"不通过 {self.failed} 格（最多 {self.allow}）"
        )
        return (
            f"{text}，{self.problem} → 没过"
            if self.problem
            else f"{text} → {'过' if self.ok else '没过'}"
        )


@dataclass
class ProvinceResult:
    province: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    pending: int = 0  # 待人工、还没复核的
    retry: int = 0  # 这次没抓到、下批重试的（不知道读得准不准，不算进通过率）
    blocked: int = 0  # 不能抓：按访问控制等不读的（不进本批，读不了也不算进通过率）
    excluded: int = 0  # 不收：读得对、但不在收录范围的（不进本批；读得对，通过率里算对）
    # 联网核对抽到的那几格（复核人以"联网核对"开头）：读得准的、算进分母的
    sample_ok: int = 0
    sample_judged: int = 0
    level: str = ""
    # 每层有哪些格（城市、区县、部门）；联网核对每层每次抽到的格 → 这一格有没有不通过
    cells: dict[str, set] = field(default_factory=dict)
    # 联网核对每层每次抽到的格 → 这一格的结论（"不通过"、"未完"、"通过"），和证据表里写的种子
    rounds: dict[tuple[str, int], dict] = field(default_factory=dict)
    seeds: dict[tuple[str, int], set] = field(default_factory=dict)
    # 抽查/<层级>/联网核对/<省>.csv 的各行：(层, 第几次) → [(种子, 日期, 凭据)]
    round_log: dict[tuple[str, int], list] = field(default_factory=dict)
    # 正文能读率门（地市、区县；govsites/bodygate.py）：抽了几份、能读几份；还没测是 None
    body: tuple[int, int] | None = None

    @property
    def sample_lines(self) -> list[SampleLine]:
        """有抽样线的层级（地市、区县）每层最近一次联网核对的结果。"""
        out = []
        for layer in sorted(self.cells):
            line = SAMPLE_LINES.get((self.level, layer))
            if line is None:
                continue
            # 最近一次：证据表和联网核对记录里最大的那次（记录里已经记了重抽、证据表还没填，
            # 按重抽算：0 格有结论，不放）
            latest = max(
                (n for lay, n in (*self.rounds, *self.round_log) if lay == layer), default=0
            )
            got = self.rounds.get((layer, latest), {})
            need, allow = line if latest <= 1 else REDRAW_LINE
            need = min(need, len(self.cells[layer]))
            judged = sum(state != "未完" for state in got.values())
            failed = sum(state == "不通过" for state in got.values())
            problem = self._stray_layers() or self._problem(layer, latest)
            out.append(SampleLine(layer, latest, judged, failed, need, allow, problem))
        return out

    def _stray_layers(self) -> str:
        """联网核对记录里写了证据表里没有的层（地市只有"部门"，区县有"部门""门户"）：整省不放，
        免得写错层的重抽被漏看。"""
        stray = sorted({layer for layer, _ in self.round_log} - set(self.cells))
        if not stray:
            return ""
        return f'联网核对记录里的层"{stray[0]}"证据表里没有（地市写部门；区县写部门或门户）'

    def _problem(self, layer: str, latest: int) -> str:
        """最近一次联网核对的凭据（看联网核对记录）：次数没超；第 1 次到这一次每次各记一行、写了
        种子，种子都不一样，重抽写了凭据；证据表里写了种子的，要和记录里这一次的对上。"""
        if latest == UNKNOWN_ROUND:
            return '复核人或联网核对记录里的次数认不出（写"联网核对"或"联网核对第N次"）'
        if latest > MAX_ROUNDS:
            return f"重抽两次还不过，要整省逐格排查（第 {latest} 次不算）"
        used: dict[str, int] = {}
        for n in range(1, latest + 1):
            logged = self.round_log.get((layer, n), [])
            if len(logged) != 1:
                what = "没有" if not logged else "记了几遍"
                where = f"抽查/{self.level}/联网核对/{self.province}.csv"
                return f"联网核对记录里第 {n} 次{what}（{where}）"
            seed, day, proof = logged[0]
            if not seed:
                return f"联网核对记录里第 {n} 次没写种子"
            if not _is_date(day):
                return f"联网核对记录里第 {n} 次的日期不对（写成 2026-11-12 这样）"
            if seed in used:
                return f"第 {n} 次的种子 {seed} 第 {used[seed]} 次用过，重抽要换种子"
            used[seed] = n
            if n > 1 and not proof:
                return f"第 {n} 次重抽没写凭据（改了哪条规则、那类问题清零了）"
            written = self.seeds.get((layer, n), set())
            if written and written != {seed}:
                shown = "、".join(sorted(written))
                return f"证据表里第 {n} 次写的种子（{shown}）和联网核对记录（{seed}）对不上"
        return ""

    @property
    def body_ok(self) -> bool:
        from youju.govsites.bodygate import gate_passed

        return self.body is not None and gate_passed(*self.body)

    def sample_note(self) -> str:
        """地市、区县：抽样线和正文能读率门的结果（国家级、省级是空的）。"""
        if self.level not in LINE_LEVELS:
            return ""
        from youju.govsites.bodygate import gate_text

        body = gate_text(*self.body) if self.body is not None else "正文能读率：还没测"
        return "；".join([*(line.text() for line in self.sample_lines), body])

    @property
    def sample_rate(self) -> float | None:
        """联网核对抽样里读得准的占多少；没有抽样时是 None。"""
        return self.sample_ok / self.sample_judged if self.sample_judged else None

    @property
    def rate(self) -> float:
        """读得准的占多少：通过和不收都算读得准，待重试、不能抓不算进分母。"""
        judged = self.total - self.retry - self.blocked
        return (self.passed + self.excluded) / judged if judged else 0.0

    @property
    def accepted(self) -> bool:
        """全表每行都有结论、读得准的过八成；地市、区县每层最近一次联网核对还要过抽样线
        （SAMPLE_LINES），正文能读率门也要过。国家级、省级是全检：有联网核对抽样的，抽样里也要
        过八成。"""
        if not (self.passed > 0 and self.pending == 0 and self.rate >= PASS_RATE):
            return False
        if self.level in LINE_LEVELS:
            lines = self.sample_lines
            return bool(lines) and all(line.ok for line in lines) and self.body_ok
        return self.sample_rate is None or self.sample_rate >= PASS_RATE


def final_verdict(row: dict) -> str:
    """一行的最终结论：人工复核写了就按人工（自动通过的也可能被复核否掉），没写看自动结论。

    复核结论按开头认（"不通过（部门挂错）"算不通过，首尾空格不算）。"待重试"是这次没抓到：不进
    本批，也不算进通过率（不知道读得准不准）。"不能抓"是按访问控制等原因不读的（列表要靠页面
    脚本另取、要登录……，D-013）：不进本批，也不算进通过率（读不了，谈不上读得准不准）。"不收"
    是读得对、但不在收录范围（如整栏都是事业单位招聘公示）：不进本批；通过率量的是读得准不准，
    所以它算读得对。写了别的词（"有问题"）一律算"待复核"，这个省补上之前不合格。"""
    verdict = (row.get("复核结论") or "").strip()
    if verdict:
        for word in ("不通过", "待重试", "不能抓", "不收", "通过"):
            if verdict.startswith(word):
                return word
        return "待复核"
    return "通过" if (row.get("自动结论") or "").strip() == "自动通过" else "待复核"


def unknown_verdicts(folder) -> list[str]:
    """复核结论写了不认识的词的行（打印出来让人改）。"""
    import csv
    from pathlib import Path

    out = []
    for path in sorted(Path(folder).glob("*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                verdict = (r.get("复核结论") or "").strip()
                if verdict and final_verdict(r) == "待复核":
                    out.append(f'{path.stem} {r.get("栏目URL", "")}：复核结论写的是"{verdict}"')
    return out


def _is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def read_round_log(folder, province: str) -> dict[tuple[str, int], list]:
    """读 <抽查/层级>/联网核对/<省>.csv：(层, 第几次) → [(种子, 日期, 凭据)]（同一次记了几遍就有
    几项；次数空着、认不出的记成 UNKNOWN_ROUND）。"""
    import csv
    from pathlib import Path

    path = Path(folder) / "联网核对" / f"{province}.csv"
    out: dict[tuple[str, int], list] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            layer = (r.get("层") or "").strip() or "部门"
            key = (layer, _count(r.get("次数") or ""))
            out.setdefault(key, []).append(
                tuple((r.get(k) or "").strip() for k in ("种子", "日期", "凭据"))
            )
    return out


def evidence_results(folder) -> list[ProvinceResult]:
    """读 抽查/<层级>/<省>.csv，算每省的结论。"""
    import csv
    from pathlib import Path

    out: list[ProvinceResult] = []
    for path in sorted(Path(folder).glob("*.csv")):
        res = ProvinceResult(path.stem)
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                res.total += 1
                verdict = final_verdict(r)
                res.level = res.level or (r.get("层级") or "").strip()
                layer = _layer(res.level, (r.get("部门") or "").strip())
                cell = tuple((r.get(k) or "").strip() for k in ("城市", "区县", "部门"))
                res.cells.setdefault(layer, set()).add(cell)
                n_round = _sample_round(r.get("复核人") or "")
                if n_round is not None:
                    # 地市、区县的抽样只认人工写的复核结论：空着的不会退回自动结论。国家级、省级照旧
                    # （全检，抽样过八成那条看最终结论）
                    manual = (r.get("复核结论") or "").strip() or res.level not in LINE_LEVELS
                    review = verdict if manual else ""
                    if review in ("通过", "不通过", "不收"):
                        res.sample_judged += 1
                        res.sample_ok += review != "不通过"
                    # 按格算：一格里有一行不通过就不通过；还有待重试、不能抓、没写结论的行就"未完"
                    # （不算样本，要补抽）；都通过或不收才算通过。只要写了这一次，就算有这一次
                    key = (layer, n_round)
                    state = (
                        "不通过"
                        if review == "不通过"
                        else ("通过" if review in ("通过", "不收") else "未完")
                    )
                    got = res.rounds.setdefault(key, {})
                    order = ("通过", "未完", "不通过")
                    got[cell] = max(got.get(cell, "通过"), state, key=order.index)
                    found = _SEED.findall(r.get("复核备注") or "")
                    res.seeds.setdefault(key, set()).update(found)
                if verdict == "通过":
                    res.passed += 1
                elif verdict == "不通过":
                    res.failed += 1
                elif verdict == "待重试":
                    res.retry += 1
                elif verdict == "不收":
                    res.excluded += 1
                elif verdict == "不能抓":
                    res.blocked += 1
                else:
                    res.pending += 1
        if res.level in LINE_LEVELS:
            from youju.govsites.bodygate import read_gate

            res.body = read_gate(path.parent, res.province)
            res.round_log = read_round_log(path.parent, res.province)
        out.append(res)
    return out
