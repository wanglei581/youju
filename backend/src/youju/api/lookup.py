"""Public, snapshot-consistent list lookup. Legacy /lists/lookup remains unchanged."""

import logging
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.crawler.detail import redact_sensitive
from youju.db import get_session
from youju.lists.lookup import lookup_name_candidates
from youju.lists.pagination import (
    LookupInputError,
    PageKey,
    _entries,
    lookup_identities,
    lookup_page,
    parse_identity,
    parse_query,
)
from youju.lists.revision import LookupChanged, decode_cursor, encode_cursor, snapshot
from youju.lists.rules import is_person_query, normalize_name, valid_org_name
from youju.models import Document, Source

log = logging.getLogger(__name__)


class LookupRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store(request):
            try:
                response = await handler(request)
            except LookupChanged as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=409)
            except LookupInputError as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=422)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )
            except RequestValidationError:
                response = JSONResponse({"detail": "查询参数有误，请重新查询"}, status_code=422)
            except Exception as exc:
                # 不把查询企业、SQL 参数或密钥写到错误响应/日志。
                log.error("名单分页读取失败：%s", type(exc).__name__)
                response = JSONResponse({"detail": "查询暂时不可用，请稍后重试"}, status_code=503)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


router = APIRouter(route_class=LookupRoute)


def lookup_session(session: Annotated[Session, Depends(get_session)]):
    if session.in_transaction():
        raise RuntimeError("名单快照必须在首次数据库访问前初始化")
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    return session


SessionDep = Annotated[Session, Depends(lookup_session)]
QueryText = Annotated[str, Query(min_length=2, max_length=100)]
PageSize = Annotated[int, Query(ge=1, le=50)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=2048)]
Revision = Annotated[str | None, Query(pattern=r"^[1-9][0-9]*-[a-f0-9]{16}-[1-9][0-9]*$")]


def _coverage(session):
    entries = _entries().subquery()
    count, since = session.execute(
        select(func.count(func.distinct(entries.c.master_id)), func.min(entries.c.publish_date))
    ).one()
    return {
        "lists": count,
        "since": since.isoformat() if since else None,
        "note": "仅查询已收录并解析的公开名单；查不到不代表企业未入选。",
    }


def _identity(identity, query):
    return {
        "key": identity.key,
        "kind": identity.kind,
        "name": query,
        "creditCode": identity.value if identity.kind == "code" else None,
        "verified": identity.kind == "code",
    }


def _limit(request):
    from youju.api.v1 import _limit_lookup

    _limit_lookup(request)


@router.get("/lists/lookup/identities")
def identities(
    request: Request,
    session: SessionDep,
    q: QueryText,
    size: PageSize = 20,
    cursor: Cursor = None,
    revision: Revision = None,
):
    _limit(request)
    query = normalize_name(q)
    state = snapshot(session)  # first consistent SELECT establishes the RR snapshot
    try:
        query = parse_query(q).name
    except LookupInputError:
        # 简称/姓名形状只提供企业全称建议，不查询个人，也不把建议当成完整命中。
        if cursor is not None or not is_person_query(query) or redact_sensitive(query) != query:
            raise
        decode_cursor(state, query, None, cursor, revision)
        candidates = [
            name for name in lookup_name_candidates(session, query) if valid_org_name(name)
        ]
        if not candidates:
            raise
        return {
            "query": query,
            "revision": state.revision,
            "identityState": "none",
            "identities": [],
            "candidates": candidates,
            "nextCursor": None,
            "coverage": _coverage(session),
        }
    # 代码归一化（大小写）在游标绑定之前完成；首屏/续屏使用同一规范查询。
    after = decode_cursor(state, query, None, cursor, revision)
    page = lookup_identities(session, query, size=size, after=after)
    return {
        "query": query,
        "revision": state.revision,
        "identityState": "multiple"
        if page.next_after is not None or cursor is not None or len(page.identities) > 1
        else "single"
        if page.identities
        else "none",
        "identities": [_identity(identity, query) for identity in page.identities],
        "candidates": [],
        "nextCursor": encode_cursor(state, query, None, page.next_after)
        if page.next_after is not None
        else None,
        "coverage": _coverage(session),
    }


def _cards(session, matches, query):
    from youju.api import v1
    from youju.categories import shown_category
    from youju.classify import effective_kind, list_status, region_label
    from youju.crawler.urls import public_url
    from youju.issuer import normalize_issuer

    # 只取本页 canonical 题录；绝不调用 _items/_details 加载全组正文。
    rows = (
        session.execute(
            select(
                Document.id,
                Document.title,
                Document.publish_date,
                Document.url,
                Document.kind_override,
                Document.rule_category,
                Document.link_gone_at,
                Source.level,
                Source.province,
                Source.city,
                Source.district,
                Source.dept_full_name,
            )
            .join(Source, Source.id == Document.source_id)
            .where(Document.id.in_([m.master_id for m in matches]))
        ).all()
        if matches
        else []
    )
    documents = {r.id: r for r in rows}
    items = []
    for match in matches:
        doc = documents[match.master_id]
        kind = effective_kind(doc.title, doc.kind_override)
        matched = {
            "name": match.name,
            "category": match.category,
            "batch": match.batch,
            "sequence": match.sequence,
            "creditCode": match.credit_code,
            "region": match.region,
            "via": "name" if match.normalized_name == query else "creditCode",
            "entryId": str(match.entry_id),
            "sourceDocumentId": str(match.document_id),
        }
        matched = {k: redact_sensitive(v) if isinstance(v, str) else v for k, v in matched.items()}
        items.append(
            {
                "id": str(doc.id),
                "title": redact_sensitive(doc.title),
                "kind": kind,
                "category": shown_category(kind, doc.rule_category, doc.title),
                "status": list_status(doc.title, kind),
                "region": region_label(doc.province, doc.level, doc.city, doc.district),
                "province": doc.province,
                "city": doc.city,
                "district": doc.district,
                "level": doc.level,
                "issuer": redact_sensitive(normalize_issuer(doc.dept_full_name)),
                "date": doc.publish_date.isoformat() if doc.publish_date else None,
                "url": public_url(doc.url),
                "link_gone": v1.link_gone_date(doc.link_gone_at),
                "deadline": None,
                "deadline_state": "none",
                "ai": None,
                "matched": matched,
            }
        )
    return items


@router.get("/lists/lookup/page")
def page(
    request: Request,
    session: SessionDep,
    q: QueryText,
    identity: Annotated[str, Query(min_length=1, max_length=256)],
    size: PageSize = 20,
    cursor: Cursor = None,
    revision: Revision = None,
):
    _limit(request)
    query = parse_query(q).name
    selected = parse_identity(identity)
    state = snapshot(session)
    raw_after = decode_cursor(state, query, selected.key, cursor, revision)
    after = None
    if raw_after is not None:
        try:
            if not isinstance(raw_after, list) or len(raw_after) != 2:
                raise ValueError
            after = PageKey(
                date.fromisoformat(raw_after[0]) if raw_after[0] is not None else None, raw_after[1]
            )
        except (ValueError, TypeError) as exc:
            raise LookupInputError("名单翻页位置无效") from exc
    result = lookup_page(session, query, selected, size=size, after=after)
    next_cursor = None
    if result.next_after is not None:
        key = result.next_after
        next_cursor = encode_cursor(
            state,
            query,
            selected.key,
            [key.publish_date.isoformat() if key.publish_date else None, key.master_id],
        )
    return {
        "query": query,
        "revision": state.revision,
        "identityState": "selected",
        "identity": _identity(selected, query),
        "items": _cards(session, result.matches, query),
        "nextCursor": next_cursor,
        "coverage": _coverage(session),
    }
