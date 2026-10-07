"""转发归组页（D-055）：同一份部里通知的各地版本，人确认后才对外按组折叠。

口令、审计日志和 /api/admin/ 前缀沿用其他核对台（由 app.py 传入 require_admin、audit）。
能做的：确认整组、撤回确认、移出某个版本（不会被自动加回）、恢复移出的版本、把几个版本拆成新组、
整组不合并、手动加入、处理后来归并成同一文件的版本。只改 repost_* 两张表：各版本的截止日期仍在
截止核对台逐条核对，不随组同步。

确认、撤回、拆组、整组不合并都要带上页面看到的修订号（确认还要带上看到的版本清单）：
组在这期间有变化（归组任务加了新版本、别人移出了版本）就返回 409，页面提示刷新后再操作。
加锁顺序统一为“按组编号锁组，再按文件编号锁组员”（和归组任务 youju.repost.rebuild 相同）；
仍遇到死锁或锁等待超时时返回 409，请稍后重试。
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
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
from youju.repost import bump, duplicate_members, labels_for

SessionDep = Annotated[Session, Depends(get_session)]
State = Literal["pending", "confirmed", "removed"]
STRENGTH_LABELS = {"strong": "强证据", "weak": "疑似"}
STALE = "这一组有变化（有版本加入、移出或确认状态变了），请刷新后再操作"
# MySQL 死锁、锁等待超时
_LOCK_ERRORS = {1205, 1213}


class ReviewerIn(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=80)

    @field_validator("reviewed_by", mode="before")
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class RevisionIn(ReviewerIn):
    # 页面上看到的修订号（GroupOut.revision）
    revision: int = Field(ge=0)


class ConfirmIn(RevisionIn):
    # 页面上看到的、要确认的在组版本（文件编号），必须和现在组里的在组版本完全一致
    document_ids: list[int] = Field(min_length=2, max_length=200)


class SplitIn(RevisionIn):
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
    # 后来归并成了同一文件的副本：主文件编号；conflict 为真表示主文件也有组员记录，等人处理
    merged_into: int | None = None
    conflict: bool = False
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
    revision: int
    versions: int
    pending: int
    # 后来归并成同一文件、和主文件的记录冲突、等人处理的组员数
    conflicts: int
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


@dataclass
class _Counts:
    """一个组的状态（在组 = active 且不是同一文件的副本）。"""

    active: list[int]
    pending: int
    removed: int
    conflicts: int


def _counts(
    rows: list[RepostGroupMember], duplicates: dict[int, int], member_ids: set[int]
) -> _Counts:
    active = [r.document_id for r in rows if r.active and r.document_id not in duplicates]
    return _Counts(
        active=active,
        pending=sum(
            1
            for r in rows
            if r.active and r.document_id not in duplicates and r.confirmed_at is None
        ),
        removed=sum(1 for r in rows if not r.active),
        conflicts=sum(
            1
            for r in rows
            if r.document_id in duplicates and duplicates[r.document_id] in member_ids
        ),
    )


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
    duplicates = duplicate_members(session, doc_ids)
    all_members = set(
        session.scalars(
            select(RepostGroupMember.document_id).where(
                RepostGroupMember.document_id.in_(list(duplicates.values()) or [-1])
            )
        )
    )
    deadlines = _deadlines(session, doc_ids)
    by_group: dict[int, list[RepostGroupMember]] = defaultdict(list)
    for row in rows:
        by_group[row.group_id].append(row)
    out = []
    for group in groups:
        members = by_group.get(group.id, [])
        counts = _counts(members, duplicates, all_members)
        labels = labels_for({m.document_id: docs[m.document_id][1] for m in members})
        items = []
        for m in members:
            doc, src, detail = docs[m.document_id]
            merged_into = duplicates.get(doc.id)
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
                    merged_into=merged_into,
                    conflict=merged_into is not None and merged_into in all_members,
                    deadlines=deadlines.get(doc.id, []),
                )
            )
        # 在组的按发布日期排，移出的放最后
        items.sort(key=lambda m: (not m.active, m.publish_date or date.max, m.document_id))
        confirmed = sum(
            1
            for m in members
            if m.active and m.document_id not in duplicates and m.confirmed_at is not None
        )
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
                revision=group.revision,
                versions=len(counts.active),
                pending=counts.pending,
                conflicts=counts.conflicts,
                public=confirmed >= 2,
                members=items,
            )
        )
    return out


def _group_ids(session: Session, state: State) -> list[int]:
    """待确认：至少 2 个在组的版本、其中有还没确认的，或有归并成同一文件、等人处理的组员；
    已确认：至少 2 个在组的版本、全部确认过、没有等人处理的；
    有移出：有人工移出的版本（可以恢复）。"""
    rows = session.scalars(select(RepostGroupMember)).all()
    duplicates = duplicate_members(session, [row.document_id for row in rows])
    member_ids = {row.document_id for row in rows}
    by_group: dict[int, list[RepostGroupMember]] = defaultdict(list)
    for row in rows:
        by_group[row.group_id].append(row)
    picked = []
    for group_id, members in by_group.items():
        counts = _counts(members, duplicates, member_ids)
        if state == "pending":
            hit = (len(counts.active) >= 2 and counts.pending > 0) or counts.conflicts > 0
        elif state == "confirmed":
            hit = len(counts.active) >= 2 and counts.pending == 0 and counts.conflicts == 0
        else:
            hit = counts.removed > 0
        if hit:
            picked.append(group_id)
    return picked


def pending_count(session: Session) -> int:
    """总览待办：待确认的转发组数。"""
    return len(_group_ids(session, "pending"))


def _lock_groups(
    session: Session, group_ids: list[int]
) -> tuple[dict[int, RepostGroup], list[RepostGroupMember]]:
    """按组编号锁组、再按文件编号锁这些组的组员，并读最新状态（和归组任务同一顺序）。"""
    ids = sorted(set(group_ids))
    groups = {
        g.id: g
        for g in session.scalars(
            select(RepostGroup)
            .where(RepostGroup.id.in_(ids))
            .order_by(RepostGroup.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    if group_ids and group_ids[0] not in groups:
        raise HTTPException(status_code=404, detail="没有找到这个转发组")
    rows = list(
        session.scalars(
            select(RepostGroupMember)
            .where(RepostGroupMember.group_id.in_(list(groups) or [-1]))
            .order_by(RepostGroupMember.document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    return groups, rows


def _lock_group(session: Session, group_id: int) -> tuple[RepostGroup, list[RepostGroupMember]]:
    groups, rows = _lock_groups(session, [group_id])
    return groups[group_id], rows


def _check_revision(group: RepostGroup, revision: int) -> None:
    if group.revision != revision:
        raise HTTPException(status_code=409, detail=STALE)


def _member(rows: list[RepostGroupMember], document_id: int) -> RepostGroupMember:
    for row in rows:
        if row.document_id == document_id:
            return row
    raise HTTPException(status_code=404, detail="这个版本不在本组")


def _unconfirm(row: RepostGroupMember) -> None:
    row.confirmed_by = None
    row.confirmed_at = None


@contextlib.contextmanager
def _busy(session: Session):
    """遇到死锁或锁等待超时：回滚，返回 409 请稍后重试（不在服务端自动重试，免得重复执行）。"""
    try:
        yield
    except OperationalError as exc:
        code = getattr(getattr(exc, "orig", None), "args", (None,))[0]
        if code not in _LOCK_ERRORS:
            raise
        session.rollback()
        raise HTTPException(status_code=409, detail="别人正在处理这一组，请稍后刷新重试") from exc


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

    def _active(session: Session, rows: list[RepostGroupMember]) -> list[RepostGroupMember]:
        duplicates = duplicate_members(session, [row.document_id for row in rows])
        return [row for row in rows if row.active and row.document_id not in duplicates]

    @routes.post("/{group_id}/confirm")
    def confirm(group_id: int, body: ConfirmIn, session: SessionDep, request: Request) -> dict:
        """确认整组：只确认页面上看到的那些版本；组在这期间有变化就不确认（409）。
        确认后这些版本对外按组折叠（各版本的截止仍各自核对）。"""
        with _busy(session):
            group, rows = _lock_group(session, group_id)
            _check_revision(group, body.revision)
            active = _active(session, rows)
            if {row.document_id for row in active} != set(body.document_ids):
                raise HTTPException(status_code=409, detail=STALE)
            if len(active) < 2:
                raise HTTPException(status_code=409, detail="组里不到 2 个版本，不能合并")
            conflicts = _group_rows(session, [group])[0].conflicts
            if conflicts:
                raise HTTPException(
                    status_code=409, detail="组里有后来归并成同一文件的版本，先处理再确认"
                )
            waiting = [row for row in active if row.confirmed_at is None]
            if not waiting:
                raise HTTPException(status_code=409, detail="这一组已经确认过")
            stamp = datetime.now(UTC)
            for row in waiting:
                row.confirmed_by = body.reviewed_by
                row.confirmed_at = stamp
            bump(group, stamp)
            session.commit()
        ids = [row.document_id for row in waiting]
        audit(
            request,
            "repost_confirm",
            group_id=group_id,
            document_ids=ids,
            versions=[row.document_id for row in active],
            revision=body.revision,
            reviewer=body.reviewed_by,
        )
        return {"confirmed": ids, "revision": group.revision}

    @routes.post("/{group_id}/withdraw")
    def withdraw(group_id: int, body: RevisionIn, session: SessionDep, request: Request) -> dict:
        """撤回确认：立即不再对外合并，各版本恢复成单条；这一组和组员以后不被归组任务清理，
        也不再自动确认（no_auto）。"""
        with _busy(session):
            group, rows = _lock_group(session, group_id)
            _check_revision(group, body.revision)
            confirmed = [row for row in rows if row.active and row.confirmed_at is not None]
            if not confirmed:
                raise HTTPException(status_code=409, detail="这一组没有确认过，不用撤回")
            before = {row.document_id: row.confirmed_by for row in confirmed}
            for row in confirmed:
                _unconfirm(row)
            group.no_auto = True
            bump(group, datetime.now(UTC))
            session.commit()
        audit(
            request,
            "repost_withdraw",
            group_id=group_id,
            document_ids=list(before),
            confirmed_by=before,
            reviewer=body.reviewed_by,
        )
        return {"withdrawn": list(before), "revision": group.revision}

    @routes.post("/{group_id}/members/{document_id}/remove")
    def remove(
        group_id: int, document_id: int, body: ReviewerIn, session: SessionDep, request: Request
    ) -> dict:
        """移出某个版本：它对外恢复成单条，以后归组任务不会再把它加回来。"""
        with _busy(session):
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
            bump(group, stamp)
            session.commit()
        audit(
            request,
            "repost_remove",
            group_id=group_id,
            document_id=document_id,
            confirmed_by=was_confirmed,
            reviewer=body.reviewed_by,
        )
        return {"removed": document_id, "revision": group.revision}

    @routes.post("/{group_id}/members/{document_id}/restore")
    def restore(
        group_id: int, document_id: int, body: ReviewerIn, session: SessionDep, request: Request
    ) -> dict:
        """恢复移出的版本：回到本组、待确认。"""
        with _busy(session):
            group, rows = _lock_group(session, group_id)
            row = _member(rows, document_id)
            if row.active:
                raise HTTPException(status_code=409, detail="这个版本没有移出")
            row.active = True
            row.joined_by = "human"
            row.removed_by = None
            row.removed_at = None
            _unconfirm(row)
            bump(group, datetime.now(UTC))
            session.commit()
        audit(
            request,
            "repost_restore",
            group_id=group_id,
            document_id=document_id,
            reviewer=body.reviewed_by,
        )
        return {"restored": document_id, "revision": group.revision}

    @routes.post("/{group_id}/members/{document_id}/drop")
    def drop(
        group_id: int, document_id: int, body: ReviewerIn, session: SessionDep, request: Request
    ) -> dict:
        """去掉一条后来归并成同一文件副本的组员记录（主文件也有记录、两条冲突时，由人决定留哪条）。
        只能去掉副本的记录；去掉前的状态记进审计日志。"""
        with _busy(session):
            group, rows = _lock_group(session, group_id)
            row = _member(rows, document_id)
            canonical = duplicate_members(session, [document_id]).get(document_id)
            if canonical is None:
                raise HTTPException(status_code=409, detail="这个版本不是同一文件的副本，请用移出")
            before = {
                "active": row.active,
                "confirmed_by": row.confirmed_by,
                "removed_by": row.removed_by,
                "joined_by": row.joined_by,
            }
            session.delete(row)
            bump(group, datetime.now(UTC))
            session.commit()
        audit(
            request,
            "repost_drop_duplicate",
            group_id=group_id,
            document_id=document_id,
            canonical_id=canonical,
            before=before,
            reviewer=body.reviewed_by,
        )
        return {"dropped": document_id, "revision": group.revision}

    @routes.post("/{group_id}/split")
    def split(group_id: int, body: SplitIn, session: SessionDep, request: Request) -> dict:
        """拆组：选中的版本拆成新的一组（待确认），其余留在原组。两组以后都不自动确认。"""
        with _busy(session):
            group, rows = _lock_group(session, group_id)
            _check_revision(group, body.revision)
            active = {row.document_id: row for row in _active(session, rows)}
            picked = sorted(set(body.document_ids))
            if any(doc_id not in active for doc_id in picked):
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
                revision=0,
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
            bump(group, stamp)
            session.commit()
        audit(
            request,
            "repost_split",
            group_id=group_id,
            new_group_id=new.id,
            document_ids=picked,
            reviewer=body.reviewed_by,
        )
        return {
            "group_id": group_id,
            "new_group_id": new.id,
            "document_ids": picked,
            "revision": group.revision,
        }

    @routes.post("/{group_id}/reject")
    def reject(group_id: int, body: RevisionIn, session: SessionDep, request: Request) -> dict:
        """整组不合并（不是同一件事）：全部版本移出、对外各自单条；这个决定永久保留，
        以后不会被自动归回这一组，也不会被自动确认。"""
        with _busy(session):
            group, rows = _lock_group(session, group_id)
            _check_revision(group, body.revision)
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
            bump(group, stamp)
            session.commit()
        ids = [row.document_id for row in active]
        audit(
            request, "repost_reject", group_id=group_id, document_ids=ids, reviewer=body.reviewed_by
        )
        return {"removed": ids, "revision": group.revision}

    @routes.post("/{group_id}/members")
    def add(group_id: int, body: AddIn, session: SessionDep, request: Request) -> dict:
        """手动加入一份文件（同一文件的其他网址按主文件算），待确认。文件原来在别的组里（已移出）
        的，两个组按编号顺序一起锁，再锁组员，不会和别的操作互相等成死锁。"""
        if session.get(Document, body.document_id) is None:
            raise HTTPException(status_code=404, detail="没有找到这份文件")
        doc_id = canonical_ids(session, [body.document_id]).get(body.document_id, body.document_id)
        with _busy(session):
            # 先不加锁看它现在在哪个组，再按组编号一起锁上，锁上后核对没变
            seen = session.scalar(
                select(RepostGroupMember.group_id).where(RepostGroupMember.document_id == doc_id)
            )
            groups, rows = _lock_groups(
                session, [group_id] + ([seen] if seen is not None and seen != group_id else [])
            )
            group = groups[group_id]
            row = next((r for r in rows if r.document_id == doc_id), None)
            if row is None:
                row = session.scalar(
                    select(RepostGroupMember)
                    .where(RepostGroupMember.document_id == doc_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if row is not None:  # 锁之前被挪到了别的组
                    raise HTTPException(
                        status_code=409, detail="这份文件刚被别人处理过，请刷新后重试"
                    )
            if row is not None and row.active:
                where = "本组" if row.group_id == group_id else f"转发组 #{row.group_id}"
                raise HTTPException(status_code=409, detail=f"这份文件已在{where}里")
            stamp = datetime.now(UTC)
            previous = row.group_id if row is not None else None
            if row is None:
                row = RepostGroupMember(document_id=doc_id, group_id=group_id, joined_at=stamp)
                session.add(row)
            row.group_id = group_id
            row.active = True
            row.joined_by = "human"
            row.evidence = "人工加入"
            row.removed_by = None
            row.removed_at = None
            _unconfirm(row)
            bump(group, stamp)
            if previous is not None and previous != group_id and previous in groups:
                bump(groups[previous], stamp)
            session.commit()
        audit(
            request,
            "repost_add",
            group_id=group_id,
            document_id=doc_id,
            from_group_id=previous if previous != group_id else None,
            reviewer=body.reviewed_by,
        )
        return {"added": doc_id, "revision": group.revision}

    return routes


def console() -> HTMLResponse:
    page = (Path(__file__).resolve().parent.parent / "admin" / "repost.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


__all__ = ["console", "pending_count", "router"]
