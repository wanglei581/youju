"""我的保存和清理期导出的接口（D-123，youju/saved.py、youju/gone.py）。

- 保存（POST /me/saved）：要同意过写进"您保存的通知"的隐私保护指引
  （agreements.SAVED_AGREEMENT_VERSION），这份通知原网页失效、还在 7 天清理期里、到期会清理
  （gone.in_window），有能保存的文字；每人最多 YOUJU_SAVED_MAX_PER_USER 份。
  开关（YOUJU_GONE_CLEANUP_ENABLED）关着时 404。
- 清理期里导出 Word（POST /documents/{id}/gone-export）：和导出题录一样不用登录，开关关着时 404。
- 已经保存的看列表、按页读、导出、删除：开关后来关了也照常能用（用户自己的数据，要能看、能删）。
"""

from __future__ import annotations

import json
import time as clock
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Request as FastAPIRequest
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from youju import gone, saved
from youju.agreements import SAVED_AGREEMENT_VERSION, agreement_at_least
from youju.api import attachments as download_api
from youju.api import exports, readers, v1
from youju.api.me import KnownUserDep, UserDep
from youju.config import get_settings
from youju.db import get_session
from youju.dedup import canonical_ids
from youju.models import Document, SavedDocument, Source
from youju.takedown import is_taken_down

router = APIRouter(prefix="/api/v1", tags=["用户"])
# 阅读页同款：不缓存，出错也按 JSON 回（readers.NoStoreRoute）
reading_router = APIRouter(route_class=readers.NoStoreRoute)
SessionDep = Annotated[Session, Depends(get_session)]
# part：空串是正文，1、2… 是第几个附件
Part = Annotated[str, Query(pattern=r"^([1-9][0-9]{0,2})?$")]
WORD_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SAVE_OUTDATED = "保存通知要先同意新版隐私保护指引，请更新小程序后重新阅读并同意"
NOT_IN_WINDOW = "只有原网页无法访问 7 天以内的通知可以保存和导出"


def _require_enabled() -> None:
    if not gone.enabled():
        raise HTTPException(status_code=404, detail="没有找到接口")


def _require_saving() -> None:
    """保存要开关开着、新版隐私指引已经上线（gone.saving_open），不然没有这个接口。"""
    if not gone.saving_open():
        raise HTTPException(status_code=404, detail="没有找到接口")


class SaveIn(BaseModel):
    docId: str = Field(pattern=r"^[1-9][0-9]{0,9}$")


def _window_document(session: Session, doc_id: int) -> Document:
    """原网页失效、还在清理期里、到期会清理的这份通知（同一通知的其他网址换成主文件）。"""
    if is_taken_down(session, doc_id):
        raise HTTPException(status_code=404, detail="该文件已下架")
    doc_id = canonical_ids(session, [doc_id]).get(doc_id, doc_id)
    row = session.execute(v1._base().where(Document.id == doc_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=v1.not_found_detail())
    doc = row[0]
    if not gone.in_window(session, doc, v1.now()):
        raise HTTPException(status_code=409, detail=NOT_IN_WINDOW)
    return doc


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _item(row, cleaned: bool) -> dict:
    return {
        "id": str(row.id),
        "docId": str(row.document_id),
        "title": row.title,
        "issuer": row.issuer,
        "site": row.site,
        "region": row.region,
        "kind": row.kind,
        "date": _iso(row.publish_date),
        "goneOn": row.gone_on.isoformat(),
        "savedAt": row.saved_at.astimezone(gone.BEIJING).isoformat(timespec="seconds"),
        "chars": row.chars,
        "truncated": bool(row.truncated),
        # 通知已经清理掉了（只剩这份副本）
        "cleaned": cleaned,
        "note": saved.gone_note(row),
    }


_LIST_COLUMNS = (
    SavedDocument.id,
    SavedDocument.document_id,
    SavedDocument.title,
    SavedDocument.issuer,
    SavedDocument.site,
    SavedDocument.region,
    SavedDocument.kind,
    SavedDocument.publish_date,
    SavedDocument.gone_on,
    SavedDocument.chars,
    SavedDocument.truncated,
    SavedDocument.saved_at,
)


def saved_list(session: Session, user_id: int) -> list[dict]:
    """这个用户能看的副本（新保存的在前），不读正文。我的数据（me.personal_data）也用。"""
    rows = session.execute(
        select(*_LIST_COLUMNS)
        .where(SavedDocument.user_id == user_id, saved.visible_condition())
        .order_by(SavedDocument.saved_at.desc(), SavedDocument.id.desc())
    ).all()
    ids = [row.document_id for row in rows]
    existing = (
        set(session.scalars(select(Document.id).where(Document.id.in_(ids)))) if ids else set()
    )
    return [_item(row, row.document_id not in existing) for row in rows]


@router.post("/me/saved", status_code=201, dependencies=[Depends(_require_saving)])
def save_document(body: SaveIn, session: SessionDep, user: UserDep) -> dict:
    if not agreement_at_least(user.agreement_version, SAVED_AGREEMENT_VERSION):
        raise HTTPException(status_code=403, detail=SAVE_OUTDATED)
    doc = _window_document(session, int(body.docId))
    now = v1.now()
    existing = session.scalar(
        select(SavedDocument).where(
            SavedDocument.user_id == user.id, SavedDocument.document_id == doc.id
        )
    )
    limit = get_settings().saved_max_per_user
    if existing is None and saved.count(session, user.id) >= limit:
        raise HTTPException(
            status_code=409, detail=f"最多保存 {limit} 份，请先在“我的保存”里删掉一些"
        )
    content, chars, truncated = saved.build(session, doc, now)
    if not saved.has_text(content):
        raise HTTPException(status_code=409, detail="这份通知没有能保存的正文和附件文字")
    info = saved.meta(doc, session.get(Source, doc.source_id))
    row = existing or SavedDocument(user_id=user.id, document_id=doc.id)
    # 又保存一次：换成现在的内容（这期间附件可能读出来了）
    row.title, row.issuer, row.site = info["title"], info["issuer"], info["site"]
    row.region, row.kind, row.publish_date = info["region"], info["kind"], info["publish_date"]
    row.gone_on = gone.gone_on(doc.link_gone_at)
    row.content = json.dumps(content, ensure_ascii=False)
    row.chars, row.truncated, row.saved_at = chars, truncated, now
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # 同一个人同时点了两次：另一次已经存好了
        session.rollback()
        row = session.scalar(
            select(SavedDocument).where(
                SavedDocument.user_id == user.id, SavedDocument.document_id == doc.id
            )
        )
        if row is None:
            raise
    return {"item": _item(row, False)}


@router.get("/me/saved")
def list_saved(session: SessionDep, user: UserDep) -> dict:
    return {"items": saved_list(session, user.id), "max": get_settings().saved_max_per_user}


@router.delete("/me/saved/{saved_id}", status_code=204)
def delete_saved(saved_id: int, session: SessionDep, user: KnownUserDep) -> None:
    """删一份（没同意过、协议版本过期的也能删；没有这份也返回成功）。"""
    if user is not None:
        session.execute(
            delete(SavedDocument).where(
                SavedDocument.id == saved_id, SavedDocument.user_id == user.id
            )
        )
        session.commit()


def _owned(session: Session, user_id: int, saved_id: int) -> SavedDocument:
    row = session.scalar(saved.owned(session, user_id).where(SavedDocument.id == saved_id))
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到这份保存的通知，可能已经删除")
    return row


def _toc(content: dict) -> dict:
    body = content["body"]
    return {
        "body": {"readable": bool(body["parts"]), "note": body["note"]},
        "attachments": [
            {
                "id": str(index),
                "name": item["name"],
                "ext": item["ext"],
                "readable": bool(item["segments"]),
                "state": "ok" if item["segments"] else "saved_none",
                "stateText": item["note"],
            }
            for index, item in enumerate(content["attachments"], 1)
        ],
    }


@reading_router.get("/me/saved/{saved_id}/reading")
def saved_reading(
    saved_id: int,
    session: SessionDep,
    user: UserDep,
    request: FastAPIRequest,
    part: Part = "",
    cursor: readers.Cursor = "0",
    revision: readers.Revision = None,
) -> dict:
    """按页读保存的副本：和阅读接口（readers.document_reading）同一个格式，阅读页照常翻页。
    和阅读接口一样限频（每翻一页要读出整份副本）。"""
    if not download_api._allow(download_api._caller(request), clock.monotonic()):
        raise HTTPException(status_code=429, detail="阅读太频繁，请稍后再试")
    row = _owned(session, user.id, saved_id)
    content = json.loads(row.content)
    source = {
        "id": str(row.document_id),
        "title": row.title,
        "issuer": row.issuer,
        "date": _iso(row.publish_date),
        "host": "",
    }
    if part:
        index = int(part)
        if index > len(content["attachments"]):
            raise HTTPException(status_code=404, detail="没有找到这个附件")
        item = content["attachments"][index - 1]
        payload = readers._payload(item["name"], source)
        segments, note, scanned = item["segments"], item["note"], item["scanned"]
    else:
        payload = readers._payload(row.title, source)
        segments = [
            {"n": n, "kind": "p", "text": text, "loc": None, "cells": None}
            for n, text in enumerate(content["body"]["parts"], 1)
        ]
        note, scanned = content["body"]["note"], 0
        if content["body"]["listHidden"]:
            payload["listHidden"] = True
    payload["toc"] = _toc(content)
    payload["goneNote"] = saved.gone_note(row)
    if not segments:
        payload.update(status="empty", state="empty", notice=note or "这部分没有保存文字。")
        return payload
    current = readers._revision("saved", row.id, row.saved_at.isoformat(), part)
    after = readers._check_revision(cursor, revision, current)
    ends = readers.page_ends(
        [(s["n"], len(json.dumps(s, ensure_ascii=False).encode()) + 64) for s in segments]
    )
    index = readers._locate(ends, after)
    start = ends[index - 1] if index else 0
    payload.update(
        revision=current,
        totalSegments=len(segments),
        state="ok",
        scannedPages=scanned,
        status="partial" if scanned else "ok",
        notice=(
            f"其中 {scanned} 页是扫描件，暂不能识别文字，下面的文字不含这些页。" if scanned else ""
        ),
    )
    return readers._page(payload, segments[start : ends[index]], ends, index)


def _word_response(data: bytes, stamp: str) -> Response:
    if len(data) >= exports.MAX_BYTES:
        raise HTTPException(
            status_code=413, detail="这份通知太长，导出的文件超过 1000 KiB，请在小程序里阅读"
        )
    filename = f"有据保存_{stamp[:10]}.docx"
    return Response(
        content=data,
        media_type=WORD_MIME,
        headers={
            "Content-Disposition": (
                f"attachment; filename=youju-saved_{stamp[:10]}.docx; "
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "Cache-Control": "no-store",
        },
    )


def _export_guard(request: FastAPIRequest):
    refused = exports._allow_export(exports._caller(request), clock.monotonic())
    if refused == "user":
        raise HTTPException(status_code=429, detail="导出太频繁，请 10 分钟后再试")
    if refused == "busy" or not exports._export_slots.acquire(timeout=exports._EXPORT_SLOTS_WAIT):
        raise HTTPException(status_code=503, detail="现在导出的人多，请稍后再试")


@router.post("/me/saved/{saved_id}/export")
def export_saved(
    saved_id: int, session: SessionDep, user: UserDep, request: FastAPIRequest
) -> Response:
    row = _owned(session, user.id, saved_id)
    _export_guard(request)
    try:
        stamp = datetime.now(gone.BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        info = {
            "title": row.title,
            "issuer": row.issuer,
            "site": row.site,
            "publish_date": row.publish_date,
        }
        data = saved.word(info, json.loads(row.content), [saved.gone_note(row)], stamp)
        return _word_response(data, stamp)
    finally:
        exports._export_slots.release()


@router.post("/documents/{doc_id}/gone-export", dependencies=[Depends(_require_enabled)])
def export_gone(doc_id: int, session: SessionDep, request: FastAPIRequest) -> Response:
    """清理期里导出这份通知的正文和附件文字（Word），不用登录（同导出题录）。"""
    doc = _window_document(session, doc_id)
    _export_guard(request)
    try:
        now = v1.now()
        content, _, _ = saved.build(session, doc, now)
        if not saved.has_text(content):
            raise HTTPException(status_code=409, detail="这份通知没有能导出的正文和附件文字")
        info = saved.meta(doc, session.get(Source, doc.source_id))
        stamp = now.astimezone(gone.BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        notice = gone.reading_note(doc)
        return _word_response(saved.word(info, content, [notice], stamp), stamp)
    finally:
        exports._export_slots.release()


router.include_router(reading_router)
