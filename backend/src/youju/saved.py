"""我的保存（D-123 ③④，创始人 11/8）：原网页失效、还在 7 天清理期里的通知（youju/gone.py），用户
可以把正文和附件文字永久保存到自己名下；清理以后在"我的保存"里还能看、能导出 Word，标明原网页
打不开的日期；用户删除或注销时一起删；下架的（四种原因都算）一起删。

存的是保存那一刻阅读页给的东西：正文（v1.reading_body）和能读的附件（attachment_text.readings），
已脱敏、按页面说法换过，和阅读接口一样分段（readers.body_parts、readers._safe_segment）；"我的保存"
用阅读页翻页显示（api/saved.py）。不存附件原件（D-123 ③，省服务器硬盘）。

表 saved_documents 的 document_id 不建外键：本机到期清理删掉通知（2.0 经同步，服务器上跟着删）以后
副本还在。下架时按编号删副本（purge_taken_down）：单库时下架当场删（takedown.change_state），2.0 里
下架在本机做、经同步过来，服务器每天的 cleanup 任务删；没删之前读的时候就不给（visible_condition）。
"""

from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO
from typing import Any

from docx import Document as WordDocument
from sqlalchemy import Select, and_, delete, exists, func, or_, select
from sqlalchemy.orm import Session

from youju import gone
from youju.models import Document, DocumentTakedownEvent, SavedDocument, Source
from youju.takedown import available_condition

VERSION = 1
# 一份副本最多存多少字节（JSON）：MEDIUMTEXT 上限 16MB；11/4 线上快照最长的正文约 22 万字、
# 最长的附件约 14 万字，4MB 能放下约 130 万字。超过的后面的附件段落不存，标 truncated
MAX_BYTES = 4 * 1024 * 1024


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def build(session: Session, doc: Document, now: datetime) -> tuple[dict, int, bool]:
    """保存、导出用的内容（和阅读页给的一样）：(content, 字数, 是否没存全)。调用方先确认这份在
    清理期里（gone.in_window）、没下架。"""
    from youju import attachment_text as texts
    from youju.api import readers, v1
    from youju.classify import effective_kind

    kind = effective_kind(doc.title, doc.kind_override)
    readings = texts.readings(session, doc.id, now)
    body = v1.reading_body(session, doc, kind, attachments=bool(readings), redact=texts.mask)
    budget = MAX_BYTES - 4096  # 留给标题、附件名等
    truncated = False
    parts: list[str] = []
    if body.status == "ok":
        for part in readers.body_parts(body.text):
            size = _size(part) + 8
            if size > budget:
                truncated = True
                break
            parts.append(part)
            budget -= size
    attachments = []
    for reading in readings:
        item: dict[str, Any] = {
            "name": v1.display_attachment_name(v1.display_marks(texts.mask(reading.name)))[:300],
            "ext": reading.ext.upper(),
            "note": "" if reading.readable else reading.note,
            "scanned": 0,
            "segments": [],
        }
        if reading.readable and not truncated:
            item["scanned"] = reading.scanned_pages or 0
            for row in texts.segments(session, reading.version_id):
                segment = readers._safe_segment(row)
                size = _size(segment) + 8
                if size > budget:
                    truncated = True
                    break
                segment["n"] = len(item["segments"]) + 1
                item["segments"].append(segment)
                budget -= size
            if not item["segments"]:
                item["note"] = "附件太长，没能保存" if truncated else texts.NOTES["empty"]
        elif reading.readable:
            item["note"] = "附件太长，没能保存"
        attachments.append(item)
    content = {
        "v": VERSION,
        "body": {
            "parts": parts,
            # 正文给不了时页面上那句（readers._BODY_NOTES），"我的保存"的目录里照写
            "note": "" if parts else readers._BODY_NOTES.get(readers._body_key(body), ""),
            "listHidden": bool(body.hidden),
        },
        "attachments": attachments,
    }
    chars = sum(len(part) for part in parts) + sum(
        len(segment["text"]) for item in attachments for segment in item["segments"]
    )
    return content, chars, truncated


def has_text(content: dict) -> bool:
    return bool(content["body"]["parts"]) or any(
        item["segments"] for item in content["attachments"]
    )


def meta(doc: Document, source: Source) -> dict:
    """标题、发文机关、来源网站、地区、类型、发布日期（和阅读页、详情页同一个写法）。"""
    from youju import attachment_text as texts
    from youju.api import v1
    from youju.classify import effective_kind, region_label
    from youju.issuer import normalize_issuer

    return {
        "title": v1.display_marks(texts.mask(doc.title))[:1000],
        "issuer": v1.display_marks(texts.mask(normalize_issuer(source.dept_full_name)))[:500],
        # 出处只写来源网站名称（D-097），不写网址
        "site": f"{source.dept_full_name}网站"[:500],
        "region": region_label(source.province, source.level, source.city, source.district)[:100],
        "kind": effective_kind(doc.title, doc.kind_override),
        "publish_date": doc.publish_date,
    }


def visible_condition():
    """读的时候不给的副本：这份通知现在下架着（整组），或者保存以后有过下架（下架后又恢复的也不给，
    下架那时就该删了）。cleanup 任务每天按同一个条件删掉（purge_taken_down）。"""
    hidden = select(Document.id).where(~available_condition())
    later = exists().where(
        DocumentTakedownEvent.document_id == SavedDocument.document_id,
        DocumentTakedownEvent.action == "takedown",
        DocumentTakedownEvent.operated_at >= SavedDocument.saved_at,
    )
    return and_(SavedDocument.document_id.not_in(hidden), ~later)


def owned(session: Session, user_id: int) -> Select:
    return select(SavedDocument).where(SavedDocument.user_id == user_id, visible_condition())


def count(session: Session, user_id: int) -> int:
    return session.scalar(select(func.count()).select_from(owned(session, user_id).subquery())) or 0


def purge_taken_down(session: Session) -> int:
    """删掉下架了的通知的副本（D-123 ④；不受开关影响）：条件同 visible_condition。返回删了几份。
    不提交，由调用方提交。"""
    hidden = select(Document.id).where(~available_condition())
    later = exists().where(
        DocumentTakedownEvent.document_id == SavedDocument.document_id,
        DocumentTakedownEvent.action == "takedown",
        DocumentTakedownEvent.operated_at >= SavedDocument.saved_at,
    )
    ids = list(
        session.scalars(
            select(SavedDocument.id).where(or_(SavedDocument.document_id.in_(hidden), later))
        )
    )
    for start in range(0, len(ids), 1000):
        session.execute(
            delete(SavedDocument).where(SavedDocument.id.in_(ids[start : start + 1000]))
        )
    return len(ids)


def purge_documents(session: Session, document_ids: list[int]) -> int:
    """下架时当场删这些通知的副本（单库时 takedown.change_state 调用；2.0 本机上没有副本，
    删 0 份）。不提交。"""
    if not document_ids:
        return 0
    return session.execute(
        delete(SavedDocument).where(SavedDocument.document_id.in_(document_ids))
    ).rowcount


def gone_note(row: SavedDocument) -> str:
    """ "我的保存"里、阅读页上写的那句：原网页打不开的日期（D-123 ③：标明原网页撤下的日期）。"""
    return (
        f"原网页{gone.md(row.gone_on)}起打不开，内容可能已被发布机关撤回或更正，以官方为准。"
        f"这是你{gone.md(gone.beijing_date(row.saved_at))}保存的副本。"
    )


# ---- 导出 Word ----


def word(info: dict, content: dict, notice_lines: list[str], stamp: str) -> bytes:
    """一份通知的 Word：标题、出处、原网页打不开的提示，正文和附件的文字，最后是来源说明。"""
    from youju.export_notes import DISCLAIMER, SEARCH_NOTICE

    book = WordDocument()
    book.add_heading(info["title"], level=1)
    line = " · ".join(
        part
        for part in (
            info["issuer"],
            info["publish_date"].isoformat() if info["publish_date"] else "",
        )
        if part
    )
    if line:
        book.add_paragraph(line)
    book.add_paragraph(f"来源：{info['site']}")
    for text in notice_lines:
        book.add_paragraph(text)
    book.add_heading("正文", level=2)
    body = content["body"]
    if body["parts"]:
        for part in body["parts"]:
            book.add_paragraph(part)
    else:
        book.add_paragraph(body["note"] or "没有能显示的正文。")
    for index, item in enumerate(content["attachments"], 1):
        book.add_heading(f"附件 {index}：{item['name']}", level=2)
        if item["note"]:
            book.add_paragraph(item["note"])
        if item["scanned"]:
            book.add_paragraph(
                f"其中 {item['scanned']} 页是扫描件，暂不能识别文字，下面的文字不含这些页。"
            )
        for segment in item["segments"]:
            book.add_paragraph(segment["text"])
    book.add_paragraph("")
    book.add_paragraph(
        "以上文字是从原网页和附件自动提取的，未经人工逐字核对；图片、公章和版式不在内。"
    )
    book.add_paragraph(DISCLAIMER)
    book.add_paragraph(SEARCH_NOTICE)
    book.add_paragraph(f"导出时间（北京时间）：{stamp}")
    output = BytesIO()
    book.save(output)
    return output.getvalue()
