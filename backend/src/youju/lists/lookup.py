"""名单精确匹配的共享查询核心。

这里不组装 API 返回格式，只负责公开口径、名称/信用代码精确匹配、别名回溯和通知归并。
调用方可以用返回的轻量条目组装自己的展示结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from youju.dedup import canonical_ids
from youju.lists.rules import normalize_name, valid_org_name
from youju.models import Document, DocumentMerge, ListEntry, Source
from youju.takedown import available_condition


@dataclass(frozen=True)
class ListLookupMatch:
    """一份归并后的名单及其最适合展示的匹配条目。"""

    master_id: int
    document_id: int
    publish_date: date | None
    entry_id: int
    name: str
    normalized_name: str
    credit_code: str | None
    category: str
    batch: str | None
    sequence: str | None
    region: str | None


@dataclass(frozen=True)
class ListLookupResult:
    query: str
    matches: tuple[ListLookupMatch, ...]
    ambiguous: bool
    truncated: bool

    @property
    def master_ids(self) -> list[int]:
        return [match.master_id for match in self.matches]


def public_condition(document=Document, source=Source):
    """可以公开给小程序的文件口径。

    调用方的查询必须已经连接 ``Source``；人工定为“不是名单”的文件在各自查询中另外排除。
    """

    twin = aliased(Document)
    twin_source = aliased(Source)
    level1_twin = (
        select(DocumentMerge.duplicate_id)
        .join(twin, twin.id == DocumentMerge.duplicate_id)
        .join(twin_source, twin_source.id == twin.source_id)
        .where(
            DocumentMerge.canonical_id == document.id,
            DocumentMerge.active.is_(True),
            twin_source.rights_level == 1,
        )
        .exists()
    )
    return and_(available_condition(document), or_(source.rights_level == 1, level1_twin))


def _public_entries(stmt: Select) -> Select:
    """给名单条目查询加公开来源和人工类型口径。"""

    return (
        stmt.join(Document, Document.id == ListEntry.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            public_condition(),
            or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
        )
    )


def lookup_list_matches(
    session: Session,
    value: str,
    *,
    scan_limit: int | None = 2000,
    result_limit: int | None = 100,
) -> ListLookupResult:
    """按查名单接口的口径返回精确匹配。

    ``scan_limit`` 和 ``result_limit`` 保留接口原有的防止极端结果拖慢请求的限制；名单比对传入
    ``None``，逐行取得该企业全部归并后的通知。查询只选择索引字段和展示条目字段，不加载 ORM
    关联整行。
    """

    query = normalize_name(value)
    code = query.upper()
    direct = or_(ListEntry.normalized_name == query, ListEntry.credit_code == code)

    # 只有名称直匹配出来的多个代码才算“多个同名”；信用代码查询本身不会触发该提示。
    name_codes = set(
        session.scalars(
            _public_entries(select(ListEntry.credit_code).distinct()).where(
                ListEntry.normalized_name == query,
                ListEntry.credit_code.is_not(None),
            )
        )
    )
    codes = set(
        session.scalars(
            _public_entries(select(ListEntry.credit_code).distinct())
            .where(direct, ListEntry.credit_code.is_not(None))
            .limit(20)
        )
    )
    matched = or_(direct, ListEntry.credit_code.in_(codes)) if codes else direct

    found_stmt = _public_entries(
        select(ListEntry.document_id, Document.publish_date).distinct()
    ).where(matched)
    if scan_limit is not None:
        found = session.execute(
            found_stmt.order_by(Document.publish_date.desc(), ListEntry.document_id.desc()).limit(
                scan_limit + 1
            )
        ).all()
        scanned_all = len(found) <= scan_limit
        found = found[:scan_limit]
    else:
        found = session.execute(
            found_stmt.order_by(Document.publish_date.desc(), ListEntry.document_id.desc())
        ).all()
        scanned_all = True

    mapping = canonical_ids(session, [doc_id for doc_id, _ in found])
    newest: dict[int, date | None] = {}
    for doc_id, published in found:
        master = mapping.get(doc_id, doc_id)
        if master not in newest or (published and (newest[master] or date.min) < published):
            newest[master] = published
    masters = sorted(
        newest,
        key=lambda master: (newest[master] is not None, newest[master] or date.min, master),
        reverse=True,
    )
    truncated = (
        len(masters) > (result_limit if result_limit is not None else len(masters))
        or not scanned_all
    )
    if result_limit is not None:
        masters = masters[:result_limit]
    keep = set(masters)
    if not keep:
        return ListLookupResult(query, (), len(name_codes) > 1, truncated)

    # 只读取组装 matched 字段需要的列；按条目 id 保持旧接口的稳定优先顺序。
    members = [doc_id for doc_id, _ in found if mapping.get(doc_id, doc_id) in keep]
    rows = session.execute(
        select(
            ListEntry.id,
            ListEntry.document_id,
            Document.publish_date,
            ListEntry.name,
            ListEntry.normalized_name,
            ListEntry.credit_code,
            ListEntry.category,
            ListEntry.batch,
            ListEntry.sequence,
            ListEntry.region,
        )
        .join(Document, Document.id == ListEntry.document_id)
        .where(ListEntry.document_id.in_(members), matched)
        .order_by(ListEntry.id)
    ).all()
    best: dict[int, ListLookupMatch] = {}
    for (
        entry_id,
        document_id,
        publish_date,
        name,
        normalized_name,
        credit_code,
        category,
        batch,
        sequence,
        region,
    ) in rows:
        master = mapping.get(document_id, document_id)
        if master not in keep:
            continue
        current = best.get(master)
        if current is not None and not (
            normalized_name == query and current.normalized_name != query
        ):
            continue
        best[master] = ListLookupMatch(
            master_id=master,
            document_id=document_id,
            publish_date=publish_date,
            entry_id=entry_id,
            name=name,
            normalized_name=normalized_name,
            credit_code=credit_code,
            category=category,
            batch=batch,
            sequence=sequence,
            region=region,
        )

    ordered = tuple(best[master] for master in masters if master in best)
    return ListLookupResult(query, ordered, len(name_codes) > 1, truncated)


# 简称候选：数据库里按名称去重、排好序后最多取这么多行，再逐个做企业/机构校验
CANDIDATE_ROWS = 200


def lookup_name_candidates(session: Session, value: str, *, limit: int = 20) -> list[str]:
    """简称候选：公开名单条目里名称包含这个词的企业和机构全称。

    去重后以这个词开头的排前面，再按名称长度、字典序排，最多 20 个。
    查全部名单条目，不只看最近的（10/9 本机 20 万条实测：
    "海尔"查全部有 27 个候选，只查最近 2 万条只有 3 个）。两头带通配符的 LIKE 用不上索引，
    要把名称列扫一遍（本机约 0.2 秒），所以去重、排序、取前 CANDIDATE_ROWS 行都在数据库里做，
    不把命中的行全取回来；接口另有限流。条目入库时已校验是企业或机构，这里再校验一次，
    防遗留的个人条目。LIKE 模式参数化，通配符和转义符先转义。
    """
    query = normalize_name(value)
    if len(query) < 2:
        return []
    escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    rows = session.execute(
        _public_entries(
            select(
                ListEntry.normalized_name,
                func.min(ListEntry.name),
                func.min(ListEntry.credit_code),
            )
        )
        .where(ListEntry.normalized_name.like(f"%{escaped}%", escape="!"))
        .group_by(ListEntry.normalized_name)
        .order_by(
            ListEntry.normalized_name.like(f"{escaped}%", escape="!").desc(),
            func.char_length(ListEntry.normalized_name),
            ListEntry.normalized_name,
        )
        .limit(CANDIDATE_ROWS)
    )
    # 以这个词开头的排前面（"海尔智家"先于"上海尔控科技"），再按名称长度、字典序
    names = [
        (not normalized.startswith(query), len(name), name)
        for normalized, name, code in rows
        if valid_org_name(name, code)
    ]
    return [name for *_, name in sorted(names)][:limit]
