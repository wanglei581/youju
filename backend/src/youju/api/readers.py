"""小程序原生阅读：只读已脱敏文字，每页重新核对公开范围和内容版本。"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju import attachment_text as texts
from youju import attachments
from youju.api import attachments as download_api
from youju.api import v1
from youju.classify import effective_kind
from youju.db import get_session
from youju.dedup import canonical_ids, group_ids
from youju.models import (
    AttachmentTextSegment,
    AttachmentTextUrl,
    AttachmentTextVersion,
    Document,
    DocumentAttachment,
    Source,
)
from youju.takedown import available_condition, is_taken_down

PAGE_SEGMENTS = 20
MAX_PAGE_BYTES = 128 * 1024
READER_VERSION = 1


class NoStoreRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store(request: Request):
            try:
                response = await handler(request)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except RequestValidationError:
                response = JSONResponse(
                    {"detail": "阅读参数有误，请返回后重新打开"}, status_code=422
                )
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


router = APIRouter(prefix="/api/v1", tags=["小程序"], route_class=NoStoreRoute)
Cursor = Annotated[str, Query(pattern=r"^(0|[1-9][0-9]{0,8})$")]
Revision = Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")]


def _reading_session(session: Annotated[Session, Depends(get_session)]) -> Session:
    # 默认连接是 RC；重提取会原地替换同 version.id 的段落。必须在规则准备的
    # 第一次 SELECT 前建立快照，避免旧 revision 配上新段落或混用来源资格。
    if session.in_transaction():
        raise RuntimeError("阅读快照必须在首次数据库访问前初始化")
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    # execution_options 随连接关闭恢复池的默认隔离，不改变其他写入接口。
    return v1._request_session(session)


ReadingSessionDep = Annotated[Session, Depends(_reading_session)]


def _revision(*parts) -> str:
    return hashlib.sha256(
        json.dumps([READER_VERSION, *parts], ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _check_revision(cursor: str, expected: str | None, current: str) -> int:
    if cursor != "0" and expected is None:
        raise HTTPException(422, "继续阅读需要内容版本，请重新打开")
    if expected is not None and expected != current:
        raise HTTPException(409, "内容已更新，请重新载入")
    return int(cursor)


def _source(doc: Document, src: Source) -> dict:
    return {
        "id": str(doc.id),
        "title": texts.mask(doc.title)[:1000],
        "issuer": texts.mask(src.dept_full_name)[:500],
        "date": doc.publish_date.isoformat() if doc.publish_date else None,
        "host": urlsplit(doc.url).hostname or "",
    }


def _payload(title: str, source: dict, status="ok", notice="") -> dict:
    return {
        "status": status,
        "title": texts.mask(title)[:1000],
        "source": source,
        "revision": None,
        "segments": [],
        "nextCursor": None,
        "totalSegments": 0,
        "notice": notice,
        "scannedPages": 0,
    }


def _document(session, doc_id: int, request: Request):
    if not download_api._allow(download_api._caller(request), time.monotonic()):
        raise HTTPException(429, "阅读太频繁，请稍后再试")
    if is_taken_down(session, doc_id):
        raise HTTPException(404, "该文件已下架")
    canonical = canonical_ids(session, [doc_id]).get(doc_id, doc_id)
    row = session.execute(v1._base().where(Document.id == canonical)).first()
    if row is None:
        raise HTTPException(404, "没有找到这份文件")
    return row


def _page(payload: dict, candidates: list[dict], after: int, last: int) -> dict:
    for segment in candidates:
        payload["segments"].append(segment)
        payload["nextCursor"] = str(segment["n"]) if segment["n"] < last else None
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PAGE_BYTES:
            payload["segments"].pop()
            if not payload["segments"]:
                payload.update(
                    status="unavailable",
                    nextCursor=None,
                    notice="这一段内容较大，暂时无法展示，请反馈问题。",
                )
                return payload
            payload["nextCursor"] = str(payload["segments"][-1]["n"])
            break
    if not candidates and after:
        raise HTTPException(422, "阅读位置超出范围，请重新打开")
    return payload


@router.get("/documents/{doc_id}/reading")
def document_reading(
    doc_id: int,
    session: ReadingSessionDep,
    request: Request,
    cursor: Cursor = "0",
    revision: Revision = None,
) -> dict:
    doc, src = _document(session, doc_id, request)
    payload = _payload(doc.title, _source(doc, src))
    kind = effective_kind(doc.title, doc.kind_override)
    # 名单类通知：正文挡掉名单内容后给读（D-092）；失信、个人类名单不给（list_filter 为 None）
    list_filter = v1.list_body_filter(session, doc) if kind == "名单" else None
    if kind == "名单" and list_filter is None:
        payload.update(status="unsupported", notice="这类名单可能包含个人信息，站内不展示全文。")
        return payload
    if kind not in v1.BODY_KINDS and list_filter is None:
        payload.update(
            status="unsupported",
            notice="这类文件可能包含个人信息，站内不展示全文。",
        )
        return payload
    if doc.link_gone_at:
        payload.update(status="unavailable", notice="来源已失效，内容暂不可阅读。")
        return payload
    chosen = v1._reading_body_selection(
        session,
        doc.id,
        redact=texts.mask,
        live_only=True,
        allowed_kinds=v1.LIST_BODY_KINDS if list_filter is not None else v1.BODY_KINDS,
    )
    if chosen is None:
        payload.update(status="empty", notice="正文暂未收录，请稍后重试或反馈问题。")
        return payload
    # 对完整正文脱敏后再分段，号码跨分页边界也不能漏出。
    owner_id, _, cleaned = chosen
    owner = session.get(Document, owner_id)
    source = session.get(Source, owner.source_id)
    payload["source"] = _source(owner, source)
    notice = "以下为收录的原文文字，未经逐字人工核对；图片、版式与未收录附件不包含在内。"
    hidden = False
    if list_filter is not None:
        result = list_filter(cleaned)
        if result.text is None and result.reason == "short":
            # 没认出名单、正文本来就太短：和别的类一样按没抓全说
            payload.update(status="empty", notice="正文收录不足，暂不展示为完整内容。")
            return payload
        if result.text is None:
            # 正文几乎全是名单，或名单没能和通知文字分开：只给查名单里的企业查询结果（D-088）
            what = (
                "这份通知的正文主要是名单"
                if result.reason == "list"
                else "这份通知的正文里有名单没能和通知文字分开，暂不展示正文"
            )
            payload.update(
                status="list",
                notice=f"{what}，站内只提供企业查询结果（查到后可以看原名单里的那一行）。",
            )
            return payload
        cleaned, hidden = result.text, result.hidden
        if hidden:
            notice = (
                "以下为收录的通知文字，未经逐字人工核对；正文里的名单内容站内不展开，可以在查名单里"
                "查企业。图片、版式与未收录附件不包含在内。"
            )
    elif len(cleaned) < v1.BODY_MIN_CHARS:
        payload.update(status="empty", notice="正文收录不足，暂不展示为完整内容。")
        return payload
    parts = [
        line[i : i + 1000]
        for line in cleaned.split("\n\n")
        if line
        for i in range(0, len(line), 1000)
    ]
    current = _revision(
        doc.id, payload["title"], payload["source"], cleaned, texts.EXTRACTOR_VERSION
    )
    after = _check_revision(cursor, revision, current)
    payload.update(revision=current, totalSegments=len(parts), notice=notice)
    if hidden:
        # 正文里有名单内容没展开：页面给"在已收录名单中查询企业"按钮
        payload["listHidden"] = True
    return _page(
        payload,
        [
            {"n": n + 1, "kind": "p", "text": parts[n], "loc": None, "cells": None}
            for n in range(after, min(after + PAGE_SEGMENTS, len(parts)))
        ],
        after,
        len(parts),
    )


def _safe_segment(row: AttachmentTextSegment) -> dict:
    loc = {}
    for key, value in (row.loc or {}).items():
        if key in ("page", "row", "table", "slide") and isinstance(value, int):
            loc[key] = value
        elif key == "sheet" and isinstance(value, str):
            loc[key] = texts.mask(value)
    cells = []
    for cell in row.cells or []:
        if not isinstance(cell, dict) or not isinstance(cell.get("c"), int):
            continue
        cells.append(
            {
                "c": cell.get("c"),
                "t": texts.mask(str(cell.get("t", ""))),
                "h": texts.mask(str(cell.get("h", ""))),
            }
        )
    return {
        "n": row.n,
        "kind": row.kind,
        "text": texts.mask(row.text),
        "loc": loc or None,
        "cells": cells or None,
    }


@router.get("/documents/{doc_id}/attachments/{attachment_id}/reading")
def attachment_reading(
    doc_id: int,
    attachment_id: int,
    session: ReadingSessionDep,
    request: Request,
    cursor: Cursor = "0",
    revision: Revision = None,
) -> dict:
    doc, _ = _document(session, doc_id, request)
    att = session.get(DocumentAttachment, attachment_id)
    if att is None or att.document_id not in group_ids(session, doc.id):
        raise HTTPException(404, "没有找到这个附件")
    owner = session.execute(
        select(Document, Source)
        .join(Source, Source.id == Document.source_id)
        .where(Document.id == att.document_id, Source.rights_level == 1, available_condition())
    ).first()
    if owner is None:
        raise HTTPException(404, "附件暂不可阅读")
    source_doc, src = owner
    payload = _payload(att.name, _source(source_doc, src))
    kind = effective_kind(source_doc.title, source_doc.kind_override)
    if "名单" in (kind, effective_kind(doc.title, doc.kind_override)):
        # 名单附件只给查名单里的那一行，不整份展示（D-088）；页面给"在已收录名单中查询企业"按钮
        payload.update(
            status="list",
            notice="名单在站内只提供企业查询结果（查到后可以看原名单里的那一行），暂不整份展示。",
        )
        return payload
    if (
        kind not in attachments.COPY_KINDS
        or effective_kind(doc.title, doc.kind_override) not in attachments.COPY_KINDS
    ):
        payload.update(status="unsupported", notice="此类附件可能包含个人信息，暂不展示全文。")
        return payload
    if doc.link_gone_at or source_doc.link_gone_at or not attachments._government_url(att.url):
        payload.update(status="unavailable", notice="来源暂不可用，请稍后重试或反馈问题。")
        return payload
    if not attachments.is_recent(
        source_doc.publish_date, source_doc.first_seen_at, v1.now(), kind=kind
    ):
        payload.update(status="unavailable", notice="此附件不在当前收录范围内。")
        return payload
    if (att.extension or "").lower() not in attachments.DOWNLOAD_TYPES:
        payload.update(status="unsupported", notice="此格式暂未支持站内阅读。")
        return payload
    url = session.scalar(
        select(AttachmentTextUrl).where(AttachmentTextUrl.url_hash == texts.url_key(att.url))
    )
    version = session.get(AttachmentTextVersion, url.version_id) if url and url.version_id else None
    if (
        not url
        or url.extractor != texts.EXTRACTOR_VERSION
        or (version and version.extractor != texts.EXTRACTOR_VERSION)
    ):
        payload.update(status="unavailable", notice="附件文字正在准备，请稍后重试。")
        return payload
    if url.status != "ok" or version is None or version.status != "ok":
        reason = url.reason or (version.reason if version else None)
        payload.update(
            status=reason if reason in ("scan", "empty") else "unavailable",
            notice="此附件为扫描件，暂未收录可读文字。"
            if reason == "scan"
            else "附件文字暂不可用，请稍后重试或反馈问题。",
        )
        return payload
    current = _revision(
        doc.id,
        att.id,
        payload["title"],
        payload["source"],
        version.sha256,
        version.extractor,
        version.extracted_at,
    )
    after = _check_revision(cursor, revision, current)
    total, last = session.execute(
        select(func.count(), func.max(AttachmentTextSegment.n)).where(
            AttachmentTextSegment.version_id == version.id
        )
    ).one()
    rows = session.scalars(
        select(AttachmentTextSegment)
        .where(AttachmentTextSegment.version_id == version.id, AttachmentTextSegment.n > after)
        .order_by(AttachmentTextSegment.n)
        .limit(PAGE_SEGMENTS)
    ).all()
    payload.update(revision=current, totalSegments=total, scannedPages=version.scanned_pages or 0)
    if not total:
        payload.update(status="empty", notice="附件未收录可读文字。")
        return payload
    payload.update(
        status="partial" if version.scanned_pages else "ok",
        notice=(
            f"其中 {version.scanned_pages} 页为扫描页，以下文字未包含这些页。"
            if version.scanned_pages
            else "以下为已提取的附件文字；图片、公章和原始版式未包含在内。"
        ),
    )
    return _page(payload, [_safe_segment(row) for row in rows], after, last)
