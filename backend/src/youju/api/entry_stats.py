"""来源统计的接口（规则见 youju/entry_stats.py）：小程序上报一次打开，管理台看每周汇总。

上报接口只在 admin 服务上开放（app._role_guard），只认经云调用来的 POST
（带平台注入的 X-WX-SOURCE），不读任何身份头。请求体只有三个字段、不到 1 KB：
在解析之前就按实际字节数限长，多带字段的拒收；字段值不认得的不拒收，清洗成 0 或空再计数。
"""

import threading
import time as clock
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

# 限流（进程内按分钟计；admin 固定 1 个实例、1 个进程）：admin 开着公网，
# 公网请求可能自带 X-WX-SOURCE（部署文档第五节第 7 条），伪造的上报最多只能刷计数，
# 碰不到任何用户数据。来源 IP 由平台写入、伪造不了（9/30 实测），按它限：
# 同一个 IP 每分钟最多 PER_IP_PER_MINUTE 次，全部来源每分钟最多 TOTAL_PER_MINUTE 次，
# 超出的回 429、不计数，也不读请求体。正常用户一次冷启动只发一次；
# 运营商共用出口 IP 时一分钟内也到不了上限。
PER_IP_PER_MINUTE = 20
TOTAL_PER_MINUTE = 600
# 一分钟内最多记这么多个 IP（内存上限）；记满后新来的 IP 这一分钟不收
MAX_IPS = 10000
_limit_lock = threading.Lock()
_limit: dict = {"minute": None, "ips": {}, "total": 0}

SessionDep = Annotated[Session, Depends(get_session)]


def _allowed(ip: str) -> bool:
    minute = int(clock.time() // 60)
    with _limit_lock:
        if _limit["minute"] != minute:
            _limit.update(minute=minute, ips={}, total=0)
        ips = _limit["ips"]
        used = ips.get(ip, 0)
        if (
            _limit["total"] >= TOTAL_PER_MINUTE
            or used >= PER_IP_PER_MINUTE
            or (not used and len(ips) >= MAX_IPS)
        ):
            return False
        ips[ip] = used + 1
        _limit["total"] += 1
        return True


class EntryIn(BaseModel):
    """小程序发来的一次打开：场景值、来源编号（JSON 里叫 from）、第一个页面。
    长度不另设上限（整个请求体不过 1 KB），超长、不合格的都在计数前清洗掉。"""

    model_config = ConfigDict(extra="forbid")
    scene: int
    code: str = Field(default="", alias="from")
    page: str = ""


async def _entry(request: Request) -> EntryIn:
    from youju.api.app import client_ip

    if not _allowed(client_ip(request)):
        raise HTTPException(status_code=429, detail="上报太频繁，这次不计")
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
