"""把 docs/数据来源/全国来源清单 CSV 导入 sources 表。

CSV 是批量导入来源的依据：重复导入会按网址更新清单里各来源的描述、抓取方式和启用状态，
但不会改动健康状态、检查记录和抓取规则。后台"来源接入"页（来源接入 Agent，D-025）加的来源
只在数据库里，重复导入 CSV 不会删除或停用它们。
"""

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.models import URL_KEY_MAX, Source
from youju.sources.onboard import checked_city

# 按"验证状态"的开头决定抓取方式，以及本迭代是否启用
_MODES: list[tuple[str, str, bool]] = [
    ("已验证", "static", True),
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


def import_sources(session: Session, csv_path: Path) -> ImportSummary:
    summary = ImportSummary()
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
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
        except ValueError as exc:
            raise ValueError(f"来源清单第 {line} 行：{exc}") from exc
        fields = {
            "city": city,
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
        if source is None:
            session.add(Source(url=url, parser_config={}, **fields))
            summary.created += 1
        else:
            for key, value in fields.items():
                setattr(source, key, value)
            summary.updated += 1
        if enabled:
            summary.enabled += 1
        else:
            summary.disabled += 1
    session.commit()
    return summary
