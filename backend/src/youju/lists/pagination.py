"""名单身份与有界 keyset 查询核心；尚未接公开 API。

跨请求的 revision 校验和单请求一致性事务由后续接口层提供。本模块不把普通 Session 当成快照，
也不沿用旧接口的 2000/100 截断。名单条目的企业校验仍由入库规则负责。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import Select, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from youju.crawler.detail import _ID_CARD, _MOBILE, redact_sensitive
from youju.lists.lookup import ListLookupMatch, public_condition
from youju.lists.rules import (
    _CREDIT_ALPHABET,
    _CREDIT_WEIGHTS,
    is_person_query,
    normalize_credit_code,
    normalize_name,
    valid_org_name,
)
from youju.models import Document, DocumentMerge, ListEntry, Source

LOOKUP_VERSION = 1
MAX_PAGE_SIZE = 50


class LookupInputError(ValueError):
    pass


@dataclass(frozen=True)
class LookupQuery:
    name: str
    code: str | None

    @property
    def requires_code(self) -> bool:
        # 无机构后缀的机关等名称只能由真实入库的有效代码佐证，不能退回无代码集合。
        return not valid_org_name(self.name)


@dataclass(frozen=True)
class LookupIdentity:
    kind: Literal["code", "unverified"]
    value: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.value}"


@dataclass(frozen=True)
class IdentityPage:
    identities: tuple[LookupIdentity, ...]
    # 数据库中的代码排序键；空串代表无代码集合，不是一个企业代码。
    next_after: str | None


@dataclass(frozen=True)
class PageKey:
    publish_date: date | None
    master_id: int


@dataclass(frozen=True)
class LookupPage:
    # publish_date 是 canonical 的发布日期；document_id 仍为匹配条目的真实来源。
    matches: tuple[ListLookupMatch, ...]
    next_after: PageKey | None


def parse_query(value: str) -> LookupQuery:
    query = normalize_name(value)
    code = normalize_credit_code(query)
    if code:
        return LookupQuery(code, code)
    if re.fullmatch(r"[A-Za-z0-9]{18}", query):
        raise LookupInputError("统一社会信用代码格式或校验位不正确")
    # 入库时 valid_org_name(name, code) 还接受带有效代码的机关名称（如“管理局”）。
    # 查询参数不再强制机构后缀；是否存在可选身份由公开名单中的精确 SQL 匹配决定。
    if (
        not 3 <= len(query) <= 100
        or query.isdecimal()
        or is_person_query(query)
        or redact_sensitive(query) != query
        or any(c in query for c in "<>@；;：:，。、\n")
    ):
        raise LookupInputError("只查企业和机构，请输入企业全称或有效统一社会信用代码")
    return LookupQuery(query, None)


def parse_identity(key: str) -> LookupIdentity:
    kind, _, value = key.partition(":")
    if kind == "code":
        code = normalize_credit_code(value)
        if code and value == code:
            return LookupIdentity("code", code)
    elif kind == "unverified":
        query = parse_query(value)
        if query.code is None and query.name == value and valid_org_name(value):
            return LookupIdentity("unverified", value)
    raise LookupInputError("查询身份无效，请重新选择")


def _size(size: int) -> None:
    if type(size) is not int or not 1 <= size <= MAX_PAGE_SIZE:
        raise LookupInputError("每页条数必须在1到50之间")


def _entries() -> Select:
    """与旧 _public_entries + API _base 相同的来源、人工类型和主文可见性口径。"""
    merge = aliased(DocumentMerge)
    master = aliased(Document)
    master_source = aliased(Source)
    hidden_master = aliased(DocumentMerge)
    return (
        select(
            ListEntry.id.label("entry_id"),
            ListEntry.normalized_name.label("normalized_name"),
            ListEntry.credit_code.label("credit_code"),
            master.id.label("master_id"),
            master.publish_date.label("publish_date"),
        )
        .select_from(ListEntry)
        .join(Document, Document.id == ListEntry.document_id)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(merge, and_(merge.duplicate_id == Document.id, merge.active.is_(True)))
        .join(master, master.id == func.coalesce(merge.canonical_id, Document.id))
        .join(master_source, master_source.id == master.source_id)
        .where(
            public_condition(),
            or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
            public_condition(master, master_source),
            ~select(hidden_master.duplicate_id)
            .where(hidden_master.duplicate_id == master.id, hidden_master.active.is_(True))
            .exists(),
        )
    )


def _valid_code_condition(code):
    # 与入库 normalize_credit_code 共用字符表和权重。SQL 先排除历史脏代码，既不把它们作为
    # 企业身份，也不把可能含个人号码的脏值放进身份翻页位置。
    alphabet = literal(_CREDIT_ALPHABET)
    weighted = sum(
        (func.instr(alphabet, func.substring(code, n, 1)) - 1) * weight
        for n, weight in enumerate(_CREDIT_WEIGHTS, 1)
    )
    return and_(
        func.char_length(code) == 18,
        code.regexp_match(f"^[{_CREDIT_ALPHABET}]{{18}}$", flags="c"),
        # 即便校验位碰巧正确，也不能把身份证/手机号形态当企业代码。其余脱敏模式含
        # 汉字、标点或连字符，不会命中上面的纯代码字符集合。
        ~code.regexp_match(_ID_CARD.pattern, flags="c"),
        ~code.regexp_match(_MOBILE.pattern, flags="c"),
        func.substring(code, 18, 1)
        == func.substring(alphabet, func.mod(31 - func.mod(weighted, 31), 31) + 1, 1),
    )


def _identity_statement(query: LookupQuery, size: int, after: str | None) -> Select:
    entries = (
        _entries()
        .where(
            ListEntry.credit_code == query.code
            if query.code
            else ListEntry.normalized_name == query.name
        )
        .where(
            or_(
                and_(
                    not query.requires_code,
                    or_(ListEntry.credit_code.is_(None), ListEntry.credit_code == ""),
                ),
                _valid_code_condition(ListEntry.credit_code),
            )
        )
        .subquery()
    )
    code = func.coalesce(entries.c.credit_code, "")
    stmt = select(code.label("identity_code")).distinct().order_by(code).limit(size + 1)
    if after is not None:
        stmt = stmt.where(code > after)
    return stmt


def lookup_identities(
    session: Session, value: str, *, size: int = 20, after: str | None = None
) -> IdentityPage:
    _size(size)
    query = parse_query(value)
    if after is not None and (len(after) > 18 or not re.fullmatch(r"[A-Z0-9]*", after)):
        raise LookupInputError("身份翻页位置无效")
    codes = list(session.scalars(_identity_statement(query, size, after)))
    identities = []
    for code in codes[:size]:
        if not code:
            identities.append(LookupIdentity("unverified", query.name))
        elif normalize_credit_code(code) == code:
            identities.append(LookupIdentity("code", code))
    return IdentityPage(tuple(identities), codes[size - 1] if len(codes) > size else None)


def _selected_entries(query: LookupQuery, identity: LookupIdentity) -> Select:
    if identity.kind == "code":
        condition = ListEntry.credit_code == identity.value
    else:
        condition = and_(
            ListEntry.normalized_name == query.name,
            or_(ListEntry.credit_code.is_(None), ListEntry.credit_code == ""),
        )
    return _entries().where(condition)


def _masters_statement(entries, size: int, after: PageKey | None) -> Select:
    day, master_id = entries.c.publish_date, entries.c.master_id
    stmt = select(master_id, day).group_by(master_id, day)
    if after is not None:
        if after.publish_date is None:
            condition = and_(day.is_(None), master_id < after.master_id)
        else:
            condition = or_(
                day < after.publish_date,
                and_(day == after.publish_date, master_id < after.master_id),
                day.is_(None),
            )
        stmt = stmt.where(condition)
    return stmt.order_by(day.desc(), master_id.desc()).limit(size + 1)


def _representatives_statement(entries, query: LookupQuery, masters: list[int]) -> Select:
    # 同一身份同一名单可能有多个附件/转载条目。SQL 选出代表，不把组内所有行传到 Python。
    return (
        select(
            entries.c.master_id,
            entries.c.publish_date,
            func.coalesce(
                func.min(case((entries.c.normalized_name == query.name, entries.c.entry_id))),
                func.min(entries.c.entry_id),
            ).label("entry_id"),
        )
        .where(entries.c.master_id.in_(masters))
        .group_by(entries.c.master_id, entries.c.publish_date)
    )


def lookup_page(
    session: Session,
    value: str,
    identity: LookupIdentity,
    *,
    size: int = 20,
    after: PageKey | None = None,
) -> LookupPage:
    """返回选定身份的下一页；调用者必须在一致性事务内读 revision、调用本函数并组装响应。"""
    _size(size)
    query = parse_query(value)
    identity = parse_identity(identity.key)
    if after is not None and (
        type(after.master_id) is not int
        or after.master_id < 1
        or (after.publish_date is not None and type(after.publish_date) is not date)
    ):
        raise LookupInputError("名单翻页位置无效")
    if (query.code and identity != LookupIdentity("code", query.code)) or (
        identity.kind == "unverified" and identity.value != query.name
    ):
        raise LookupInputError("所选身份与查询不符")
    selected = _selected_entries(query, identity)
    if not query.code and identity.kind == "code":
        # 用户不能把甲企业名称与不属于这个名称的乙企业代码拼在一起。
        belongs = selected.where(ListEntry.normalized_name == query.name).exists()
        if not session.scalar(select(belongs)):
            raise LookupInputError("所选身份与查询不符")
    entries = selected.subquery()
    masters = session.execute(_masters_statement(entries, size, after)).all()
    current = masters[:size]
    if not current:
        return LookupPage((), None)
    representatives = _representatives_statement(
        entries, query, [master_id for master_id, _ in current]
    ).subquery()
    rows = session.execute(
        select(
            representatives.c.master_id,
            ListEntry.document_id,
            representatives.c.publish_date,
            ListEntry.id,
            ListEntry.name,
            ListEntry.normalized_name,
            ListEntry.credit_code,
            ListEntry.category,
            ListEntry.batch,
            ListEntry.sequence,
            ListEntry.region,
        ).join(ListEntry, ListEntry.id == representatives.c.entry_id)
    ).all()
    by_master = {row[0]: ListLookupMatch(*row) for row in rows}
    # 如果调用方未提供一致性事务，不能将缺失代表行包装为一份完整结果。
    if set(by_master) != {master_id for master_id, _ in current}:
        raise RuntimeError("名单结果读取期间发生变化，请使用一致性事务重新查询")
    last_id, last_day = current[-1]
    return LookupPage(
        tuple(by_master[master_id] for master_id, _ in current),
        PageKey(last_day, last_id) if len(masters) > size else None,
    )
