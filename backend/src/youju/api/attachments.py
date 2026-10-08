"""附件副本的分段下载（小程序经 callContainer 调用，见 youju/attachments.py）。

callContainer 返回包上限 1000 KiB，一份附件按 900 KiB 分段取；小程序收齐后写成本机文件，
用 wx.openDocument 打开。这里只按附件编号找已登记的副本，不接受文件 ID 或路径。
"""

from __future__ import annotations

import logging
import threading
import time as clock
from collections import OrderedDict, deque
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from youju import attachments as copies
from youju.backup import BackupError
from youju.db import get_session

router = APIRouter(prefix="/api/v1", tags=["小程序"])
SessionDep = Annotated[Session, Depends(get_session)]
log = logging.getLogger("youju.api.attachments")

# 每个用户 10 分钟内最多取这么多段（约 500 MB），防止刷接口
PARTS_PER_10MIN = 600
_WINDOW = 600.0
_KEYS_MAX = 10000
_SLOTS_WAIT = 10.0
_times: OrderedDict[str, deque[float]] = OrderedDict()
_guard = threading.Lock()
# 每个实例同时最多读这么多段（每段最多 900 KiB 在内存里）
_slots = threading.BoundedSemaphore(8)


def _client() -> httpx.Client:
    """读对象存储用的连接（测试里替换成模拟的）。"""
    return httpx.Client(trust_env=False)


def _caller(request: Request) -> str:
    """限流按谁算：小程序用户按 openid；取不到（本机开发）按来源地址。"""
    from youju.api.me import request_openid

    try:
        return "u:" + request_openid(request)
    except HTTPException:
        return "ip:" + (request.client.host if request.client else "?")


def _allow(caller: str, now: float) -> bool:
    with _guard:
        times = _times.get(caller) or deque()
        while times and now - times[0] > _WINDOW:
            times.popleft()
        if len(times) >= PARTS_PER_10MIN:
            return False
        times.append(now)
        _times[caller] = times
        _times.move_to_end(caller)
        while len(_times) > _KEYS_MAX:
            _times.popitem(last=False)
        return True


@router.get("/attachments/{attachment_id}/content")
def attachment_content(
    attachment_id: int,
    session: SessionDep,
    request: Request,
    v: Annotated[str, Query(pattern=r"^[0-9a-f]{16}$", description="详情接口给的版本")],
    part: Annotated[int, Query(ge=0, lt=copies.MAX_PARTS, description="第几段，从 0 起")] = 0,
) -> Response:
    """附件副本的第 part 段（二进制）。版本对不上（副本已更新）返回 409，小程序改为复制原文链接。"""
    if not _allow(_caller(request), clock.monotonic()):
        raise HTTPException(status_code=429, detail="打开附件太频繁，请稍后再试")
    row = copies.servable_copy(session, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="这个附件暂时不能在小程序里打开")
    if not row.sha256.startswith(v):
        raise HTTPException(status_code=409, detail="附件已更新，请返回后重新打开")
    start = part * copies.PART_SIZE
    if start >= row.size:
        raise HTTPException(status_code=422, detail="分段编号超出范围")
    end = min(row.size, start + copies.PART_SIZE)
    file_id, size = row.file_id, row.size
    # 读对象存储前结束查询事务，网络请求期间不占数据库连接
    session.close()
    if not _slots.acquire(timeout=_SLOTS_WAIT):
        raise HTTPException(status_code=503, detail="现在打开附件的人多，请稍后再试")
    try:
        with _client() as client:
            data = copies.read_copy(client, file_id, start, end, size)
    except (BackupError, httpx.HTTPError) as exc:
        log.warning("读附件副本失败（附件 %s 第 %s 段）：%s", attachment_id, part, exc)
        raise HTTPException(status_code=503, detail="附件暂时取不到，请稍后再试") from None
    finally:
        _slots.release()
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )
