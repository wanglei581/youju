"""小程序"本期清单"（公众号方案 PR #287 第 3 件事）：公开只读，只给已发布的周报。

公众号正文每省只列 5 条、不能放政府网站链接，这一页列这一期的全部条目，可以按省份筛，
每条点进详情看原文。条目是哪些、在哪一栏，按发布时的快照（DigestIssue.items）；标题、
发文机关、截止日期按现在的数据算，和小程序其他列表同一口径（v1._items）——日期后来改了
显示新的，撤回核对的不再写日期。撤下或不再公开的条目不返回（v1._base 的 public_condition，
加上周报本身的一级来源口径），只给条数。草稿、撤回的和临期提醒、名单速报一律 404。
条目分页给（每页最多 200 条，可按省份筛），一期再多也不会超过云调用 1000 KB 的回包上限。
"""

from __future__ import annotations

import json
import time as _time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from youju import weekly
from youju.api import v1
from youju.classify import effective_kind
from youju.dedup import canonical_ids
from youju.models import DigestIssue, DigestProfile, Document, Source
from youju.report_revision import publication_revision

router = APIRouter(prefix="/api/v1", tags=["小程序"])

# 每页条数：每条约 0.7 KB；另按实际字节数截页（标题、网址特别长时少给几条），回包不超过云调用
# 1000 KB 的上限。小程序按实际取到的条数接着取下一页。
PAGE_MAX = 200
PAGE_DEFAULT = 100
PAGE_BYTES = 600_000
# 公众号推送后短时间内很多人打开同一期：按期、日期、截止/下架版本和来源公开级别缓存 60 秒。
CACHE_TTL = 60
CACHE_MAX = 50
_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_SECTIONS = {key: n for n, key in enumerate(weekly.SECTION_ORDER)}
_LEGACY_SECTIONS = {"申报通知": "applications", "名单": "lists", "政策": "policies"}


def _published(session, issue_id: int) -> DigestIssue | None:
    return session.scalar(
        select(DigestIssue).where(
            DigestIssue.id == issue_id,
            DigestIssue.kind == "weekly",
            DigestIssue.status == "published",
        )
    )


@router.get("/digests/latest")
def latest_digest(session: v1.SessionDep) -> dict[str, Any]:
    """ "我的 → 本周周报"：最近一期已发布的全国版周报（生成时不限省份、不筛关键词，见 scope）。

    按期刊上记下的范围找，方案后来改了地区或关键词也不影响已发布的期（Codex 第二轮）。"""
    issue = session.scalar(
        select(DigestIssue)
        .where(
            DigestIssue.kind == "weekly",
            DigestIssue.status == "published",
            DigestIssue.scope == "全国",
        )
        .order_by(DigestIssue.week_start.desc(), DigestIssue.id.desc())
        .limit(1)
    )
    if issue is None:
        raise HTTPException(status_code=404, detail="还没有发布的周报")
    return {
        "id": issue.id,
        "title": issue.title,
        "weekStart": issue.week_start.isoformat(),
        "weekEnd": issue.week_end.isoformat(),
    }


def _within_budget(items: list[dict]) -> list[dict]:
    """按序列化后的字节数截页：至少给一条，累计超过 PAGE_BYTES 就停（Codex 第二轮）。"""
    page, size = [], 0
    for item in items:
        size += len(json.dumps(item, ensure_ascii=False, default=str).encode())
        if page and size > PAGE_BYTES:
            break
        page.append(item)
    return page


def _source_revision(session) -> tuple:
    """来源公开级别的版本：一级来源改成只存题录或按授权后，缓存立即失效（来源表只有几百行）。"""
    return tuple(
        session.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(func.crc32(func.concat_ws("|", Source.id, Source.rights_level))), 0
                ),
            )
        ).one()
    )


def _entries(session, issue: DigestIssue) -> list[dict]:
    """快照里的条目；本功能上线前生成的期刊没有 items，按正文里出现的文件和现在的类型给栏目。"""
    if issue.items is not None:
        return [entry for entry in issue.items if isinstance(entry, dict) and entry.get("id")]
    rows = session.execute(
        select(Document.id, Document.title, Document.kind_override, Source.province)
        .join(Source, Source.id == Document.source_id)
        .where(Document.id.in_(issue.document_ids))
    ).all()
    entries = []
    for row in rows:
        section = _LEGACY_SECTIONS.get(effective_kind(row.title, row.kind_override))
        if section:
            entries.append({"id": row.id, "section": section, "province": row.province})
    return entries


def _in_scope(profile: DigestProfile | None, doc: Document, src: Source) -> bool:
    """方案的地区和关键词（和生成周报时一样：国家部委始终包含，标题含任一关键词）。"""
    if profile is None:
        return False
    if profile.provinces and src.province not in (weekly.NATIONAL, *profile.provinces):
        return False
    return not profile.keywords or any(word in doc.title for word in profile.keywords)


def _payload(session, issue: DigestIssue) -> dict[str, Any]:
    entries = _entries(session, issue)
    mapping = canonical_ids(session, [int(entry["id"]) for entry in entries])
    # 快照里的文件后来成了别的文件的副本：按现在的主文件列一次（详情页也是这样打开），
    # 但新的主文件要仍在方案的地区和关键词范围里，否则算不再公开、不列。
    snapshot: dict[int, dict] = {}
    for entry in entries:
        original = int(entry["id"])
        snapshot.setdefault(mapping.get(original, original), entry)
    originals = {int(entry["id"]) for entry in entries}
    moved = {master for master in snapshot if master not in originals}
    ids = list(snapshot)
    rows = (
        session.execute(v1._base().where(Document.id.in_(ids), Source.rights_level == 1)).all()
        if ids
        else []
    )
    profile = session.get(DigestProfile, issue.profile_id) if moved else None
    public = {
        doc.id: (doc, src)
        for doc, src in rows
        if weekly.safe_url(doc.url) and (doc.id not in moved or _in_scope(profile, doc, src))
    }
    kept = [doc_id for doc_id in ids if doc_id in public]
    items = v1._items(session, [public[doc_id] for doc_id in kept])
    for doc_id, item in zip(kept, items, strict=True):
        item["section"] = snapshot[doc_id].get("section") or "applications"
    counts: dict[str, int] = {}
    for item in items:
        counts[item["province"]] = counts.get(item["province"], 0) + 1
    # 省份的先后和公众号正文一样：国家部委在前，其余按本期条数从多到少。
    order = sorted(
        counts,
        key=lambda province: (
            province != weekly.NATIONAL,
            -counts[province],
            weekly.PROVINCE_ORDER.get(province, len(weekly.PROVINCE_ORDER)),
            province,
        ),
    )
    rank = {province: n for n, province in enumerate(order)}
    position = {doc_id: n for n, doc_id in enumerate(kept)}
    items.sort(
        key=lambda item: (
            rank[item["province"]],
            _SECTIONS.get(item["section"], len(_SECTIONS)),
            position[int(item["id"])],
        )
    )
    return {
        "id": issue.id,
        "title": issue.title,
        "weekStart": issue.week_start.isoformat(),
        "weekEnd": issue.week_end.isoformat(),
        "dataAt": issue.generated_at.isoformat(),
        "counts": issue.counts,
        "provinces": [
            {"province": province, "name": v1._province_name(province), "count": counts[province]}
            for province in order
        ],
        "items": items,
        # 已下架或不再公开、这里不列的条数（只给数字，不给标题和原因）
        "hidden": len(ids) - len(kept),
    }


@router.get("/digests/{issue_id}")
def digest(
    issue_id: int,
    session: v1.SessionDep,
    province: Annotated[
        str, Query(max_length=32, description="只看一个省（国家部委写'国家'）")
    ] = "",
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
) -> dict[str, Any]:
    """本期清单：一期已发布周报的全部条目（按省份分组的顺序），每条和列表行同一格式；分页给。"""
    issue = _published(session, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="这一期周报不存在，或已撤回")
    key = (
        issue.id,
        issue.generated_at,
        v1.now().astimezone(v1._TZ).date(),
        publication_revision(session, "deadlines"),
        _source_revision(session),
    )
    cached = _CACHE.get(key)
    current = _time.monotonic()
    if cached and current - cached[0] < CACHE_TTL:
        payload = cached[1]
    else:
        payload = _payload(session, issue)
        if len(_CACHE) >= CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (current, payload)
    items = payload["items"]
    if province:
        items = [item for item in items if item["province"] == province]
    return {
        **{name: value for name, value in payload.items() if name != "items"},
        "province": province,
        "total": len(items),
        "offset": offset,
        "items": _within_budget(items[offset : offset + limit]),
    }
