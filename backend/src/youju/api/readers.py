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

PAGE_SEGMENTS = 120
MAX_PAGE_BYTES = 128 * 1024
# 分页按存下的文字大小算（每页最多 PAGE_SEGMENTS 段、合计不超过 PAGE_BUDGET 字节），整份内容的
# 页数、每页从哪一段开始是定的：页面能写"第 N 页 / 共 M 页"、记住读到哪一页（D-097）。一页约
# 4000 字（手机上十来屏）：2 万字的指南分成 5 页左右，续读才有意义；返回前还要再脱敏、换说法
# （会变长一点），离返回上限 MAX_PAGE_BYTES 还很远。
# 段数上限只防一页被极短的段撑得太长（附件每段另算 64 字节，再短的段一页也就一百多段），低于小程序
# 每页最多 200 段的校验。原来是 20：填报模板、表格这类短段落的附件一页只有几百字，5000 多字要翻
# 14 页（10/29 D-097 ④ 验收）
PAGE_BUDGET = 12 * 1024
# 2：D-097 分页定下来、遮挡处说法改了，旧版本号的续读位置作废。10/29 段数上限放宽、分页变了，
# 版本号没改：记着的位置、部署时开着的页面翻页用的位置，不再是一页的开头时阅读接口回 422，小程序
# 回到第 1 页、写"位置已失效"；改了版本号小程序会说成"已更新"，内容其实没变，分页没变的文件也要
# 从头读
READER_VERSION = 2


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
        "title": v1.display_marks(texts.mask(doc.title))[:1000],
        "issuer": v1.display_marks(texts.mask(src.dept_full_name))[:500],
        "date": doc.publish_date.isoformat() if doc.publish_date else None,
        # 旧版小程序还显示网址；新版只写网站名称、发文机关、日期（D-097），下一版去掉
        "host": urlsplit(doc.url).hostname or "",
    }


def _payload(title: str, source: dict, status="ok", notice="") -> dict:
    return {
        "status": status,
        "state": None,
        "title": v1.display_marks(texts.mask(title))[:1000],
        "source": source,
        "revision": None,
        "segments": [],
        "nextCursor": None,
        "prevCursor": None,
        "page": 0,
        "pages": 0,
        "totalSegments": 0,
        "notice": notice,
        "scannedPages": 0,
        "toc": None,
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


SEGMENT_MAX = 1000


def _cut(line: str) -> int:
    """一行超过 SEGMENT_MAX 字时在哪儿切：这个长度以内最后一个句号、分号后面；
    前一半里都没有就在 SEGMENT_MAX 处切。"""
    end = max(line.rfind(mark, 0, SEGMENT_MAX) for mark in "。；！？")
    return end + 1 if end >= SEGMENT_MAX // 2 else SEGMENT_MAX


def body_parts(text: str) -> list[str]:
    """正文分段：空行隔开的一块是一段；超过 SEGMENT_MAX 字的按行拼成不超过这么长的几段（一行本身
    太长才在句末切开），翻页不切在半句话中间。"""
    parts: list[str] = []
    for block in text.split("\n\n"):
        if not block:
            continue
        current = ""
        for line in block.split("\n"):
            while len(line) > SEGMENT_MAX:
                if current:
                    parts.append(current)
                    current = ""
                cut = _cut(line)
                parts.append(line[:cut])
                line = line[cut:]
            if current and len(current) + 1 + len(line) > SEGMENT_MAX:
                parts.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            parts.append(current)
    return parts


def page_ends(sizes: list[tuple[int, int]]) -> list[int]:
    """按段的顺序（段号, 字节数）分页，返回每页最后一段的段号。一段超过预算的自己一页。"""
    ends: list[int] = []
    count = used = 0
    last = 0
    for n, size in sizes:
        if count and (count >= PAGE_SEGMENTS or used + size > PAGE_BUDGET):
            ends.append(last)
            count = used = 0
        count += 1
        used += size
        last = n
    if count:
        ends.append(last)
    return ends


def _locate(ends: list[int], after: int) -> int:
    """续读位置（上一页最后一段的段号，第 1 页是 0）是第几页（从 0 数）；不是页的开头就报错。"""
    starts = [0, *ends[:-1]]
    if after not in starts:
        raise HTTPException(422, "阅读位置超出范围，请重新打开")
    return starts.index(after)


def _page(payload: dict, segments: list[dict], ends: list[int], index: int) -> dict:
    payload.update(
        segments=segments,
        page=index + 1,
        pages=len(ends),
        nextCursor=str(ends[index]) if index + 1 < len(ends) else None,
        prevCursor=(str(ends[index - 2]) if index >= 2 else "0") if index >= 1 else None,
    )
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PAGE_BYTES:
        # 一段特别大（几万字的表格行）：这一页不显示，翻页照常
        payload.update(
            status="unavailable",
            segments=[],
            notice="这一页有一段特别长，暂时显示不了；可以翻页接着读，或反馈问题。",
        )
    return payload


def _toc(body: v1.ReadingBody, readings: list[texts.Reading]) -> dict:
    """阅读页的目录：正文和每个附件能不能读（和详情页同一个判断）。"""
    return {
        "body": {"readable": body.status == "ok", "note": _BODY_NOTES.get(_body_key(body), "")},
        "attachments": [
            {
                "id": str(r.attachment_id),
                "name": v1.display_marks(texts.mask(r.name))[:300],
                "ext": r.ext.upper(),
                "readable": r.readable,
                "state": r.state,
                "stateText": r.note,
            }
            for r in readings
        ],
    }


# 正文读不了时页面上写的话（D-097 设计稿第三节）
_BODY_NOTES = {
    "personal": "含个人信息，不在小程序内展示全文。",
    "list:list": "这份通知的正文主要是名单，名单请在“查名单”里按企业名称查询。",
    "list:mixed": (
        "这份通知的正文里名单没能和通知文字分开，不展示正文；名单请在“查名单”里按企业名称查询。"
    ),
    "none:attachments": "这份通知的内容在附件里。",
    "none:site": "这个网站的正文还取不到。",
    "none:link_gone": "原网页已打不开，正文暂不提供。",
}
# 正文读不了时阅读接口的 status（旧版小程序按它显示）
_BODY_STATUS = {
    "personal": "unsupported",
    "list:list": "list",
    "list:mixed": "list",
    "none:attachments": "empty",
    "none:site": "empty",
    "none:link_gone": "unavailable",
}


def _body_key(body: v1.ReadingBody) -> str:
    return body.status if body.status in ("ok", "personal") else f"{body.status}:{body.reason}"


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
    readings = texts.readings(session, doc.id, v1.now())
    # 对完整正文脱敏后再分段，号码跨分页边界也不能漏出。
    body = v1.reading_body(session, doc, kind, attachments=bool(readings), redact=texts.mask)
    payload["toc"] = _toc(body, readings)
    if body.status != "ok":
        payload.update(
            status=_BODY_STATUS.get(_body_key(body), "unavailable"),
            state=body.status if body.status != "none" else body.reason,
            notice=_BODY_NOTES.get(_body_key(body), ""),
        )
        return payload
    owner = session.get(Document, body.owner_id)
    source = session.get(Source, owner.source_id)
    payload["source"] = _source(owner, source)
    parts = body_parts(body.text)
    segments = [
        {"n": n + 1, "kind": "p", "text": part, "loc": None, "cells": None}
        for n, part in enumerate(parts)
    ]
    ends = page_ends([(s["n"], len(json.dumps(s, ensure_ascii=False).encode())) for s in segments])
    current = _revision(
        doc.id, payload["title"], payload["source"], body.text, texts.EXTRACTOR_VERSION
    )
    after = _check_revision(cursor, revision, current)
    index = _locate(ends, after)
    payload.update(
        revision=current,
        totalSegments=len(parts),
        state="ok",
        notice=(
            "正文如下，名单请在“查名单”里按企业名称查询。文字自动提取，未经人工逐字核对。"
            if body.hidden
            else "以下是从原网页自动提取的文字，未经人工逐字核对；图片和表格样式不在内。"
        ),
    )
    if body.hidden:
        # 正文里有名单内容没展开：页面给"在已收录名单中查询企业"按钮
        payload["listHidden"] = True
    start = ends[index - 1] if index else 0
    return _page(payload, segments[start : ends[index]], ends, index)


def _safe_segment(row: AttachmentTextSegment) -> dict:
    loc = {}
    for key, value in (row.loc or {}).items():
        if key in ("page", "row", "table", "slide") and isinstance(value, int):
            loc[key] = value
        elif key == "sheet" and isinstance(value, str):
            loc[key] = v1.display_marks(texts.mask(value))
    cells = []
    for cell in row.cells or []:
        if not isinstance(cell, dict) or not isinstance(cell.get("c"), int):
            continue
        cells.append(
            {
                "c": cell.get("c"),
                "t": v1.display_marks(texts.mask(str(cell.get("t", "")))),
                "h": v1.display_marks(texts.mask(str(cell.get("h", "")))),
            }
        )
    return {
        "n": row.n,
        "kind": row.kind,
        "text": v1.display_marks(texts.mask(row.text)),
        "loc": loc or None,
        "cells": cells or None,
    }


# 附件读不了时阅读接口的 status（旧版小程序按它显示）；页面上的话见 attachment_text.NOTES
_ATTACHMENT_STATUS = {
    "personal": "unsupported",
    "list": "list",
    "kind": "unsupported",
    "format": "unsupported",
    "scan": "scan",
    "empty": "empty",
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
    now = v1.now()
    readings = texts.readings(session, doc.id, now)
    # 详情页列出的附件直接用目录里的判断；组里别的网址的附件（以前的详情页可能列过）单独判断
    state = next((r for r in readings if r.attachment_id == att.id), None) or next(
        iter(texts.judge(session, doc, [att], now))
    )
    kind = effective_kind(doc.title, doc.kind_override)
    body = v1.reading_body(session, doc, kind, attachments=bool(readings), redact=texts.mask)
    payload["toc"] = _toc(body, readings)
    payload["state"] = state.state
    if not state.readable:
        payload.update(
            status=_ATTACHMENT_STATUS.get(state.state, "unavailable"), notice=f"{state.note}。"
        )
        return payload
    url = session.scalar(
        select(AttachmentTextUrl).where(AttachmentTextUrl.url_hash == texts.url_key(att.url))
    )
    version = session.get(AttachmentTextVersion, url.version_id)
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
    sizes = [
        (n, int(size or 0) + 64)
        for n, size in session.execute(
            select(
                AttachmentTextSegment.n,
                func.octet_length(AttachmentTextSegment.text)
                + func.coalesce(func.octet_length(AttachmentTextSegment.cells), 0),
            )
            .where(AttachmentTextSegment.version_id == version.id)
            .order_by(AttachmentTextSegment.n)
        )
    ]
    payload.update(revision=current, totalSegments=len(sizes))
    if not sizes:
        payload.update(status="empty", state="empty", notice=f"{texts.NOTES['empty']}。")
        return payload
    ends = page_ends(sizes)
    index = _locate(ends, after)
    start = ends[index - 1] if index else 0
    rows = session.scalars(
        select(AttachmentTextSegment)
        .where(
            AttachmentTextSegment.version_id == version.id,
            AttachmentTextSegment.n > start,
            AttachmentTextSegment.n <= ends[index],
        )
        .order_by(AttachmentTextSegment.n)
    ).all()
    scanned = version.scanned_pages or 0
    payload.update(
        scannedPages=scanned,
        status="partial" if scanned else "ok",
        notice=(
            f"其中 {scanned} 页是扫描件，暂不能识别文字，下面的文字不含这些页。"
            if scanned
            else "以下是从附件自动提取的文字，未经人工逐字核对；图片、公章和版式不在内。"
        ),
    )
    return _page(payload, [_safe_segment(row) for row in rows], ends, index)
