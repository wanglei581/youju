"""核对台的 AI 预审提示和排序：只读已有的 AI 检查记录（AICheck），不调模型、不写库。

红线：截止日期必须人工点"通过"才对外显示。这里给出的"建议通过"只用来排序和提示，
任何代码都不能据此改候选状态。
"""

from dataclasses import dataclass
from datetime import date, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.ai.deadlines import _date_span, stored_items
from youju.dedup import groups_for
from youju.extract.deadlines import may_be_same, same_moment
from youju.models import AICheck, DocumentDeadline

# 核对台优先核的范围：今天起这么多天内截止（含今天和第 30 天，和筛选"30 天内截止"一致）
SOON_DAYS = 30


@dataclass(frozen=True)
class Hint:
    """code：suggest 建议通过 / agree AI 一致但不建议直接通过 / kind_mismatch 日期一致、
    类型不同 / mismatch AI 查过、没找到这一天 / ai_only 只有 AI 找到 / unchecked AI 还没核过。
    text 是页面上的标签。"""

    code: str
    text: str


# AI 抽到的一项截止：（日期, 时刻, AI 给的类型）
Found = tuple[date, time | None, str]


def _parse_found(result: object) -> list[Found]:
    """AICheck.result 里的截止；格式不对的项跳过（只读，宁可少认，不报错）。"""
    found: list[Found] = []
    for item in stored_items(result):
        if not isinstance(item, dict):
            continue
        try:
            due = date.fromisoformat(str(item["date"]))
            clock = time.fromisoformat(item["time"]) if item.get("time") else None
        except (KeyError, TypeError, ValueError):
            continue
        found.append((due, clock, str(item.get("kind") or "")))
    return found


def ai_found_by_group(session: Session, masters: list[int]) -> dict[int, list[Found] | None]:
    """每个归并组（按主文件编号）AI 抽到的截止；组里还没有一份成功检查过的为 None。"""
    groups = groups_for(session, sorted(set(masters)))
    master_of = {member: master for master, ids in groups.items() for member in ids}
    output: dict[int, list[Found] | None] = dict.fromkeys(groups)
    if not master_of:
        return output
    checks = session.scalars(
        select(AICheck).where(
            AICheck.document_id.in_(list(master_of)),
            AICheck.task == "deadlines",
            AICheck.error.is_(None),
        )
    )
    for check in checks:
        master = master_of[check.document_id]
        output[master] = (output[master] or []) + _parse_found(check.result)
    return output


def deadline_hint(row: DocumentDeadline, found: list[Found] | None) -> Hint:
    """一条待核对候选的 AI 预审提示。"建议通过"要同时满足：规则找到的，AI 也找到同一天、时刻也一样
    （两边都没写，或都写了且相同）、AI 给的类型和规则相同，而且证据原句里写着这个日期（写了年份的
    年份也对）。日期一样、时刻只有一边写了的，提示"时刻只有一边写了"，不建议直接通过
    （D-099 期 0）。"""
    if row.origin == "ai":
        return Hint("ai_only", "仅 AI 找到")
    if found is None:
        return Hint("unchecked", "AI 未核")
    kinds = [
        kind
        for due, clock, kind in found
        if same_moment(due, clock, row.candidate_date, row.candidate_time)
    ]
    if not kinds:
        if any(
            may_be_same(due, clock, row.candidate_date, row.candidate_time)
            for due, clock, _ in found
        ):
            return Hint("agree", "日期一致，但时刻只有一边写了")
        return Hint("mismatch", "规则与 AI 不一致")
    if row.kind not in kinds:
        return Hint("kind_mismatch", f"类型不一致：AI 认为是{kinds[0] or '其他'}")
    if row.candidate_date is None or _date_span(row.evidence or "", row.candidate_date) is None:
        return Hint("agree", "AI 一致，但证据句里没写这个日期")
    if row.audit_state == "reopened":
        # 质检退回的必须由人重新核对，不给"建议通过"
        return Hint("agree", "AI 一致（质检退回，请重核）")
    return Hint("suggest", "建议通过")


def deadline_hints(session: Session, rows: list[tuple[DocumentDeadline, int]]) -> dict[int, Hint]:
    """一批候选（候选, 主文件编号）的提示：{候选编号: 提示}。只读。"""
    found = ai_found_by_group(session, [master for _, master in rows])
    return {row.id: deadline_hint(row, found.get(master)) for row, master in rows}


def review_rank(due: date | None, hint: Hint, today: date) -> int:
    """核对顺序的分档：0 三十天内且建议通过；1 三十天内其余；2 三十天以后；3 已过期；4 没有日期。"""
    if due is None:
        return 4
    if due < today:
        return 3
    if due <= today + timedelta(days=SOON_DAYS):
        return 0 if hint.code == "suggest" else 1
    return 2


def order_key(row: DocumentDeadline, hint: Hint, today: date) -> str:
    """排序键（按字符串升序就是核对顺序）：分档、截止日期升序、编号升序。也用作翻页游标。"""
    due = row.candidate_date
    rank = review_rank(due, hint, today)
    return f"{rank}|{(due or date.max).isoformat()}|{row.id:012d}"
