"""覆盖看板的管理接口（D-122）：按级、按省看各状态格数，逐格看原因和来源，人工确认和按省抽查。

只给管理员用（口令和审计由管理台共用实现提供）；状态由 youju coverage-refresh 按抓取结果算，
这里不能手填"已收录"，人能改的只有三件事：确认网站无此栏目、填替代来源、备注（都写审计，记改前改后）。
确认网站无此栏目只对没认出栏目的格生效（boarddb.can_confirm_no_column）；保存后这一格当场重算。
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.db import get_session
from youju.govsites import boarddb
from youju.govsites.names import TARGET_DEPTS
from youju.models import (
    CoverageAcceptance,
    CoverageCell,
    CoverageCellSource,
    CoverageDivision,
    Source,
)
from youju.sources.onboard import PROVINCES

SessionDep = Annotated[Session, Depends(get_session)]
Level = Literal["国家级", "省级", "市级", "区县级"]
_PAGE_MAX = 500


def _safe_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return ""
    if len(value) > 512 or not value.lower().startswith(("http://", "https://")):
        raise ValueError("替代来源请填 http(s) 开头的网址，最多 512 字")
    return value


def _who(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("请填操作人")
    return value


class CellEditIn(BaseModel):
    by: str = Field(min_length=1, max_length=40)
    confirm_no_column: bool | None = None
    alt_source: str | None = None
    note: str | None = Field(default=None, max_length=300)

    @field_validator("by")
    @classmethod
    def by_trimmed(cls, value: str) -> str:
        return _who(value)

    @field_validator("alt_source")
    @classmethod
    def alt_ok(cls, value):
        return _safe_url(value)


class AcceptanceIn(BaseModel):
    by: str = Field(min_length=1, max_length=40)
    province: str
    level: Literal["国家级", "省级", "市级", "区县级", "全部"]
    sample_size: int = Field(ge=1, le=10000)
    passed: bool
    note: str | None = Field(default=None, max_length=300)

    @field_validator("by")
    @classmethod
    def by_trimmed(cls, value: str) -> str:
        return _who(value)

    @field_validator("province")
    @classmethod
    def province_ok(cls, value: str) -> str:
        if value not in PROVINCES:
            raise ValueError('请填省份简称（国家级填"国家"）')
        return value

    @model_validator(mode="after")
    def level_matches(self):
        # 国家级只有"国家"一行；省份没有国家级
        if (self.province == "国家") != (self.level == "国家级") and self.level != "全部":
            raise ValueError('国家级抽查省份填"国家"，各省不能记国家级')
        return self


def _cell_out(cell: CoverageCell, div: CoverageDivision) -> dict:
    return {
        "id": cell.id,
        "code": div.code,
        "level": div.level,
        "province": div.province,
        "city": div.city,
        "district": div.district,
        "division": div.name,
        "dept": cell.dept,
        "seq": cell.seq,
        "required": cell.required,
        "dept_full_name": cell.dept_full_name,
        "state": cell.state,
        "reason": cell.reason,
        "last_success_at": cell.last_success_at.isoformat() if cell.last_success_at else None,
        "confirmed_no_column": cell.confirmed_no_column,
        "alt_source": cell.alt_source,
        "note": cell.note,
    }


def router(require_admin, audit) -> APIRouter:
    def no_store(response: Response):
        response.headers["Cache-Control"] = "no-store"

    routes = APIRouter(
        prefix="/api/admin/coverage", dependencies=[Depends(require_admin), Depends(no_store)]
    )

    @routes.get("/summary")
    def coverage_summary(session: SessionDep, level: Level | None = None):
        return boarddb.summary(session, level)

    @routes.get("/cells")
    def cells(
        session: SessionDep,
        province: str | None = None,
        level: Level | None = None,
        city: str | None = Query(default=None, max_length=32),
        state: str | None = None,
        dept: str | None = None,
        page: int = Query(default=1, ge=1),
        size: int = Query(default=200, ge=1, le=_PAGE_MAX),
    ):
        if province is not None and province not in PROVINCES:
            raise HTTPException(422, "省份不对")
        if state is not None and state not in boarddb.BOARD_STATES:
            raise HTTPException(422, "状态不对")
        if dept is not None and dept not in TARGET_DEPTS:
            raise HTTPException(422, "部门不对")
        stmt = select(CoverageCell, CoverageDivision).join(
            CoverageDivision, CoverageDivision.code == CoverageCell.division_code
        )
        if province:
            stmt = stmt.where(CoverageDivision.province == province)
        if level:
            stmt = stmt.where(CoverageDivision.level == level)
        if city:
            stmt = stmt.where(CoverageDivision.city == city)
        if state:
            stmt = stmt.where(CoverageCell.state == state)
        if dept:
            stmt = stmt.where(CoverageCell.dept == dept)
        total = session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = session.execute(
            stmt.order_by(CoverageDivision.code, CoverageCell.dept, CoverageCell.seq)
            .offset((page - 1) * size)
            .limit(size)
        ).all()
        return {"total": total, "page": page, "cells": [_cell_out(c, d) for c, d in rows]}

    @routes.get("/cells/{cell_id}")
    def cell_detail(cell_id: int, session: SessionDep):
        row = session.execute(
            select(CoverageCell, CoverageDivision)
            .join(CoverageDivision, CoverageDivision.code == CoverageCell.division_code)
            .where(CoverageCell.id == cell_id)
        ).first()
        if row is None:
            raise HTTPException(404, "没有这一格")
        cell, div = row
        sources = session.execute(
            select(Source, CoverageCellSource.kind)
            .join(CoverageCellSource, CoverageCellSource.source_id == Source.id)
            .where(CoverageCellSource.cell_id == cell.id)
        ).all()
        out = _cell_out(cell, div)
        out.update(
            site_url=cell.site_url,
            site_type=cell.site_type,
            nav_source=cell.nav_source,
            inventory_status=cell.inventory_status,
            inventory_reason=cell.inventory_reason,
            portal_url=div.portal_url,
            portal_nav_source=div.portal_nav_source,
            columns=cell.columns or [],
            sources=[
                {
                    "id": s.id,
                    "kind": kind,
                    "url": s.url,
                    "enabled": s.enabled,
                    "health": s.health,
                    "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
                    "last_error": s.last_error,
                }
                for s, kind in sources
            ],
        )
        return out

    @routes.post("/cells/{cell_id}")
    def edit_cell(cell_id: int, body: CellEditIn, session: SessionDep, request: Request):
        row = session.execute(
            select(CoverageCell, CoverageDivision)
            .join(CoverageDivision, CoverageDivision.code == CoverageCell.division_code)
            .where(CoverageCell.id == cell_id)
            # 和 coverage-refresh 错开：锁住这一格到提交，读到的是最新的人工字段和状态
            .with_for_update(of=CoverageCell)
        ).first()
        if row is None:
            raise HTTPException(404, "没有这一格")
        cell, div = row
        changes: dict[str, dict[str, object]] = {}

        def put(field: str, value: object) -> None:
            # 只写真正变了的字段；审计记改前和改后
            before = getattr(cell, field)
            if before != value:
                changes[field] = {"before": before, "after": value}
                setattr(cell, field, value)

        if body.confirm_no_column is not None:
            if (
                body.confirm_no_column
                and not cell.confirmed_no_column
                and not boarddb.can_confirm_no_column(cell.columns, cell.state)
            ):
                raise HTTPException(
                    422, "这一格认出了栏目，或网站打不开、拒绝抓取：不能确认网站无此栏目"
                )
            if body.confirm_no_column != cell.confirmed_no_column:
                # 只在确认状态真的变了时记核验人和时间：之后只改备注不会覆盖最初的核验记录
                put("confirmed_no_column", body.confirm_no_column)
                put("confirmed_by", body.by if body.confirm_no_column else None)
                put("confirmed_at", datetime.now(UTC) if body.confirm_no_column else None)
        if body.alt_source is not None:
            put("alt_source", body.alt_source or None)
        if body.note is not None:
            put("note", body.note.strip() or None)
        if not changes:
            return {"ok": True, "changed": [], "state": cell.state, "reason": cell.reason}
        # 当场重算这一格，看板上马上看到结果（规则和 coverage-refresh 同一份）
        boarddb.refresh_cells(session, [(cell, div)])
        session.commit()
        audit(request, "coverage_cell_edit", cell_id=cell_id, by=body.by, changes=changes)
        return {"ok": True, "changed": sorted(changes), "state": cell.state, "reason": cell.reason}

    @routes.post("/acceptances", status_code=201)
    def add_acceptance(body: AcceptanceIn, session: SessionDep, request: Request):
        row = CoverageAcceptance(
            province=body.province,
            level=body.level,
            sampled_by=body.by,
            sampled_at=datetime.now(UTC),
            sample_size=body.sample_size,
            passed=body.passed,
            note=(body.note or "").strip() or None,
        )
        session.add(row)
        session.commit()
        audit(
            request,
            "coverage_acceptance",
            acceptance_id=row.id,
            province=body.province,
            level=body.level,
            by=body.by,
            sample_size=body.sample_size,
            passed=body.passed,
            note=row.note,
        )
        return {"id": row.id}

    return routes


def console() -> HTMLResponse:
    page = (Path(__file__).resolve().parent.parent / "admin" / "coverage.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})
