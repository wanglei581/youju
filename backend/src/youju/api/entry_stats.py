"""来源统计的接口（规则见 youju/entry_stats.py）：小程序上报一次打开，管理台看每周汇总。

上报接口只在 admin 服务上开放（app._role_guard），只认经云调用来的 POST
（带平台注入的 X-WX-SOURCE），不读任何身份头。请求体只有三个字段、不到 1 KB：
在解析之前就按实际字节数限长，多带字段的拒收。
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from youju import entry_stats
from youju.api import v1
from youju.db import get_session

PATH = "/api/v1/entry-stats"
BODY_LIMIT = 1024

SessionDep = Annotated[Session, Depends(get_session)]


class EntryIn(BaseModel):
    """小程序发来的一次打开：场景值、来源编号（JSON 里叫 from）、第一个页面。"""

    model_config = ConfigDict(extra="forbid")
    scene: int = Field(ge=0, le=99999)
    code: str = Field(default="", alias="from", max_length=64)
    page: str = Field(default="", max_length=128)


async def _entry(request: Request) -> EntryIn:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            size = -1
        if size < 0:
            raise HTTPException(status_code=400, detail="请求内容大小无效")
        if size > BODY_LIMIT:
            raise HTTPException(status_code=413, detail="请求内容过大")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > BODY_LIMIT:
            raise HTTPException(status_code=413, detail="请求内容过大")
    try:
        return EntryIn.model_validate_json(bytes(body))
    except ValidationError:
        # 不回显请求内容
        raise HTTPException(status_code=422, detail="统计内容不符合要求") from None


public_router = APIRouter()


@public_router.post(PATH, status_code=204, include_in_schema=False)
def create_entry(entry: Annotated[EntryIn, Depends(_entry)], session: SessionDep) -> Response:
    entry_stats.record(
        session,
        day=v1.now().date(),
        scene=entry_stats.clean_scene(entry.scene),
        code=entry_stats.clean_code(entry.code),
        page=entry_stats.clean_page(entry.page),
    )
    return Response(status_code=204)


def admin_router(require_admin) -> APIRouter:
    def no_store(response: Response):
        response.headers["Cache-Control"] = "no-store"

    routes = APIRouter(dependencies=[Depends(require_admin), Depends(no_store)])

    @routes.get("/api/admin/entry-stats")
    def weekly(session: SessionDep, weeks: Annotated[int, Query(ge=1, le=26)] = 8) -> dict:
        return entry_stats.weekly(session, today=v1.now().date(), weeks=weeks)

    return routes


def console() -> HTMLResponse:
    page = (Path(__file__).resolve().parent.parent / "admin" / "entry_stats.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})
