"""申报条件核对台（D-052 第④项）：逐条通过、改写或删除，整份确认后才对外。

口令、审计日志和 /api/admin/ 前缀沿用其他核对台（由 app.py 传入 require_admin、audit）。
改写只能改条件那句话和类别，原文引用不能改；确认过的清单要先撤回确认才能再改。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju import conditions
from youju.ai.conditions import TEXT_MAX, clean_text, is_judgement
from youju.db import get_session
from youju.models import CONDITION_CATEGORIES, ConditionItem, ConditionList, Document

SessionDep = Annotated[Session, Depends(get_session)]
Category = Literal["主体资格", "财务指标", "知识产权", "人员", "其他"]
TIER_LABELS = {0: "30 天内截止", 1: "截止较晚或未知", 2: "已过截止"}


class ReviewerIn(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=80)

    @field_validator("reviewed_by", mode="before")
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class EditIn(ReviewerIn):
    text: str = Field(min_length=1, max_length=TEXT_MAX)
    category: Category | None = None


class ItemOut(BaseModel):
    id: int
    position: int
    category: str
    text: str
    original_text: str
    quote: str
    before: str
    after: str
    status: Literal["pending", "approved", "deleted"]
    reviewed_by: str | None


class ListOut(BaseModel):
    list_id: int
    document_id: int
    title: str
    url: str
    publish_date: date | None
    status: str
    # 排队的分档：0 三十天内截止 / 1 截止较晚或未知 / 2 已过截止（已确认的清单为空）
    tier: int | None
    tier_label: str | None
    due: date | None
    dropped: int
    current: bool
    confirmed_by: str | None
    confirmed_at: datetime | None
    items: list[ItemOut]


def _out(session: Session, row: ConditionList, order: tuple[int, int, int] | None) -> ListOut:
    doc = session.get(Document, row.document_id)
    current = conditions.is_current(session, row)
    body = conditions.public_body(doc.detail.body_text) if current and doc.detail else ""
    items = []
    for item in row.items:
        before, after = conditions.quote_context(body, item) if body else ("", "")
        items.append(
            ItemOut(
                id=item.id,
                position=item.position,
                category=item.category,
                text=item.text,
                original_text=item.original_text,
                quote=item.quote,
                before=before,
                after=after,
                status=item.status,
                reviewed_by=item.reviewed_by,
            )
        )
    tier = order[0] if order else None
    return ListOut(
        list_id=row.id,
        document_id=row.document_id,
        title=doc.title,
        url=doc.url,
        publish_date=doc.publish_date,
        status=row.status,
        tier=tier,
        tier_label=TIER_LABELS.get(tier) if tier is not None else None,
        due=date.fromordinal(order[1]) if order and order[0] == 0 else None,
        dropped=row.dropped,
        current=current,
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        items=items,
    )


def _lock_list(session: Session, list_id: int) -> ConditionList:
    row = session.scalar(
        select(ConditionList)
        .where(ConditionList.id == list_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到这份条件清单")
    return row


def _editable(session: Session, row: ConditionList) -> None:
    if row.status == "confirmed":
        raise HTTPException(status_code=409, detail="这份清单已确认，先撤回确认再修改")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="这份清单已被新版正文替代，不能再核对")
    if not conditions.is_current(session, row):
        raise HTTPException(status_code=409, detail="原文已变化，等按新正文重新抽取后再核对")


def _lock_item(session: Session, item_id: int) -> tuple[ConditionList, ConditionItem]:
    """先锁清单再锁条目（和整份确认同一顺序），核对和确认不会交错。"""
    list_id = session.scalar(select(ConditionItem.list_id).where(ConditionItem.id == item_id))
    if list_id is None:
        raise HTTPException(status_code=404, detail="没有找到这条条件")
    row = _lock_list(session, list_id)
    _editable(session, row)
    item = session.scalar(
        select(ConditionItem)
        .where(ConditionItem.id == item_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return row, item


def router(require_admin, audit) -> APIRouter:
    def no_store(response: Response):
        response.headers["Cache-Control"] = "no-store"

    routes = APIRouter(
        prefix="/api/admin/conditions",
        dependencies=[Depends(require_admin), Depends(no_store)],
    )

    @routes.get("/count")
    def count(session: SessionDep) -> dict[str, int]:
        return {"pending": conditions.pending_count(session)}

    @routes.get("", response_model=list[ListOut])
    def queue(
        session: SessionDep,
        request: Request,
        state: Literal["pending", "confirmed"] = "pending",
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> list[ListOut]:
        if state == "pending":
            out = [
                _out(session, row, order)
                for row, order in conditions.review_queue(session, limit=limit)
            ]
        else:
            out = [
                _out(session, row, None) for row in conditions.confirmed_lists(session, limit=limit)
            ]
        audit(request, "conditions_read", state=state, count=len(out))
        return out

    def _decide(item_id: int, session: Session, reviewer: str, status: str, **changes) -> dict:
        row, item = _lock_item(session, item_id)
        before = {"status": item.status, "text": item.text, "category": item.category}
        for key, value in changes.items():
            setattr(item, key, value)
        item.status = status
        item.reviewed_by = reviewer if status != "pending" else None
        item.reviewed_at = datetime.now(UTC) if status != "pending" else None
        session.commit()
        return {
            "list_id": row.id,
            "document_id": row.document_id,
            "before": before,
            "after": {"status": item.status, "text": item.text, "category": item.category},
        }

    @routes.post("/items/{item_id}/approve")
    def approve(item_id: int, body: ReviewerIn, session: SessionDep, request: Request) -> dict:
        result = _decide(item_id, session, body.reviewed_by, "approved")
        audit(request, "condition_approve", item_id=item_id, reviewer=body.reviewed_by, **result)
        return {"status": "approved"}

    @routes.post("/items/{item_id}/edit")
    def edit(item_id: int, body: EditIn, session: SessionDep, request: Request) -> dict:
        text = clean_text(body.text)
        if not text:
            raise HTTPException(status_code=422, detail="条件不能为空")
        if is_judgement(text):
            raise HTTPException(status_code=422, detail="只写原文的要求，不写能否申报的判断")
        changes = {"text": text}
        if body.category is not None:
            changes["category"] = body.category
        # 原文引用（quote、quote_offset）不在可改范围内：改后的条件仍挂在同一句原文上
        result = _decide(item_id, session, body.reviewed_by, "approved", **changes)
        audit(request, "condition_edit", item_id=item_id, reviewer=body.reviewed_by, **result)
        return {"status": "approved", "text": text, "category": result["after"]["category"]}

    @routes.post("/items/{item_id}/delete")
    def delete(item_id: int, body: ReviewerIn, session: SessionDep, request: Request) -> dict:
        result = _decide(item_id, session, body.reviewed_by, "deleted")
        audit(request, "condition_delete", item_id=item_id, reviewer=body.reviewed_by, **result)
        return {"status": "deleted"}

    @routes.post("/items/{item_id}/reset")
    def reset(item_id: int, body: ReviewerIn, session: SessionDep, request: Request) -> dict:
        """撤回这一条的核对（回到待核），清单确认前随时可以。"""
        result = _decide(item_id, session, body.reviewed_by, "pending")
        audit(request, "condition_reset", item_id=item_id, reviewer=body.reviewed_by, **result)
        return {"status": "pending"}

    @routes.post("/{list_id}/confirm")
    def confirm(list_id: int, body: ReviewerIn, session: SessionDep, request: Request) -> dict:
        row = _lock_list(session, list_id)
        _editable(session, row)
        statuses = [item.status for item in row.items]
        waiting = statuses.count("pending")
        if waiting:
            raise HTTPException(
                status_code=409, detail=f"还有 {waiting} 条没核对，逐条核对后再确认"
            )
        row.status = "confirmed"
        row.confirmed_by = body.reviewed_by
        row.confirmed_at = datetime.now(UTC)
        session.commit()
        approved = statuses.count("approved")
        audit(
            request,
            "conditions_confirm",
            list_id=list_id,
            document_id=row.document_id,
            reviewer=body.reviewed_by,
            approved=approved,
            deleted=statuses.count("deleted"),
        )
        return {"status": "confirmed", "approved": approved}

    @routes.post("/{list_id}/reopen")
    def reopen(list_id: int, body: ReviewerIn, session: SessionDep, request: Request) -> dict:
        """撤回整份确认：立即不再对外，回到待核对（逐条的核对结果保留）。"""
        row = _lock_list(session, list_id)
        if row.status != "confirmed":
            raise HTTPException(status_code=409, detail="这份清单没有确认过，不用撤回")
        confirmed_by, confirmed_at = row.confirmed_by, row.confirmed_at
        row.status = "pending"
        row.confirmed_by = None
        row.confirmed_at = None
        session.commit()
        audit(
            request,
            "conditions_reopen",
            list_id=list_id,
            document_id=row.document_id,
            reviewer=body.reviewed_by,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
        )
        return {"status": "pending"}

    return routes


def console() -> HTMLResponse:
    page = (Path(__file__).resolve().parent.parent / "admin" / "conditions.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


__all__ = ["CONDITION_CATEGORIES", "console", "router"]
