#!/usr/bin/env python3
"""地市第一批来源的本机礼貌试抓（D-086 ④）：量出每个栏目每周新增多少份、其中多少是部里或省里
通知的转发版本（D-055 转发组，要创始人确认）、截止日期能摘出原句的比例、正文能读出来的比例。

只在本机自己的 MySQL 库里跑（--db，库名必须以 youju_citytrial 开头，会被重建），不连生产库、
不把正文发给外部模型。抓取一律用 youju 的 PoliteFetcher：透明标识 YoujuBot、先读 robots
（RFC 9309，取不到按禁止）、同一网站两次请求至少隔 3 秒、只抓政府网站、不跟到内网地址；
网站拒绝（403、防护页、robots 禁止或取不到）就记下来停在那里，不换标识、不用浏览器绕过。

步骤（按顺序跑，每步的结果写在 --out 目录，可以单独重跑）：
  lists    每个栏目抓列表第 1 页；第 1 页盖不住统计窗口时，只跟页面上写着的"下一页"链接，
           最多 3 页
  load     重建 --db 库，导入国家和省级来源、第一批清单（只在这个库里启用），写入列表条目；
           --history 从本机的旧副本库补国家和省级的历史题录和正文（只读那个库）
  details  补正文：第一批栏目窗口内的全部条目；国家和省级只补标题和地市条目相近的
  measure  抽截止候选、按 D-055 的规则归转发组，出每个栏目的四项数字（measure.json、measure.csv）

用法（用后端虚拟环境里的 Python）：
  backend/.venv/bin/python tools/team/city_trial.py lists --manifest <清单.csv> --out <目录>
  backend/.venv/bin/python tools/team/city_trial.py load --manifest <清单.csv> --out <目录> --db <库名>
  backend/.venv/bin/python tools/team/city_trial.py details --out <目录> --db <库名>
  backend/.venv/bin/python tools/team/city_trial.py measure --manifest <清单.csv> --out <目录> --db <库名>
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

# 统计窗口：截至试抓前一天的 8 整周（56 天）
WINDOW_WEEKS = 8
MAX_PAGES = 3
_NEXT_TEXT = re.compile(r"^\s*(?:下一页|下页|后一页|[>›»＞]|&gt;)\s*$")
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_HREF = re.compile(r"""href\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
_LABEL = re.compile(
    r"""(?:aria-label|title)\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL
)
_TAGS = re.compile(r"<[^>]+>")
_DB_NAME = re.compile(r"youju_citytrial[A-Za-z0-9_]*")
# 页面脚本里写着的翻页（拓尔思等政府网站常用）：第 i 页（从 0 起）是 名字_i.后缀，第 0 页是栏目首页。
# createPageHTML(总页数, 当前页, "index", "html") 和 _nPageCount / _sPageName 两种写法从 0 起；
# createPageHTML('page_div', 总页数, 当前页, 'common_list', 'shtml', 总条数) 从 1 起
_TRS_ZERO = re.compile(
    r"""createPageHTML\(\s*["']?(\d+)["']?\s*,\s*["']?(\d+)["']?\s*,\s*["'](\w+)["']\s*,\s*["'](\w+)["']"""
)
_TRS_ONE = re.compile(
    r"""createPageHTML\(\s*["'][\w-]+["']\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*["'](\w+)["']\s*,\s*["'](\w+)["']"""
)
_TRS_VARS = re.compile(
    r"""_nPageCount\s*=\s*(\d+)\s*;\s*var\s+_nCurrIndex\s*=\s*(\d+)\s*;\s*"""
    r"""var\s+_sPageName\s*=\s*["'](\w+)["']\s*;\s*var\s+_sPageExt\s*=\s*["'](\w+)["']"""
)
_COUNT_PAGE = re.compile(r"""countPage\s*=\s*(\d+)""")
_COUNT_HREF = re.compile(
    r"""["'](index)["']\s*\+\s*["']_["']\s*\+\s*i\s*\+\s*["']\.["']\s*\+\s*["'](\w+)"""
)


def today_cn() -> date:
    """北京时间的今天（试抓的统计窗口按北京时间算）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def window(today: date, weeks: int = WINDOW_WEEKS) -> tuple[date, date]:
    """统计窗口 [开始, 结束]：结束是试抓前一天，共 weeks 整周。"""
    end = today - timedelta(days=1)
    return end - timedelta(days=7 * weeks - 1), end


def row_key(row: dict) -> str:
    """清单一行的短编号，如"广东-广州-科技"（第一批里同城同部门只有一个栏目）。"""
    return f"{row['省份']}-{row['城市']}-{row['部门']}"


def read_manifest(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [row for row in csv.DictReader(fh) if (row.get("栏目URL") or "").strip()]
    keys = [row_key(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise SystemExit("清单里有同城同部门的两个栏目，row_key 需要区分")
    return rows


def in_column(target: str, first_url: str) -> bool:
    """翻页地址必须还在这个栏目里：同一网站、同一目录，栏目首页网址里的查询参数原样都在
    （常州那种 tzgg.php?catname=…，"下一页"写成 ?page=2 会丢掉栏目参数，跟过去就串到别的栏目）。"""
    first, other = urlsplit(first_url), urlsplit(target)
    if other.hostname != first.hostname or not target.startswith(
        urljoin(first_url, ".")
    ):
        return False
    need = set(parse_qsl(first.query, keep_blank_values=True))
    return need <= set(parse_qsl(other.query, keep_blank_values=True))


def next_page_url(html: str, page_url: str, first_url: str | None = None) -> str | None:
    """页面上写着"下一页"（或 >、»，或 aria-label/title 写"下一页"）的链接；只认同一栏目
    （见 in_column，first_url 默认就是这一页）的普通链接。"""
    first_url = first_url or page_url
    for attrs, inner in _ANCHOR.findall(html or ""):
        text = _TAGS.sub("", inner).replace("&nbsp;", " ")
        label = _LABEL.search(attrs)
        if not _NEXT_TEXT.match(text) and not (
            label and _NEXT_TEXT.match(label.group(2))
        ):
            continue
        found = _HREF.search(attrs)
        if not found:
            continue
        href = found.group(2).strip()
        if not href or href.startswith(("#", "javascript:", "JavaScript:")):
            continue
        target = urljoin(page_url, href)
        if target != page_url and in_column(target, first_url):
            return target
    return None


def script_pages(html: str, first_url: str) -> list[str]:
    """页面脚本里写着的翻页地址（见 _TRS_*）：返回第 2、3……页的网址，最多到总页数。"""
    html = html or ""
    base = urljoin(first_url, ".")
    found = _TRS_ONE.search(html)
    if found:
        count, name, ext = int(found.group(1)), found.group(3), found.group(4)
        return [urljoin(base, f"{name}_{p}.{ext}") for p in range(2, count + 1)]
    found = _TRS_ZERO.search(html) or _TRS_VARS.search(html)
    if found:
        count, name, ext = int(found.group(1)), found.group(3), found.group(4)
        return [urljoin(base, f"{name}_{i}.{ext}") for i in range(1, count)]
    count, href = _COUNT_PAGE.search(html), _COUNT_HREF.search(html)
    if count and href:
        return [
            urljoin(base, f"{href.group(1)}_{i}.{href.group(2)}")
            for i in range(1, int(count.group(1)))
        ]
    return []


def tail_date(items: list[dict], tail: int = 3) -> date | None:
    """页面末尾几条日期的中间那个。置顶的旧文件一般在页首，不能拿全页最早的日期判断翻没翻够；
    末尾偶尔也夹一条旧文，取中间那个，一条特别旧或特别新的都不左右结果。"""
    days = [
        date.fromisoformat(i["publish_date"]) for i in items if i.get("publish_date")
    ]
    if not days:
        return None
    last = sorted(days[-tail:])
    return last[len(last) // 2]


def covered(items: list[dict], start: date) -> bool:
    """这一页末尾的条目已经早于窗口开始，后面的页不用再翻。"""
    last = tail_date(items)
    return last is not None and last < start


def same_day(old: dict, today: date) -> bool:
    """--only 补抓的结果只能并进同一天抓的 lists.json：统计窗口跟着试抓日期走，
    不同日期抓的列表合在一起会被同一个窗口统计。"""
    return not old or old.get("today") == today.isoformat()


# ---- lists ----------------------------------------------------------------------------------


def _fetch_row(row: dict, fetcher, out: Path, start: date, max_pages: int) -> dict:
    from youju.crawler.listing import extract_items

    key = row_key(row)
    pages = []
    url: str | None = row["栏目URL"].strip()
    seen: set[str] = set()
    scripted: list[str] = []
    while url and len(pages) < max_pages and url not in seen:
        seen.add(url)
        fetched = fetcher.fetch(url)
        page = {
            "url": url,
            "status": fetched.status,
            "http_status": fetched.http_status,
            "final_url": fetched.final_url,
            "error": fetched.error,
            "items": [],
        }
        pages.append(page)
        if fetched.status != "ok":
            break
        (out / "html" / f"{key}_p{len(pages)}.html").write_text(
            fetched.text, encoding="utf-8"
        )
        items = extract_items(fetched.text, fetched.final_url or url, {})
        page["items"] = [
            {
                "title": i.title,
                "url": i.url,
                "publish_date": i.publish_date.isoformat() if i.publish_date else None,
            }
            for i in items
        ]
        before = {i["url"] for p in pages[:-1] for i in p["items"]}
        if before and {i["url"] for i in page["items"]} <= before:
            # 有的网站翻页链接返回的还是第 1 页（翻页靠脚本）：不算新的一页，按已有的折算
            page["duplicate"] = True
            page["items"] = []
            break
        if not items or covered(page["items"], start):
            break
        if len(pages) == 1:
            scripted = script_pages(fetched.text, fetched.final_url or url)
        first_url = pages[0]["final_url"] or pages[0]["url"]
        url = next_page_url(fetched.text, fetched.final_url or url, first_url) or (
            scripted[len(pages) - 1] if len(pages) - 1 < len(scripted) else None
        )
    return {
        "key": key,
        "province": row["省份"],
        "city": row["城市"],
        "dept": row["部门"],
        "url": row["栏目URL"].strip(),
        "pages": pages,
    }


def cmd_lists(args: argparse.Namespace) -> int:
    from youju.sources.onboard import trial_fetcher

    rows = read_manifest(args.manifest)
    if args.only:
        rows = [row for row in rows if row_key(row) in set(args.only)]
    out: Path = args.out
    (out / "html").mkdir(parents=True, exist_ok=True)
    path = out / "lists.json"
    old = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists() and args.only
        else {}
    )
    if not same_day(old, args.today):
        raise SystemExit(
            f"{path} 是 {old.get('today')} 抓的，--only 只能补同一天的；整批重抓"
        )
    start, end = window(args.today)
    with trial_fetcher() as fetcher, ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda row: _fetch_row(row, fetcher, out, start, args.pages), rows)
        )
    merged = {r["key"]: r for r in old.get("rows", [])}
    merged.update({r["key"]: r for r in results})
    path.write_text(
        json.dumps(
            {
                "today": args.today.isoformat(),
                "window": [start.isoformat(), end.isoformat()],
                "rows": list(merged.values()),
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    for r in results:
        first = r["pages"][0]
        items = [i for p in r["pages"] for i in p["items"]]
        dates = sorted(i["publish_date"] for i in items if i["publish_date"])
        print(
            f"{r['key']:<14} {first['status']:<17} {first['http_status'] or '':<4} "
            f"页 {len(r['pages'])} 条 {len(items):>3} "
            f"{dates[0] if dates else '':<10}～{dates[-1] if dates else ''} {first['error'] or ''}"
        )
    return 0


# ---- 本机试抓库 ------------------------------------------------------------------------------


def db_url(name: str) -> str:
    """只认 youju_citytrial 开头的本机库：load 会整库重建，不能误指到别人的库。"""
    if not _DB_NAME.fullmatch(name):
        raise SystemExit("--db 必须是 youju_citytrial 开头的库名（会被重建）")
    return f"mysql+pymysql://root@127.0.0.1:33306/{name}?charset=utf8mb4"


def _open(name: str):
    """把 youju 的默认连接也指到试抓库（有的函数用默认连接），返回会话工厂。"""
    import os

    url = db_url(name)
    os.environ["YOUJU_DATABASE_URL"] = url
    from sqlalchemy.orm import sessionmaker
    from youju.config import get_settings
    from youju.db import create_mysql_engine

    get_settings.cache_clear()
    return sessionmaker(create_mysql_engine(url), expire_on_commit=False)


def _recreate(name: str) -> None:
    from sqlalchemy import make_url, text
    from youju.db import create_mysql_engine
    from youju.migrations import upgrade

    url = db_url(name)
    server = create_mysql_engine(make_url(url).set(database=""))
    with server.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
        conn.execute(
            text(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
            )
        )
    server.dispose()
    upgrade(url)


_HISTORY_SQL = """
SELECT s.url, d.url, d.url_key, d.title, d.publish_date, d.first_seen_at, d.kind_override,
       dd.status, dd.body_text, dd.issuer, dd.doc_number, dd.publish_date, dd.fetched_at,
       dd.http_status, dd.final_url
FROM `{db}`.documents d
JOIN `{db}`.sources s ON s.id = d.source_id
LEFT JOIN `{db}`.document_details dd ON dd.document_id = d.id
WHERE s.level IN ('国家级', '省级') AND d.publish_date IS NOT NULL
"""


def _copy_history(session, history_db: str) -> int:
    """从本机旧副本库（只读）补国家和省级的旧题录和正文：地市转发的上游通知可能早于这次的列表页。"""
    from sqlalchemy import select, text
    from youju.categories import doc_category
    from youju.classify import doc_kind
    from youju.models import Document, DocumentDetail, Source
    from youju.title_tags import INDUSTRY_RULES, SUPPORT_RULES

    if not re.fullmatch(r"youju[A-Za-z0-9_]*", history_db):
        raise SystemExit("--history 库名不对")
    sources = {s.url: s.id for s in session.scalars(select(Source))}
    have = set(session.scalars(select(Document.url_key)))
    copied = 0
    for row in session.execute(text(_HISTORY_SQL.format(db=history_db))).all():
        (src_url, url, key, title, published, seen, override, status, body, issuer, number,
         detail_date, fetched, http_status, final_url) = row  # fmt: skip
        if src_url not in sources or key in have:
            continue
        doc = Document(
            source_id=sources[src_url],
            url=url,
            url_key=key,
            title=title,
            rule_kind=doc_kind(title),
            rule_category=doc_category(title),
            rule_supports=SUPPORT_RULES.stored(title),
            rule_industries=INDUSTRY_RULES.stored(title),
            publish_date=published,
            first_seen_at=seen,
            last_seen_at=seen,
            is_baseline=True,
            status="fetched" if status == "ok" else "discovered",
            kind_override=override,
        )
        session.add(doc)
        session.flush()
        have.add(key)
        if status is not None:
            session.add(
                DocumentDetail(
                    document_id=doc.id,
                    status=status,
                    body_text=body or "",
                    issuer=issuer,
                    doc_number=number,
                    publish_date=detail_date,
                    fetched_at=fetched,
                    http_status=http_status,
                    final_url=final_url,
                    attempt_count=1,
                )
            )
        copied += 1
    session.commit()
    return copied


def cmd_load(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from sqlalchemy import select
    from youju.config import DEFAULT_SOURCES_CSV, get_settings
    from youju.crawler.fetcher import PoliteFetcher
    from youju.crawler.listing import ListItem
    from youju.crawler.service import crawl_sources, save_items
    from youju.dedup import reconcile_documents
    from youju.models import Source
    from youju.sources.importer import import_sources

    data = json.loads((args.out / "lists.json").read_text(encoding="utf-8"))
    _recreate(args.db)
    factory = _open(args.db)
    with factory() as session:
        national = import_sources(session, DEFAULT_SOURCES_CSV)
        batch = import_sources(session, args.manifest)
        print(
            f"国家和省级来源 {national.created}（启用 {national.enabled}）；第一批 {batch.created}"
            f"（启用 {batch.enabled}，应为 0）"
        )
        if args.history:
            print(
                f"从 {args.history} 补国家和省级旧题录 {_copy_history(session, args.history)} 份"
            )
    # 国家和省级来源照常抓一次列表第 1 页（和线上每轮一样）；地市只用 lists 那一步抓到的条目
    with factory() as session:
        ids = list(
            session.scalars(
                select(Source.id).where(
                    Source.enabled.is_(True), Source.level != "市级"
                )
            )
        )
    settings = get_settings()
    if not args.skip_national:
        with PoliteFetcher(
            settings.user_agent, settings.request_timeout, settings.per_host_interval
        ) as fetcher:
            results = crawl_sources(factory, ids, fetcher, max_workers=8)
        ok = sum(r.status == "ok" for r in results)
        print(f"国家和省级列表第 1 页：{ok}/{len(results)} 个栏目成功")
    seen_at = datetime.now(UTC)
    with factory() as session:
        by_url = {
            s.url: s
            for s in session.scalars(select(Source).where(Source.level == "市级"))
        }
        written = 0
        for row in data["rows"]:
            items = [
                ListItem(
                    title=i["title"],
                    url=i["url"],
                    publish_date=date.fromisoformat(i["publish_date"])
                    if i["publish_date"]
                    else None,
                )
                for page in row["pages"]
                for i in page["items"]
            ]
            source = by_url[row["url"]]
            if not items:
                continue
            written += save_items(
                session, source, items, baseline=True, seen_at=seen_at
            )
            # 只在这个试抓库里记成"抓成功过"，measure 才认它是在抓的栏目；线上仍是停用
            source.last_success_at = seen_at
        reconcile_documents(session)
        session.commit()
    print(f"地市条目写入 {written} 份")
    return 0


def _window_city_ids(session, start: date, end: date) -> list[int]:
    from sqlalchemy import select
    from youju.models import Document, Source

    return list(
        session.scalars(
            select(Document.id)
            .join(Source, Source.id == Document.source_id)
            .where(
                Source.level == "市级",
                Document.publish_date >= start,
                Document.publish_date <= end,
            )
            .order_by(Document.id)
        )
    )


def cmd_details(args: argparse.Namespace) -> int:
    from collections import Counter

    from sqlalchemy import select
    from youju.crawler.detail_service import fetch_details
    from youju.dedup import reconcile_documents
    from youju.models import Document, DocumentDetail, Source
    from youju.repost import MAX_DAYS_APART, core_title, similar_titles

    data = json.loads((args.out / "lists.json").read_text(encoding="utf-8"))
    start, end = (date.fromisoformat(d) for d in data["window"])
    factory = _open(args.db)
    with factory() as session:
        city_ids = _window_city_ids(session, start, end)
        cores = {
            core_title(title)
            for title in session.scalars(
                select(Document.title).where(Document.id.in_(city_ids))
            )
        }
        upstream = [
            (doc_id, title)
            for doc_id, title in session.execute(
                select(Document.id, Document.title)
                .join(Source, Source.id == Document.source_id)
                .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
                .where(
                    Source.level != "市级",
                    DocumentDetail.document_id.is_(None),
                    Document.publish_date >= start - timedelta(days=MAX_DAYS_APART),
                )
            )
        ]
        # 国家和省级只补标题和窗口内地市条目相近的（同一份上游通知才可能归进一个转发组）
        related = [
            doc_id
            for doc_id, title in upstream
            if any(similar_titles(core_title(title), core) for core in cores)
        ]
        ids = city_ids + related
        print(
            f"补正文：地市窗口内 {len(city_ids)} 份，国家和省级标题相近的 {len(related)} 份"
        )
        results = fetch_details(
            session, limit=max(1, len(ids)), document_ids=ids, max_workers=args.workers
        )
        print(
            "结果："
            + "，".join(
                f"{k} {v}" for k, v in Counter(r.status for r in results).items()
            )
        )
        reconcile_documents(session)
        session.commit()
    return 0


# ---- measure --------------------------------------------------------------------------------


def span_days(pages: list[dict], start: date, end: date) -> int:
    """抓到的列表页盖住了窗口里的多少天：翻够了是整个窗口；没翻够的从末尾那条的日期算到窗口结束。"""
    items = [i for p in pages for i in p["items"]]
    last = tail_date(items)
    if last is None:
        return 0
    return (end - max(last, start)).days + 1


def per_week(count: int, days: int) -> float:
    return round(count * 7 / days, 1) if days else 0.0


def shared_weekly(parts: list[tuple[set[int], int]]) -> float:
    """几个栏目合起来每周触到多少个不同的转发组。parts 是每个栏目（触到的组, 盖住的天数）：
    各栏目按自己盖住的天数折算后相加，再按组在栏目之间重复的比例折掉（同一份部里通知被几个
    栏目转发只算一组）。盖住天数不同的栏目不能直接合在一起除以同一个天数。"""
    total = sum(len(ids) for ids, _ in parts)
    if not total:
        return 0.0
    distinct = len(set().union(*(ids for ids, _ in parts)))
    rate = sum(len(ids) * 7 / days for ids, days in parts if days)
    return round(rate * distinct / total, 1)


def _deadline_facts(body: str, published: date | None) -> tuple[list[date], bool]:
    """规则抽出的截止候选日期，以及有没有一条能当机器摘录给出（D-061）：原句里写着这个日期，
    原句也确实在正文里——和线上 deadline_excerpts.batch 用同一套判断（去空白、全角半角统一）。"""
    from youju.deadline_excerpts import _normal, date_in_quote
    from youju.extract.deadlines import extract_deadlines

    found = extract_deadlines(body, published)
    text = _normal(body)
    return [c.due_date for c in found], any(
        date_in_quote(c.due_date, c.evidence) and _normal(c.evidence) in text
        for c in found
    )


_FOOTER = re.compile("ICP备|网站标识码|公网安备")


def boilerplate_ids(bodies: dict[int, str]) -> set[int]:
    """读成了网页的公共部分（页脚、版权）而不是正文的：同一栏目 3 份以上一字不差；
    或者备案号、网站标识码前面不到 100 字（正文还没开始就到了页脚）。"""
    counts: dict[str, int] = {}
    for body in bodies.values():
        counts[body] = counts.get(body, 0) + 1
    junk = set()
    for doc_id, body in bodies.items():
        footer = _FOOTER.search(body)
        if counts[body] >= 3 or (footer and len(body[: footer.start()].strip()) < 100):
            junk.add(doc_id)
    return junk


def cmd_measure(args: argparse.Namespace) -> int:
    from collections import Counter, defaultdict

    from sqlalchemy import select
    from youju.classify import effective_kind
    from youju.models import Document, DocumentDetail, DocumentMerge, Source
    from youju.repost import link, load_candidates

    data = json.loads((args.out / "lists.json").read_text(encoding="utf-8"))
    start, end = (date.fromisoformat(d) for d in data["window"])
    trial_day = date.fromisoformat(data["today"])
    factory = _open(args.db)
    with factory() as session:
        sources = {s.id: s for s in session.scalars(select(Source))}
        by_url = {s.url: s for s in sources.values()}
        docs = {
            doc.id: (doc, detail)
            for doc, detail in session.execute(
                select(Document, DocumentDetail).outerjoin(
                    DocumentDetail, DocumentDetail.document_id == Document.id
                )
            )
        }
        merges = list(
            session.scalars(select(DocumentMerge).where(DocumentMerge.active.is_(True)))
        )
        proposals, _ = link(load_candidates(session))

    def level(doc_id: int) -> str:
        return sources[docs[doc_id][0].source_id].level

    # 同一文件（第一层，自动归并，不用人确认）
    merged: set[int] = set()
    by_canonical: dict[int, list[int]] = defaultdict(list)
    for m in merges:
        by_canonical[m.canonical_id].append(m.duplicate_id)
    for canonical, dups in by_canonical.items():
        members = [canonical, *dups]
        if any(level(i) == "市级" for i in members):
            merged |= set(members)
    # 转发组（第二层，D-055，前 4 周每组都要创始人确认）
    group_of = {doc_id: n for n, p in enumerate(proposals) for doc_id in p.ids}
    upstream = {
        n: any(level(i) != "市级" for i in p.ids) for n, p in enumerate(proposals)
    }

    rows_out = []
    parts: dict[str, list[tuple[set[int], int]]] = defaultdict(list)
    all_touched: set[int] = set()
    for row in data["rows"]:
        source = by_url[row["url"]]
        first = row["pages"][0]
        days = span_days(row["pages"], start, end) if first["status"] == "ok" else 0
        since = end - timedelta(days=days - 1)
        mine = [
            (doc, detail)
            for doc, detail in docs.values()
            if doc.source_id == source.id
            and doc.publish_date is not None
            and since <= doc.publish_date <= end
        ]
        ok = {
            doc.id: detail.body_text.strip()
            for doc, detail in mine
            if detail and detail.status == "ok" and (detail.body_text or "").strip()
        }
        junk = boilerplate_ids(ok)
        readable = {doc_id: body for doc_id, body in ok.items() if doc_id not in junk}
        notices = [
            (doc, detail)
            for doc, detail in mine
            if effective_kind(doc.title, doc.kind_override) == "申报通知"
        ]
        with_deadline = with_quote = still_open = 0
        for doc, detail in notices:
            if doc.id not in readable:
                continue
            days_found, quoted = _deadline_facts(
                readable[doc.id], detail.publish_date or doc.publish_date
            )
            with_deadline += bool(days_found)
            with_quote += quoted
            still_open += any(day >= trial_day for day in days_found)
        notices_readable = sum(doc.id in readable for doc, _ in notices)
        reposted = [doc for doc, _ in mine if doc.id in group_of]
        touched = {group_of[doc.id] for doc in reposted}
        parts[row["city"]].append((touched, days))
        all_touched |= touched
        local = [
            doc for doc, _ in notices if doc.id not in group_of and doc.id not in merged
        ]
        rows_out.append(
            {
                "key": row["key"],
                "province": row["province"],
                "city": row["city"],
                "dept": row["dept"],
                "url": row["url"],
                "list_status": first["status"],
                "list_error": first["error"],
                "pages": len([p for p in row["pages"] if p["items"]]),
                "span_days": days,
                "covered": days == (end - start).days + 1,
                "window_docs": len(mine),
                "weekly_docs": per_week(len(mine), days),
                "kinds": dict(
                    Counter(effective_kind(d.title, d.kind_override) for d, _ in mine)
                ),
                "notices": len(notices),
                "weekly_notices": per_week(len(notices), days),
                "detail_status": dict(
                    Counter(dd.status if dd else "未抓" for _, dd in mine)
                ),
                "boilerplate": len(junk),
                "readable": len(readable),
                "readable_rate": round(len(readable) / len(mine), 3) if mine else None,
                "notices_readable": notices_readable,
                "notices_with_deadline": with_deadline,
                "notices_with_quote": with_quote,
                "quote_rate": round(with_quote / notices_readable, 3)
                if notices_readable
                else None,
                "weekly_deadline_checks": per_week(with_deadline, days),
                "open_deadlines_on_trial_day": still_open,
                "auto_merged": sum(doc.id in merged for doc, _ in mine),
                "repost_docs": len(reposted),
                "repost_upstream_docs": sum(upstream[group_of[d.id]] for d in reposted),
                "repost_groups": len(touched),
                "weekly_repost_groups": per_week(len(touched), days),
                "local_notices": len(local),
                "weekly_local_notices": per_week(len(local), days),
            }
        )
    out = {
        "window": data["window"],
        "today": data["today"],
        "rows": rows_out,
        "city_groups": {
            city: {
                "groups": len(set().union(*(ids for ids, _ in mine))),
                "weekly": shared_weekly(mine),
            }
            for city, mine in parts.items()
        },
        "all_groups": {
            "groups": len(all_touched),
            "weekly": shared_weekly([part for mine in parts.values() for part in mine]),
        },
        "groups": [
            {
                "strength": p.strength,
                "upstream": upstream[n],
                "topic": p.topic,
                "in_window": n in all_touched,
                "members": [
                    {
                        "id": doc_id,
                        "level": level(doc_id),
                        "source": "{0.province}{1}{0.dept}".format(
                            sources[docs[doc_id][0].source_id],
                            sources[docs[doc_id][0].source_id].city or "",
                        ),
                        "date": docs[doc_id][0].publish_date.isoformat(),
                        "title": docs[doc_id][0].title,
                    }
                    for doc_id in p.ids
                ],
            }
            for n, p in enumerate(proposals)
            if any(level(i) == "市级" for i in p.ids)
        ],
    }
    (args.out / "measure.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    fields = (
        [k for k in rows_out[0] if k not in ("kinds", "detail_status")]
        if rows_out
        else []
    )
    with (args.out / "measure.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows_out)
    for r in rows_out:
        print(
            f"{r['key']:<12} 盖{r['span_days']:>2}天 窗口{r['window_docs']:>3} 周{r['weekly_docs']:>4} "
            f"申报{r['notices']:>3} 正文{r['readable']:>3}/{r['window_docs']:<3}"
            f"(页脚{r['boilerplate']}) 截止{r['notices_with_quote']:>2}/{r['notices_readable']:<2} "
            f"未过期{r['open_deadlines_on_trial_day']:>2} 同文{r['auto_merged']} "
            f"转发{r['repost_docs']:>2}(上游{r['repost_upstream_docs']}) 组{r['repost_groups']} "
            f"本地独有{r['local_notices']}"
        )
    print(
        f"窗口内地市文件进的转发组：{len(all_touched)} 组，每周约 {out['all_groups']['weekly']} 组"
    )
    return 0


# ---- main -----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lists", help="抓列表页")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--today", type=date.fromisoformat, default=today_cn())
    p.add_argument("--pages", type=int, default=MAX_PAGES)
    p.add_argument("--only", nargs="*", help="只重抓这几个栏目（row_key）")
    p.set_defaults(func=cmd_lists)
    p = sub.add_parser("load", help="建试抓库、导入来源和条目")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--db", required=True)
    p.add_argument(
        "--history", help="本机旧副本库名（只读），补国家和省级的旧题录和正文"
    )
    p.add_argument(
        "--skip-national", action="store_true", help="不抓国家和省级列表页（调试用）"
    )
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("details", help="补正文")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(func=cmd_details)
    p = sub.add_parser("measure", help="出每个栏目的四项数字")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_measure)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
