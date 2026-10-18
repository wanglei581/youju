"""小程序"本期清单"（公众号方案 PR #287 第 3 件事）：公开只读，只给已发布的周报。

公众号正文每省只列 5 条、不能放政府网站链接，这一页列这一期的全部条目，可以按省份筛，
每条点进详情看原文。条目是哪些、在哪一栏，按发布时的快照（DigestIssue.items）；标题、
发文机关、截止日期按现在的数据算，和小程序其他列表同一口径（v1._items）——日期后来改了
显示新的，撤回核对的不再写日期。撤下或不再公开的条目不返回（v1._base 的 public_condition，
加上周报本身的一级来源口径），只给条数。草稿、撤回的和临期提醒、名单速报一律 404。
"""

from __future__ import annotations

import time as _time
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from youju import weekly
from youju.api import v1
from youju.classify import effective_kind
from youju.dedup import canonical_ids
from youju.models import DigestIssue, DigestProfile, Document, Source
from youju.report_revision import publication_revision

router = APIRouter(prefix="/api/v1", tags=["小程序"])

# 一期最多给多少条：每条约 0.7 KB，600 条约 420 KB，在云调用 1000 KB 的回包上限以内。
MAX_ITEMS = 600
# 公众号推送后短时间内很多人打开同一期：按期、日期和截止/下架版本缓存 60 秒。
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
    """ "我的 → 本周周报"：全国方案（不限省份、不筛关键词）最近一期已发布的周报。"""
    issue = session.scalar(
        select(DigestIssue)
        .join(DigestProfile, DigestProfile.id == DigestIssue.profile_id)
        .where(
            DigestIssue.kind == "weekly",
            DigestIssue.status == "published",
            func.json_length(DigestProfile.provinces) == 0,
            func.json_length(DigestProfile.keywords) == 0,
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


def _payload(session, issue: DigestIssue) -> dict[str, Any]:
    entries = _entries(session, issue)
    mapping = canonical_ids(session, [int(entry["id"]) for entry in entries])
    # 快照里的文件后来成了别的文件的副本：按现在的主文件列一次（详情页也是这样打开）。
    snapshot: dict[int, dict] = {}
    for entry in entries:
        snapshot.setdefault(mapping.get(int(entry["id"]), int(entry["id"])), entry)
    ids = list(snapshot)
    rows = (
        session.execute(v1._base().where(Document.id.in_(ids), Source.rights_level == 1)).all()
        if ids
        else []
    )
    public = {doc.id: (doc, src) for doc, src in rows if weekly.safe_url(doc.url)}
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
        "items": items[:MAX_ITEMS],
        "total": len(items),
        "truncated": len(items) > MAX_ITEMS,
        # 已下架或不再公开、这里不列的条数（只给数字，不给标题和原因）
        "hidden": len(ids) - len(kept),
    }


@router.get("/digests/{issue_id}")
def digest(issue_id: int, session: v1.SessionDep) -> dict[str, Any]:
    """本期清单：一期已发布周报的全部条目（按省份分组的顺序），每条和列表行同一格式。"""
    issue = _published(session, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="这一期周报不存在，或已撤回")
    key = (
        issue.id,
        issue.generated_at,
        v1.now().astimezone(v1._TZ).date(),
        publication_revision(session, "deadlines"),
    )
    cached = _CACHE.get(key)
    current = _time.monotonic()
    if cached and current - cached[0] < CACHE_TTL:
        return cached[1]
    payload = _payload(session, issue)
    if len(_CACHE) >= CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (current, payload)
    return payload
