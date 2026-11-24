"""补详情失败页（D8）：临时性失败（网络中断、robots.txt 暂时取不到、网站 5xx）连续 3 次、
已停止自动重试的文件列在这里，让人知道哪些文件没有正文、问题出在哪个网站。

口令、审计日志和 /api/admin/ 前缀沿用其他核对台（由 app.py 传入 require_admin、audit）。
只读，不改数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.crawler.detail_service import MAX_ATTEMPTS, stopped_retrying
from youju.db import get_session
from youju.models import Document, DocumentDetail, Source

SessionDep = Annotated[Session, Depends(get_session)]
# 网址里的主机名（入库的网址都是 scheme://host/… 的规范写法，主机名已小写）
_HOST = func.substring_index(func.substring_index(Document.url, "/", 3), "/", -1)


def stopped_count(session: Session) -> int:
    return (
        session.scalar(select(func.count()).select_from(DocumentDetail).where(stopped_retrying()))
        or 0
    )


def router(require_admin, audit) -> APIRouter:
    def no_store(response: Response):
        response.headers["Cache-Control"] = "no-store"

    routes = APIRouter(
        prefix="/api/admin/detail-failures",
        dependencies=[Depends(require_admin), Depends(no_store)],
    )

    @routes.get("")
    def stopped(
        session: SessionDep,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        total = stopped_count(session)
        rows = session.execute(
            select(Document, Source, DocumentDetail)
            .join(DocumentDetail, DocumentDetail.document_id == Document.id)
            .join(Source, Source.id == Document.source_id)
            .where(stopped_retrying())
            .order_by(DocumentDetail.fetched_at.desc(), Document.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        # 按网站汇总全部停止重试的文件（在数据库里数，不受分页影响），列最多的 10 个网站
        count = func.count().label("count")
        hosts = session.execute(
            select(_HOST.label("host"), count)
            .select_from(DocumentDetail)
            .join(Document, Document.id == DocumentDetail.document_id)
            .where(stopped_retrying())
            .group_by(_HOST)
            .order_by(count.desc(), _HOST)
            .limit(10)
        ).all()
        audit(
            request,
            "detail_failures_read",
            total=total,
            count=len(rows),
            limit=limit,
            offset=offset,
        )
        return {
            "total": total,
            "max_attempts": MAX_ATTEMPTS,
            "hosts": [{"host": host, "count": n} for host, n in hosts],
            "items": [
                {
                    "document_id": doc.id,
                    "title": doc.title,
                    "url": doc.url,
                    "publish_date": doc.publish_date,
                    "source": f"{source.province} · {source.dept_full_name} · {source.column_name}",
                    "status": detail.status,
                    "http_status": detail.http_status,
                    "failure_reason": detail.failure_reason,
                    "attempts": detail.attempt_count,
                    "last_attempt_at": detail.fetched_at,
                }
                for doc, source, detail in rows
            ],
        }

    return routes


def console() -> HTMLResponse:
    page = (Path(__file__).resolve().parent.parent / "admin" / "details.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})
