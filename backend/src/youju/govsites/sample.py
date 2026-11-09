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

都过记"自动通过"，否则记"待人工"并写明哪项没过；最后三列（复核人、复核结论、复核备注）
留给人工复核。复核结论写"通过""不通过""待重试"（这次没抓到）或"不收"（读得对、但不在收录范围，
如整栏是招聘公示），后面可以加括号说明（见 final_verdict）。只读库、不联网。
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.govsites.divisions import name_variants
from youju.govsites.links import registrable
from youju.govsites.names import column_kind, dept_categories
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


def _kind(name: str | None) -> str | None:
    hit = column_kind(name or "")
    return hit[0] if hit else None


# 省级部门全称常只写本级前缀（"省农业农村厅""区商务厅""自治区人社厅""市发展改革委"）
_MUNICIPALITIES = ("北京", "天津", "上海", "重庆")
_REGIONS = ("内蒙古", "广西", "西藏", "宁夏", "新疆")


def _own_prefixes(s: Source) -> set[str]:
    own = {v for v in name_variants(s.province) | {s.province} if len(v) >= 2}
    if s.level == "省级":
        if s.province in _MUNICIPALITIES:
            own.add("市")
        elif s.province in _REGIONS:
            own |= {"自治区", "区"}
        else:
            own.add("省")
    return own


def _dept_ok(s: Source) -> bool:
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
    else:
        ok_kind = s.dept in dept_categories(name)
    if not ok_kind:
        return False
    if s.province == "国家" or s.dept == "开发区":
        return True  # 部委、开发区没有省名前缀
    return any(name.startswith(v) for v in _own_prefixes(s))


def sample_rows(
    session: Session,
    level: str,
    province: list[str] | None = None,
    size: int | None = None,
    now: datetime | None = None,
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
        if not _dept_ok(s):
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
        return f"{day.isoformat() if day else '无日期'} {title[:40]} {url}"

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


# 一省一级算合格：每个栏目都有结论（自动通过，或人工复核通过、不通过），而且通过的占八成以上。
# 不通过的栏目不进这一批的导入文件（不会被启用）；通过率太低说明这个省有成片的问题，整省先不放。
PASS_RATE = 0.8


@dataclass
class ProvinceResult:
    province: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    pending: int = 0  # 待人工、还没复核的
    retry: int = 0  # 这次没抓到、下批重试的（不算进通过率）
    excluded: int = 0  # 不收：读得对、但不在收录范围的（不算进通过率）

    @property
    def rate(self) -> float:
        judged = self.total - self.retry - self.excluded
        return self.passed / judged if judged else 0.0

    @property
    def accepted(self) -> bool:
        return self.passed > 0 and self.pending == 0 and self.rate >= PASS_RATE


def final_verdict(row: dict) -> str:
    """一行的最终结论：人工复核写了就按人工（自动通过的也可能被复核否掉），没写看自动结论。

    复核结论按开头认（"不通过（部门挂错）"算不通过，首尾空格不算）；"待重试"是这次没抓到，
    "不收"是读得对、但不在收录范围（如整栏都是事业单位招聘公示）：这两种都不进本批，也不算进
    通过率（通过率量的是读得准不准）；写了别的词（"有问题"）一律算"待复核"，这个省补上之前不合格。"""
    verdict = (row.get("复核结论") or "").strip()
    if verdict:
        for word in ("不通过", "待重试", "不收", "通过"):
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
                if verdict == "通过":
                    res.passed += 1
                elif verdict == "不通过":
                    res.failed += 1
                elif verdict == "待重试":
                    res.retry += 1
                elif verdict == "不收":
                    res.excluded += 1
                else:
                    res.pending += 1
        out.append(res)
    return out
