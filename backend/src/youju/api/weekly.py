"""周报、临期提醒和名单速报的管理接口；口令和审计由管理台共用实现提供。"""

from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from youju import weekly
from youju.db import get_session
from youju.models import DigestIssue, DigestProfile
from youju.sources.onboard import PROVINCES

SessionDep = Annotated[Session, Depends(get_session)]
Kind = Literal["weekly", "reminder", "list_flash"]


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    provinces: list[str] = Field(default_factory=list, max_length=31)
    keywords: list[str] = Field(default_factory=list, max_length=40)
    columns: dict[str, bool] = Field(default_factory=lambda: dict.fromkeys(weekly.COLUMNS, True))
    weekday: int = Field(default=1, ge=1, le=7)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def name_allowed(cls, value):
        value = value.strip()
        if not value or weekly.FORBIDDEN.search(value):
            raise ValueError("名称不能为空或包含周报禁用文字")
        return value

    @field_validator("provinces")
    @classmethod
    def provinces_allowed(cls, value):
        if any(v not in PROVINCES or v == "国家" for v in value):
            raise ValueError("请填写省份简称，国家部委始终包含，无须选择")
        return list(dict.fromkeys(value))

    @field_validator("keywords")
    @classmethod
    def keywords_allowed(cls, value):
        values = [word.strip() for word in value if word.strip()]
        if any(len(word) > 100 for word in values):
            raise ValueError("每个关键词最多 100 字")
        return list(dict.fromkeys(values))

    @field_validator("columns")
    @classmethod
    def columns_allowed(cls, value):
        if set(value) != set(weekly.COLUMNS):
            raise ValueError("请提供四个栏目开关")
        return value


class GenerateIn(BaseModel):
    profile_id: int = Field(gt=0)
    week: date | None = None
    kind: Kind = "weekly"
    # 名单速报：写哪份名单（文件编号）；公示期由人照原文填写，系统里没有核对过的公示期字段
    subject_id: int = Field(default=0, ge=0)
    period: str = Field(default="", max_length=60)
    # 填了公示期必须勾"已对照原文核对"：公示期是关键日期，未经核对不发布（AGENTS 合规红线）
    period_confirmed: bool = False

    @field_validator("week")
    @classmethod
    def monday(cls, value):
        if value:
            weekly.week_interval(value)
        return value


class StatusIn(BaseModel):
    status: Literal["draft", "published"]
    note: str = Field(default="", max_length=500)


def profile_out(profile):
    return {
        key: getattr(profile, key)
        for key in (
            "id",
            "name",
            "provinces",
            "keywords",
            "columns",
            "weekday",
            "enabled",
        )
    }


def issue_out(issue, profile_name, *, detail=False):
    out = {
        key: getattr(issue, key)
        for key in (
            "id",
            "profile_id",
            "kind",
            "subject_id",
            "week_start",
            "week_end",
            "title",
            "counts",
            "generated_at",
            "status",
            "note",
        )
    }
    out["profile_name"] = profile_name
    if detail:
        out.update(
            body_html=issue.body_html,
            body_text=issue.body_text,
            body_text_with_links=issue.body_text_with_links,
            # 公众号编辑器里插入小程序链接时填的页面路径（本期清单要先标为已发布）
            paths=weekly.page_paths(issue),
            period=(issue.items or [{}])[0].get("period", "") if issue.kind == "list_flash" else "",
        )
    return out


def router(require_admin, audit) -> APIRouter:
    def no_store(response: Response):
        response.headers["Cache-Control"] = "no-store"

    routes = APIRouter(
        prefix="/api/admin/weekly", dependencies=[Depends(require_admin), Depends(no_store)]
    )

    def available(session, issue):
        if not weekly.issue_available(session, issue):
            raise HTTPException(409, "内容已下架或来源不再公开，请撤回已发布标记后重新生成")

    @routes.get("/profiles")
    def profiles(session: SessionDep, request: Request):
        rows = session.scalars(select(DigestProfile).order_by(DigestProfile.id)).all()
        audit(request, "weekly_profiles_list", count=len(rows))
        return {
            "items": [profile_out(row) for row in rows],
            "provinces": [province for province in PROVINCES if province != "国家"],
        }

    def save_profile(session, body, profile=None):
        if profile is None:
            profile = DigestProfile()
            session.add(profile)
        for key, value in body.model_dump().items():
            setattr(profile, key, value)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "方案名称已存在") from None
        return profile

    @routes.post("/profiles", status_code=201)
    def create_profile(body: ProfileIn, session: SessionDep, request: Request):
        profile = save_profile(session, body)
        audit(request, "weekly_profile_create", profile_id=profile.id)
        return profile_out(profile)

    @routes.put("/profiles/{profile_id}")
    def edit_profile(profile_id: int, body: ProfileIn, session: SessionDep, request: Request):
        profile = session.scalar(
            select(DigestProfile).where(DigestProfile.id == profile_id).with_for_update()
        )
        if profile is None:
            raise HTTPException(404, "周报方案不存在")
        profile = save_profile(session, body, profile)
        audit(request, "weekly_profile_update", profile_id=profile.id)
        return profile_out(profile)

    @routes.get("/issues")
    def issues(
        session: SessionDep,
        request: Request,
        profile_id: int | None = None,
        kind: Kind | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        stmt = select(DigestIssue, DigestProfile.name).join(DigestProfile)
        if profile_id:
            stmt = stmt.where(DigestIssue.profile_id == profile_id)
        if kind:
            stmt = stmt.where(DigestIssue.kind == kind)
        total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = session.execute(
            stmt.order_by(DigestIssue.week_start.desc(), DigestIssue.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        audit(request, "weekly_issues_list", profile_id=profile_id, count=len(rows))
        return {"items": [issue_out(issue, name) for issue, name in rows], "total": total}

    @routes.get("/issues/{issue_id}")
    def detail(issue_id: int, session: SessionDep, request: Request):
        issue = session.get(DigestIssue, issue_id)
        if issue is None:
            raise HTTPException(404, "周报不存在")
        audit(request, "weekly_issue_view", issue_id=issue.id)
        available(session, issue)
        return issue_out(issue, session.get(DigestProfile, issue.profile_id).name, detail=True)

    def run_generation(
        session, profile_id, week, request, kind="weekly", subject_id=0, period=None
    ):
        try:
            result = weekly.generate(
                session, profile_id, week, kind=kind, subject_id=subject_id, period=period
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit(
            request,
            "weekly_generate",
            profile_id=profile_id,
            week=week,
            kind=kind,
            subject_id=subject_id,
            period=bool(period),
            outcome=result.outcome,
        )
        return {
            "outcome": result.outcome,
            "reason": result.reason,
            "issue_id": result.issue.id if result.issue else None,
        }

    @routes.post("/generate")
    def generate(body: GenerateIn, session: SessionDep, request: Request):
        if body.kind == "list_flash" and body.period.strip() and not body.period_confirmed:
            raise HTTPException(422, "填了公示期：请先对照原文核对，再勾选“已对照原文核对公示期”")
        return run_generation(
            session,
            body.profile_id,
            body.week,
            request,
            body.kind,
            body.subject_id,
            # 公示期留空沿用上次填的（已填过的不用每次重填）
            (body.period.strip() or None) if body.kind == "list_flash" else None,
        )

    @routes.post("/issues/{issue_id}/regenerate")
    def regenerate(issue_id: int, session: SessionDep, request: Request):
        issue = session.get(DigestIssue, issue_id)
        if issue is None:
            raise HTTPException(404, "周报不存在")
        # 名单速报重新生成沿用上次填的公示期
        return run_generation(
            session, issue.profile_id, issue.week_start, request, issue.kind, issue.subject_id
        )

    @routes.get("/lists")
    def flash_lists(profile_id: int, session: SessionDep, request: Request):
        """名单速报选名单：近 14 天发布、在方案范围内的名单，按条数从多到少。"""
        profile = session.get(DigestProfile, profile_id)
        if profile is None:
            raise HTTPException(404, "周报方案不存在")
        items = weekly.flash_candidates(session, profile)
        audit(request, "weekly_lists", profile_id=profile_id, count=len(items))
        return {"items": items}

    @routes.post("/issues/{issue_id}/status")
    def status(issue_id: int, body: StatusIn, session: SessionDep, request: Request):
        existing = session.get(DigestIssue, issue_id)
        if existing is None:
            raise HTTPException(404, "周报不存在")
        session.scalar(
            select(DigestProfile).where(DigestProfile.id == existing.profile_id).with_for_update()
        )
        issue = session.scalar(
            select(DigestIssue)
            .where(DigestIssue.id == issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if issue is None:
            raise HTTPException(404, "周报已被重新生成移除")
        if body.status == "published":
            available(session, issue)
        before = issue.status
        issue.status, issue.note = body.status, body.note.strip()
        session.commit()
        audit(request, "weekly_status", issue_id=issue.id, before=before, after=issue.status)
        return {"status": issue.status, "note": issue.note}

    return routes


def console() -> HTMLResponse:
    page = (Path(__file__).resolve().parent.parent / "admin" / "weekly.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})
