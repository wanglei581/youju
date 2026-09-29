"""验收 A9 抽检：随机抽几份文件，重新打开原文，核对详情里的发文机关、文号、发布日期、附件名称
是不是原文里写着的。对不上的列出来，由人打开原文判断是真的错了还是写法不同。

只读数据库；抓取遵守 robots、每站间隔 3 秒（和定时抓取同一个礼貌抓取器）。
用法（在 backend/ 目录下）：
    uv run python scripts/check_details.py --sample 20 [--seed 929]
"""

import argparse
import random
import re
import sys

import lxml.html
from sqlalchemy import select

from youju.crawler.detail_service import _government_url
from youju.crawler.fetcher import PoliteFetcher
from youju.db import session_factory
from youju.dedup import visible_documents
from youju.models import Document, DocumentAttachment, DocumentDetail, Source


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _page_text(html: str) -> str:
    try:
        doc = lxml.html.document_fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return ""
    for node in doc.xpath("//script|//style|//noscript"):
        node.drop_tree()
    text = doc.text_content()
    links = " ".join(a.text_content() for a in doc.iter("a"))
    return _compact(text + links)


# 机关全称和原文里常见的简称：两种写法都算原文写着
_ALIASES = (
    ("科学技术厅", "科技厅"),
    ("科学技术局", "科技局"),
    ("科学技术委员会", "科委"),
    ("工业和信息化厅", "工信厅"),
    ("工业和信息化局", "工信局"),
    ("经济和信息化厅", "经信厅"),
    ("经济和信息化局", "经信局"),
    ("经济和信息化委员会", "经信委"),
)


def _issuer_found(issuer: str, text: str) -> bool:
    """联合发文按顿号拆开逐个找；全称、简称都算。"""
    for part in filter(None, (_compact(p) for p in issuer.split("、"))):
        forms = {part}
        for full, short in _ALIASES:
            if full in part:
                forms.add(part.replace(full, short))
        if not any(form in text for form in forms):
            return False
    return True


def _date_forms(day) -> list[str]:
    return [
        day.isoformat(),
        f"{day.year}年{day.month}月{day.day}日",
        f"{day.year}年{day.month:02d}月{day.day:02d}日",
        f"{day.year}/{day.month:02d}/{day.day:02d}",
        f"{day.year}-{day.month}-{day.day}",
        f"{day.year}.{day.month:02d}.{day.day:02d}",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--seed", type=int, default=929)
    args = parser.parse_args()
    from youju.config import get_settings

    settings = get_settings()
    with session_factory()() as session:
        # 只取用得到的字段：不依赖新版本才有的列，老版本的库也能直接抽检（只读）
        rows = session.execute(
            visible_documents(
                select(
                    Document.id,
                    Document.url,
                    Document.title,
                    Document.publish_date,
                    DocumentDetail.issuer,
                    DocumentDetail.doc_number,
                    DocumentDetail.publish_date.label("detail_date"),
                    Source.dept_full_name,
                )
                .join(DocumentDetail, DocumentDetail.document_id == Document.id)
                .join(Source, Source.id == Document.source_id)
                .where(DocumentDetail.status == "ok", Source.rights_level == 1)
            )
        ).all()
        picked = random.Random(args.seed).sample(rows, min(args.sample, len(rows)))
        attachments = {
            row.id: list(
                session.scalars(
                    select(DocumentAttachment.name).where(DocumentAttachment.document_id == row.id)
                )
            )
            for row in picked
        }
    problems = 0
    with PoliteFetcher(
        settings.user_agent,
        settings.request_timeout,
        settings.per_host_interval,
        url_allowed=_government_url,
    ) as fetcher:
        for row in picked:
            fetched = fetcher.fetch(row.url)
            if fetched.status != "ok":
                print(f"#{row.id} 原文打不开（{fetched.status}）：{row.url}")
                continue
            text = _page_text(fetched.text)
            issuer = row.issuer or row.dept_full_name
            checks = {"发文机关": _issuer_found(issuer, text)}
            if row.doc_number:
                checks["文号"] = _compact(row.doc_number) in text
            day = row.detail_date or row.publish_date
            if day:
                checks["发布日期"] = any(form in text for form in _date_forms(day))
            names = attachments[row.id]
            found = sum(1 for name in names if _compact(name) in text)
            if names:
                checks[f"附件 {found}/{len(names)}"] = found == len(names)
            bad = [k for k, ok in checks.items() if not ok]
            problems += bool(bad)
            mark = "对不上：" + "、".join(bad) if bad else "全部一致"
            print(
                f"#{row.id} {row.title[:24]}｜机关 {issuer[:14]}｜文号 {row.doc_number or '—'}"
                f"｜日期 {day}｜{mark}"
            )
    print(f"抽 {len(picked)} 份，{problems} 份有对不上的项，需要打开原文人工判断")
    return 0


if __name__ == "__main__":
    sys.exit(main())
