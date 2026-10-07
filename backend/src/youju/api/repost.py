"""转发归组页（D-055）：同一份部里通知的各地版本，人确认后才对外按组折叠。

口令、审计日志和 /api/admin/ 前缀沿用其他核对台（由 app.py 传入 require_admin、audit）。
能做的：确认整组、撤回确认、移出某个版本（不会被自动加回）、恢复移出的版本、把几个版本拆成新组、
整组不合并、手动加入。只改 repost_* 两张表：各版本的截止日期仍在截止核对台逐条核对，不随组同步。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.classify import effective_kind
from youju.db import get_session
from youju.dedup import canonical_ids, groups_for
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentDetail,
    RepostGroup,
    RepostGroupMember,
    Source,
)
from youju.repost import labels_for

SessionDep = Annotated[Session, Depends(get_session)]
State = Literal["pending", "confirmed", "removed"]
STRENGTH_LABELS = {"strong": "强证据", "weak": "疑似"}


class ReviewerIn(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=80)

    @field_validator("reviewed_by", mode="before")
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class SplitIn(ReviewerIn):
    document_ids: list[int] = Field(min_length=1, max_length=50)


class AddIn(ReviewerIn):
    document_id: int = Field(ge=1)


class DeadlineOut(BaseModel):
    id: int
    kind: str
    date: date
    time: str | None
    status: Literal["pending", "approved", "rejected", "changed"]
    evidence: str


class MemberOut(BaseModel):
    document_id: int
    label: str
    province: str
    issuer: str
    publish_date: date | None
    title: str
    url: str
    kind: str
    evidence: str
    joined_by: str
    active: bool
    confirmed_by: str | None
    confirmed_at: datetime | None
    removed_by: str | None
    removed_at: datetime | None
    taken_down: bool
    # 这一版自己的截止候选（含同一文件的其他网址上的），各自核对，不互相套用
    deadlines: list[DeadlineOut]


class GroupOut(BaseModel):
    id: int
    strength: str
    strength_label: str
    topic: str
    upstream_title: str | None
    upstream_number: str | None
    created_by: str
    no_auto: bool
    versions: int
    pending: int
    # 已对外按组折叠：至少 2 个在组的版本已确认
    public: bool
    members: list[MemberOut]


def _hhmm(value) -> str | None:
    return value.strftime("%H:%M") if value else None


def _deadlines(session: Session, doc_ids: list[int]) -> dict[int, list[DeadlineOut]]:
    """每个版本自己的截止候选（同一文件的其他网址上的也算这一版的）。"""
    members = groups_for(session, doc_ids)
    owner = {i: master for master, ids in members.items() for i in ids}
    out: dict[int, list[DeadlineOut]] = defaultdict(list)
    seen: set[tuple] = set()
    for row in session.scalars(
        select(DocumentDeadline)
        .where(DocumentDeadline.document_id.in_(list(owner)))
        .order_by(DocumentDeadline.candidate_date, DocumentDeadline.id)
    ):
        decided = row.status in ("approved", "changed")
        day = row.approved_date if decided and row.approved_date else row.candidate_date
        clock = row.approved_time if decided else row.candidate_time
        master = owner[row.document_id]
        key = (master, row.kind, day, clock, row.status)
        if key in seen:
            continue
        seen.add(key)
        out[master].append(
            DeadlineOut(
                id=row.id,
                kind=row.kind,
                date=day,
                time=_hhmm(clock),
                status=row.status,
                evidence=row.evidence,
            )
        )
    return out


def _group_rows(session: Session, groups: list[RepostGroup]) -> list[GroupOut]:
    if not groups:
        return []
    rows = session.scalars(
        select(RepostGroupMember)
        .where(RepostGroupMember.group_id.in_([g.id for g in groups]))
        .order_by(RepostGroupMember.document_id)
    ).all()
    doc_ids = [row.document_id for row in rows]
    docs = {
        doc.id: (doc, src, detail)
        for doc, src, detail in session.execute(
            select(Document, Source, DocumentDetail)
            .join(Source, Source.id == Document.source_id)
            .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
            .where(Document.id.in_(doc_ids))
        )
    }
    deadlines = _deadlines(session, doc_ids)
    by_group: dict[int, list[RepostGroupMember]] = defaultdict(list)
    for row in rows:
        by_group[row.group_id].append(row)
    out = []
    for group in groups:
        members = by_group.get(group.id, [])
        labels = labels_for({m.document_id: docs[m.document_id][1] for m in members})
        items = []
        for m in members:
            doc, src, detail = docs[m.document_id]
            items.append(
                MemberOut(
                    document_id=doc.id,
                    label=labels[doc.id],
                    province="国家部委" if src.province == "国家" else src.province,
                    issuer=detail.issuer if detail and detail.issuer else src.dept_full_name,
                    publish_date=doc.publish_date,
                    title=doc.title,
                    url=doc.url,
                    kind=effective_kind(doc.title, doc.kind_override),
                    evidence=m.evidence,
                    joined_by=m.joined_by,
                    active=m.active,
                    confirmed_by=m.confirmed_by,
                    confirmed_at=m.confirmed_at,
                    removed_by=m.removed_by,
                    removed_at=m.removed_at,
                    taken_down=doc.taken_down_at is not None,
                    deadlines=deadlines.get(doc.id, []),
                )
            )
        # 在组的按发布日期排，移出的放最后
        items.sort(key=lambda m: (not m.active, m.publish_date or date.max, m.document_id))
        active = [m for m in items if m.active]
        out.append(
            GroupOut(
                id=group.id,
                strength=group.strength,
                strength_label=STRENGTH_LABELS[group.strength],
                topic=group.topic,
                upstream_title=group.upstream_title,
                upstream_number=group.upstream_number,
                created_by=group.created_by,
                no_auto=group.no_auto,
                versions=len(active),
                pending=sum(1 for m in active if m.confirmed_at is None),
                public=sum(1 for m in active if m.confirmed_at is not None) >= 2,
                members=items,
            )
        )
    return out


def _group_ids(session: Session, state: State) -> list[int]:
    """待确认：至少 2 个在组的版本、其中有还没确认的；已确认：至少 2 个在组的版本、全部确认过；
    有移出：有人工移出的版本（可以恢复）。"""
    active = func.sum(RepostGroupMember.active)
    unconfirmed = func.sum(RepostGroupMember.active & RepostGroupMember.confirmed_at.is_(None))
    stmt = select(RepostGroupMember.group_id).group_by(RepostGroupMember.group_id)
    if state == "pending":
        stmt = stmt.having(active >= 2, unconfirmed > 0)
    elif state == "confirmed":
        stmt = stmt.having(active >= 2, unconfirmed == 0)
    else:
        stmt = stmt.having(func.sum(~RepostGroupMember.active) > 0)
    return list(session.scalars(stmt))


def pending_count(session: Session) -> int:
    """总览待办：待确认的转发组数。"""
    return len(_group_ids(session, "pending"))


def _lock_group(session: Session, group_id: int) -> tuple[RepostGroup, list[RepostGroupMember]]:
    """先锁组、再按文件编号锁组员，并读最新状态（多人同时操作不会交错）。"""
    group = session.scalar(
        select(RepostGroup)
        .where(RepostGroup.id == group_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if group is None:
        raise HTTPException(status_code=404, detail="没有找到这个转发组")
    rows = list(
        session.scalars(
            select(RepostGroupMember)
            .where(RepostGroupMember.group_id == group_id)
            .order_by(RepostGroupMember.document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    return group, rows


def _member(rows: list[RepostGroupMember], document_id: int) -> RepostGroupMember:
    for row in rows:
        if row.document_id == document_id:
            return row
    raise HTTPException(status_code=404, detail="这个版本不在本组")


def _unconfirm(row: RepostGroupMember) -> None:
    row.confirmed_by = None
    row.confirmed_at = None


def router(require_admin, audit) -> APIRouter:
    def no_store(response: Response):
        response.headers["Cache-Control"] = "no-store"

    routes = APIRouter(
        prefix="/api/admin/repost-groups",
        dependencies=[Depends(require_admin), Depends(no_store)],
    )

    @routes.get("/count")
    def count(session: SessionDep) -> dict[str, int]:
        return {"pending": pending_count(session)}

    @routes.get("", response_model=list[GroupOut])
    def queue(
        session: SessionDep,
        request: Request,
        state: State = "pending",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[GroupOut]:
        ids = _group_ids(session, state)
        groups = session.scalars(select(RepostGroup).where(RepostGroup.id.in_(ids or [-1]))).all()
        out = _group_rows(session, list(groups))
        # 最近有新版本的组排在前面
        out.sort(
            key=lambda g: (
                max((m.publish_date or date.min for m in g.members), default=date.min),
                g.id,
            ),
            reverse=True,
        )
        out = out[:limit]
        audit(request, "repost_read", state=state, count=len(out))
        return out

    @routes.post("/{group_id}/confirm")
    def confirm(group_id: int, body: ReviewerIn, session: SessionDep, request: Request) -> dict:
        """确认整组：组里在组的版本对外按组折叠（各版本的截止仍各自核对）。"""
        group, rows = _lock_group(session, group_id)
        active = [row for row in rows if row.active]
        if len(active) < 2:
            raise HTTPException(status_code=409, detail="组里不到 2 个版本，不能合并")
        waiting = [row for row in active if row.confirmed_at is None]
        if not waiting:
            raise HTTPException(status_code=409, detail="这一组已经确认过")
        stamp = datetime.now(UTC)
        for row in waiting:
            row.confirmed_by = body.reviewed_by
            row.confirmed_at = stamp
        group.updated_at = stamp
        session.commit()
        ids = [row.document_id for row in waiting]
        audit(
            request,
            "repost_confirm",
            group_id=group_id,
            document_ids=ids,
            versions=[row.document_id for row in active],
            reviewer=body.reviewed_by,
        )
        return {"confirmed": ids}

    @routes.post("/{group_id}/withdraw")
    def withdraw(group_id: int, body: ReviewerIn, session: SessionDep, request: Request) -> dict:
        """撤回确认：立即不再对外合并，各版本恢复成单条；之后不再自动确认这一组。"""
        group, rows = _lock_group(session, group_id)
        confirmed = [row for row in rows if row.active and row.confirmed_at is not None]
        if not confirmed:
            raise HTTPException(status_code=409, detail="这一组没有确认过，不用撤回")
        before = {row.document_id: row.confirmed_by for row in confirmed}
        for row in confirmed:
            _unconfirm(row)
        group.no_auto = True
        group.updated_at = datetime.now(UTC)
        session.commit()
        audit(
            request,
            "repost_withdraw",
            group_id=group_id,
            document_ids=list(before),
            confirmed_by=before,
            reviewer=body.reviewed_by,
        )
        return {"withdrawn": list(before)}

    @routes.post("/{group_id}/members/{document_id}/remove")
    def remove(
        group_id: int, document_id: int, body: ReviewerIn, session: SessionDep, request: Request
    ) -> dict:
        """移出某个版本：它对外恢复成单条，以后归组任务不会再把它加回来。"""
        group, rows = _lock_group(session, group_id)
        row = _member(rows, document_id)
        if not row.active:
            raise HTTPException(status_code=409, detail="这个版本已经移出")
        was_confirmed = row.confirmed_by
        stamp = datetime.now(UTC)
        row.active = False
        row.removed_by = body.reviewed_by
        row.removed_at = stamp
        _unconfirm(row)
        group.no_auto = True
        group.updated_at = stamp
        session.commit()
        audit(
            request,
            "repost_remove",
            group_id=group_id,
            document_id=document_id,
            confirmed_by=was_confirmed,
            reviewer=body.reviewed_by,
        )
        return {"removed": document_id}

    @routes.post("/{group_id}/members/{document_id}/restore")
    def restore(
        group_id: int, document_id: int, body: ReviewerIn, session: SessionDep, request: Request
    ) -> dict:
        """恢复移出的版本：回到本组、待确认。"""
        group, rows = _lock_group(session, group_id)
        row = _member(rows, document_id)
        if row.active:
            raise HTTPException(status_code=409, detail="这个版本没有移出")
        row.active = True
        row.joined_by = "human"
        row.removed_by = None
        row.removed_at = None
        _unconfirm(row)
        group.updated_at = datetime.now(UTC)
        session.commit()
        audit(
            request,
            "repost_restore",
            group_id=group_id,
            document_id=document_id,
            reviewer=body.reviewed_by,
        )
        return {"restored": document_id}

    @routes.post("/{group_id}/split")
    def split(group_id: int, body: SplitIn, session: SessionDep, request: Request) -> dict:
        """拆组：选中的版本拆成新的一组（待确认），其余留在原组。两组以后都不自动确认。"""
        group, rows = _lock_group(session, group_id)
        active = {row.document_id: row for row in rows if row.active}
        picked = sorted(set(body.document_ids))
        missing = [doc_id for doc_id in picked if doc_id not in active]
        if missing:
            raise HTTPException(status_code=422, detail="只能拆出本组里在组的版本")
        if len(picked) >= len(active):
            raise HTTPException(status_code=422, detail="原组至少要留下一个版本")
        stamp = datetime.now(UTC)
        new = RepostGroup(
            strength=group.strength,
            topic=group.topic,
            upstream_title=group.upstream_title,
            upstream_number=group.upstream_number,
            created_by="human",
            no_auto=True,
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(new)
        session.flush()
        for doc_id in picked:
            row = active[doc_id]
            row.group_id = new.id
            row.joined_by = "human"
            _unconfirm(row)
        group.no_auto = True
        group.updated_at = stamp
        session.commit()
        audit(
            request,
            "repost_split",
            group_id=group_id,
            new_group_id=new.id,
            document_ids=picked,
            reviewer=body.reviewed_by,
        )
        return {"group_id": group_id, "new_group_id": new.id, "document_ids": picked}

    @routes.post("/{group_id}/reject")
    def reject(group_id: int, body: ReviewerIn, session: SessionDep, request: Request) -> dict:
        """整组不合并（不是同一件事）：全部版本移出、对外各自单条，以后不会被自动归回这一组。"""
        group, rows = _lock_group(session, group_id)
        active = [row for row in rows if row.active]
        if not active:
            raise HTTPException(status_code=409, detail="这一组已经没有在组的版本")
        stamp = datetime.now(UTC)
        for row in active:
            row.active = False
            row.removed_by = body.reviewed_by
            row.removed_at = stamp
            _unconfirm(row)
        group.no_auto = True
        group.updated_at = stamp
        session.commit()
        ids = [row.document_id for row in active]
        audit(
            request, "repost_reject", group_id=group_id, document_ids=ids, reviewer=body.reviewed_by
        )
        return {"removed": ids}

    @routes.post("/{group_id}/members")
    def add(group_id: int, body: AddIn, session: SessionDep, request: Request) -> dict:
        """手动加入一份文件（同一文件的其他网址按主文件算），待确认。"""
        group, rows = _lock_group(session, group_id)
        if session.get(Document, body.document_id) is None:
            raise HTTPException(status_code=404, detail="没有找到这份文件")
        doc_id = canonical_ids(session, [body.document_id]).get(body.document_id, body.document_id)
        row = session.scalar(
            select(RepostGroupMember)
            .where(RepostGroupMember.document_id == doc_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is not None and row.active:
            where = "本组" if row.group_id == group_id else f"转发组 #{row.group_id}"
            raise HTTPException(status_code=409, detail=f"这份文件已在{where}里")
        stamp = datetime.now(UTC)
        if row is None:
            row = RepostGroupMember(document_id=doc_id, group_id=group_id, joined_at=stamp)
            session.add(row)
        previous = row.group_id
        row.group_id = group_id
        row.active = True
        row.joined_by = "human"
        row.evidence = "人工加入"
        row.removed_by = None
        row.removed_at = None
        _unconfirm(row)
        group.updated_at = stamp
        session.commit()
        audit(
            request,
            "repost_add",
            group_id=group_id,
            document_id=doc_id,
            from_group_id=previous if previous != group_id else None,
            reviewer=body.reviewed_by,
        )
        return {"added": doc_id}

    return routes


def console() -> HTMLResponse:
    page = (Path(__file__).resolve().parent.parent / "admin" / "repost.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


__all__ = ["console", "pending_count", "router"]
