"""验收 A4 抽检：查名单结果和名单原件一致；换一种括号也查得到；不在名单上的查不到。

- 在名单上的：按来源文件类型抽 10 家（PDF 5、Excel 3、Word 1、网页 1，各来自不同的文件），
  重新下载名单原件，确认企业名称写在原件里；再用查名单接口按原样、换括号各查一次，都要查到这份文件。
- 不在名单上的：从机构线索表里挑 10 家不在任何已收录名单上的真实机构，接口都要查不到。

只读数据库；下载遵守 robots、每站间隔 3 秒。要用和数据库同一版本的代码运行。
用法（在 backend/ 目录下）：
    uv run python scripts/check_lists.py --database-url "mysql+pymysql://..." [--seed 929]
"""

import argparse
import csv
import io
import os
import random
import re
import sys
from pathlib import Path
from urllib.parse import quote

PLAN = {"pdf": 5, "xlsx": 3, "docx": 1, "html": 1}
LEADS = Path(__file__).resolve().parents[2] / "docs" / "商业" / "机构线索_v1.csv"


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "").replace("（", "(").replace("）", ")")


def _kind(url: str) -> str:
    ext = url.rsplit("?", 1)[0].rsplit(".", 1)[-1].lower()
    return "html" if ext in ("html", "htm", "shtml") else ext


def _text(kind: str, content: bytes, html: str, name: str) -> str:
    if kind == "pdf":
        import pdfplumber

        parts = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages[:300]:
                parts.append(page.extract_text() or "")
                if _compact(name) in _compact(parts[-1]):
                    break
        return "\n".join(parts)
    if kind == "xlsx":
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        return "\n".join(
            " ".join(str(c) for c in row if c is not None)
            for sheet in book.worksheets
            for row in sheet.iter_rows(values_only=True)
        )
    if kind == "docx":
        from docx import Document as Word

        doc = Word(io.BytesIO(content))
        cells = [c.text for t in doc.tables for r in t.rows for c in r.cells]
        return "\n".join([p.text for p in doc.paragraphs] + cells)
    import lxml.html

    return lxml.html.document_fromstring(html).text_content()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--seed", type=int, default=929)
    args = parser.parse_args()
    os.environ["YOUJU_DATABASE_URL"] = args.database_url

    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from youju.api.app import app
    from youju.config import get_settings
    from youju.crawler.detail_service import _government_url
    from youju.crawler.fetcher import PoliteFetcher
    from youju.db import session_factory
    from youju.models import Document, ListEntry, Source

    rng = random.Random(args.seed)
    with session_factory()() as session:
        rows = session.execute(
            select(ListEntry.name, ListEntry.source_url, ListEntry.document_id, Document.title)
            .join(Document, Document.id == ListEntry.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(Source.rights_level == 1)
        ).all()
        listed = set(session.scalars(select(func.distinct(ListEntry.normalized_name))))
    by_kind: dict[str, dict[str, list]] = {}
    for row in rows:
        by_kind.setdefault(_kind(row.source_url), {}).setdefault(row.source_url, []).append(row)
    picked = []
    for kind, count in PLAN.items():
        files = sorted(by_kind.get(kind, {}))
        for url in rng.sample(files, min(count, len(files))):
            picked.append((kind, rng.choice(by_kind[kind][url])))

    settings = get_settings()
    client = TestClient(app)
    ok_original = ok_lookup = 0
    with PoliteFetcher(
        settings.user_agent,
        settings.request_timeout,
        settings.per_host_interval,
        url_allowed=_government_url,
    ) as fetcher:
        for kind, row in picked:
            fetched = fetcher.fetch(row.source_url)
            if fetched.status != "ok":
                print(f"[{kind}] {row.name}：原件打不开（{fetched.status}）{row.source_url}")
                continue
            try:
                text = _text(kind, fetched.content, fetched.text, row.name)
                in_original = _compact(row.name) in _compact(text)
            except Exception as exc:  # noqa: BLE001 - 原件格式各异，读不出来就记下来人工看
                in_original = False
                print(f"   读原件出错：{exc.__class__.__name__}")
            swapped = row.name.translate(str.maketrans("（）()", "()（）"))
            found = []
            for query in dict.fromkeys([row.name, swapped]):
                body = client.get("/api/v1/lists/lookup?q=" + quote(query)).json()
                found.append(any(int(i["id"]) == row.document_id for i in body.get("items", [])))
            ok_original += in_original
            ok_lookup += all(found)
            print(
                f"[{kind}] {row.name}｜原件里有：{'是' if in_original else '否'}"
                f"｜原样查到：{'是' if found[0] else '否'}"
                f"｜换括号查到：{'是' if found[-1] else '否'}｜{row.title[:28]}"
            )

    with LEADS.open(encoding="utf-8-sig") as fh:
        leads = [r["机构名称"].strip() for r in csv.DictReader(fh) if r.get("机构名称")]
    outside = [n for n in leads if n and n not in listed][:10]
    empty = 0
    for name in outside:
        body = client.get("/api/v1/lists/lookup?q=" + quote(name)).json()
        empty += not body.get("items")
        print(f"[不在名单上] {name}｜查到 {len(body.get('items', []))} 份")
    print(
        f"在名单上 {len(picked)} 家：原件里有 {ok_original} 家，原样和换括号都查到 {ok_lookup} 家；"
        f"不在名单上 {len(outside)} 家：查不到 {empty} 家"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
