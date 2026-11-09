"""把 docs/数据来源/全国来源清单 CSV 导入 sources 表。

CSV 是批量导入来源的依据：重复导入会按网址更新清单里各来源的描述、抓取方式和启用状态，
但不会改动健康状态、检查记录和抓取规则。后台"来源接入"页（来源接入 Agent，D-025）加的来源
只在数据库里，重复导入 CSV 不会删除或停用它们。
"""

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.models import URL_KEY_MAX, Source
from youju.regions import district_parent_city
from youju.sources.onboard import checked_city, checked_district

# 按"验证状态"的开头决定抓取方式，以及本迭代是否启用
_MODES: list[tuple[str, str, bool]] = [
    ("已验证", "static", True),
    # 全国覆盖盘点试读通过的新栏目：能直接读，但先不启用，等分批核对后在后台打开
    ("试跑可读", "static", False),
    ("需浏览器渲染", "browser", False),  # 浏览器渲染在后续迭代实现
    ("已访问", "manual", False),  # 需要人工确认列表页或找列表接口
    ("本机无法访问", "manual", False),
    ("重复入口", "manual", False),  # 与另一个栏目内容相同，不重复抓取
]


@dataclass
class ImportSummary:
    created: int = 0
    updated: int = 0
    enabled: int = 0
    disabled: int = 0
    skipped: int = 0  # only_new 时跳过的已有来源
    existing: list[str] = field(default_factory=list)  # 清单里库里已经有的网址（更新或跳过的）


# 这些状态的来源重导入时不改启用开关（新建时不启用，之后由后台分批打开）
_KEEP_ENABLED = ("试跑可读",)
# 状态里出现这些词，说明网站明确不欢迎脚本访问：一律停用，不用浏览器渲染等方式绕过
_REFUSAL_WORDS = ("拒绝", "防护", "拦截", "robots 禁止")


def mode_for(verification_status: str) -> tuple[str, bool]:
    if any(word in verification_status for word in _REFUSAL_WORDS):
        return "blocked", False
    for prefix, mode, enabled in _MODES:
        if verification_status.startswith(prefix):
            return mode, enabled
    return "manual", False


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def import_sources(
    session: Session, csv_path: Path, only_new: bool = False, dry_run: bool = False
) -> ImportSummary:
    """only_new：库里已有的网址一律不碰（全国覆盖分批上线用，不改已经在收的来源）；
    dry_run：只数一数、列出库里已有的网址，不写库。"""
    summary = ImportSummary()
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    # 同一个栏目网址出现两次，后一行会覆盖前一行（省厅栏目被记成区县来源）：先查，一行都不写
    seen: dict[str, int] = {}
    dups: list[str] = []
    for line, row in enumerate(rows, start=2):
        url = (row.get("栏目URL") or "").strip()
        if url and url in seen:
            dups.append(f"第 {seen[url]} 行和第 {line} 行：{url}")
        seen.setdefault(url, line)
    if dups:
        raise ValueError("来源清单里栏目网址重复：" + "；".join(dups[:5]))
    for line, row in enumerate(rows, start=2):  # 第 1 行是表头
        url = row["栏目URL"].strip()
        if not url:
            continue
        if len(url) > URL_KEY_MAX:
            # 数据库里来源网址有唯一索引，长度有上限；清单是人工整理的，直接报错让人改
            raise ValueError(f"来源清单第 {line} 行的栏目网址超过 {URL_KEY_MAX} 字")
        status = row.get("验证状态", "").strip()
        fetch_mode, enabled = mode_for(status)
        try:
            city = checked_city(row["层级"].strip(), row.get("城市"))
            district = checked_district(row["层级"].strip(), row.get("区县"))
            if district:
                city = district_parent_city(row["省份"].strip(), city)
        except ValueError as exc:
            raise ValueError(f"来源清单第 {line} 行：{exc}") from exc
        fields = {
            "city": city,
            "district": district,
            "level": row["层级"].strip(),
            "province": row["省份"].strip(),
            "dept": row["部门"].strip(),
            "dept_full_name": row["部门全称"].strip(),
            "column_name": row["栏目名称"].strip(),
            "fetch_mode": fetch_mode,
            "enabled": enabled,
            "verification_status": status or None,
            "verified_on": _parse_day(row.get("验证日期", "")),
            "note": (row.get("备注") or "").strip() or None,
        }
        source = session.scalar(select(Source).where(Source.url == url))
        if source is not None:
            summary.existing.append(url)
        if source is not None and only_new:
            summary.skipped += 1
            continue
        if source is None:
            session.add(Source(url=url, parser_config={}, **fields))
            summary.created += 1
        else:
            if status.startswith(_KEEP_ENABLED):
                # 全国覆盖的新栏目只在新建时设为不启用；后台核对后打开的，重导入不关掉
                fields.pop("enabled")
                enabled = source.enabled
            for key, value in fields.items():
                setattr(source, key, value)
            summary.updated += 1
        if enabled:
            summary.enabled += 1
        else:
            summary.disabled += 1
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return summary
