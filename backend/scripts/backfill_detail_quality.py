"""对近三个月已抓取但正文过短的申报通知限额重取；仅访问公开政府网站。"""

import argparse
from collections import Counter
from datetime import date
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select, text

from youju.classify import kind_filter
from youju.config import get_settings
from youju.crawler.detail_service import _fetch_one, _government_url, _save_result
from youju.crawler.fetcher import PoliteFetcher
from youju.db import session_factory
from youju.models import Document, DocumentDetail, Source


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=100)
    args = parser.parse_args()
    settings = get_settings()
    db = urlsplit(settings.database_url)
    if db.hostname not in {"localhost", "127.0.0.1"} or db.port != 55432:
        raise SystemExit("仅允许本地 55432 开发库")
    if args.max_requests < 1 or args.max_requests > 100:
        raise SystemExit("请求上限必须为 1 至 100")

    with session_factory()() as session:
        rows = session.execute(
            select(Document.id, Document.url, DocumentDetail.status)
            .join(DocumentDetail, DocumentDetail.document_id == Document.id)
            .join(Source, Source.id == Document.source_id)
            .where(
                Source.rights_level == 1,
                Document.publish_date >= text("current_date - interval '3 months'"),
                kind_filter(Document.title, ["申报通知"]),
                func.length(DocumentDetail.body_text) < 30,
                DocumentDetail.status.in_(("parse_empty", "ok", "pdf_error", "robots_error")),
            )
            .order_by((DocumentDetail.status == "parse_empty").desc(), Document.id)
        ).all()
        session.commit()
        count = 0
        blocked_hosts: set[str] = set()
        results: Counter[str] = Counter()

        def count_request(request: httpx.Request) -> None:
            nonlocal count
            if count >= args.max_requests:
                raise httpx.RequestError("已达请求上限", request=request)
            count += 1

        with PoliteFetcher(
            settings.user_agent,
            settings.request_timeout,
            settings.per_host_interval,
            url_allowed=_government_url,
        ) as fetcher:
            fetcher._client.event_hooks["request"].append(count_request)
            fetcher._compat_client.event_hooks["request"].append(count_request)
            for document_id, url, _ in rows:
                host = (urlsplit(url).hostname or "").lower()
                if count >= args.max_requests:
                    break
                if host in blocked_hosts or not _government_url(url):
                    continue
                outcome = _fetch_one(document_id, url, fetcher)
                _save_result(session, outcome)
                results[outcome.status] += 1
                if outcome.status in {"protected", "robots_disallowed"} or (
                    outcome.fetch and outcome.fetch.http_status in {401, 403, 429}
                ):
                    blocked_hosts.add(host)
        print(f"日期={date.today()} 候选={len(rows)} 请求={count} 处理={sum(results.values())}")
        print(f"状态={dict(results)} 拦截站点={len(blocked_hosts)}")


if __name__ == "__main__":
    main()
