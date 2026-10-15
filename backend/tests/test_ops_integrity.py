"""异常必须留下可核验的失败；缺告警配置不能伪装成发送成功。"""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_service import LIST_V1, FakeFetcher, make_source, page
from youju import alerts, cli
from youju.crawler import service
from youju.models import Document, Source, SourceCheck
from youju.source_health import check_sources


class BrokenFetcher:
    def fetch(self, url):
        raise RuntimeError("sensitive-response-must-not-appear")


def test_internal_error_replaces_today_success_without_losing_baseline(session_factory, caplog):
    with session_factory() as session:
        source = make_source(session)
        source_id, url = source.id, source.url
        service.check_source(session, source, FakeFetcher({url: [page(url, LIST_V1)]}))
        last_success = source.last_success_at

    result = service.crawl_sources(session_factory, [source_id], BrokenFetcher())[0]
    assert result.status == "internal_error" and result.recorded and not result.baseline
    with session_factory() as session:
        source = session.get(Source, source_id)
        checks = session.scalars(select(SourceCheck).order_by(SourceCheck.id)).all()
        assert len(checks) == 2
        assert checks[0].is_baseline
        assert checks[1].status == "internal_error" and not checks[1].is_baseline
        assert checks[1].error == "内部错误"
        assert checks[1].items_new == checks[1].items_found == 0
        assert source.last_success_at == last_success
        assert source.health == "degraded" and source.consecutive_failures == 1
        assert check_sources(session).successful == 0
        assert not check_sources(session).passed
    assert "sensitive-response-must-not-appear" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_failure_record_transaction_rolls_back_and_next_source_runs(
    session_factory, monkeypatch, caplog
):
    with session_factory() as session:
        first = make_source(session, "https://same.gov.cn/broken/").id
        second = make_source(session, "https://same.gov.cn/good/").id
    update = service._update_health

    def fail_record(source, status, *args):
        if status == "internal_error":
            raise RuntimeError("simulated bookkeeping failure")
        return update(source, status, *args)

    class Fetcher:
        def fetch(self, url):
            if url.endswith("broken/"):
                raise RuntimeError("simulated fetch failure")
            return page(url, LIST_V1)

    monkeypatch.setattr(service, "_update_health", fail_record)
    results = service.crawl_sources(session_factory, [first, second], Fetcher(), max_workers=1)
    assert {r.source_id: (r.status, r.recorded) for r in results} == {
        first: ("internal_error", False),
        second: ("ok", True),
    }
    assert "simulated bookkeeping failure" not in caplog.text
    assert "simulated fetch failure" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    with session_factory() as session:
        checks = session.scalars(select(SourceCheck)).all()
        assert len(checks) == 1 and checks[0].source_id == second
        assert session.get(Source, second).health == "ok"


def test_failure_record_refreshes_health_under_source_lock(session_factory, monkeypatch):
    with session_factory() as session:
        source_id = make_source(session).id
    lock = service._lock_source

    def commit_elsewhere_then_lock(session, source):
        with session_factory() as other:
            other.get(Source, source_id).consecutive_failures = 2
            other.commit()
        lock(session, source)

    monkeypatch.setattr(service, "_lock_source", commit_elsewhere_then_lock)
    with session_factory() as session:
        assert service._record_internal_error(
            session, source_id, 3, service._now(), service.time.monotonic()
        )
    with session_factory() as session:
        source = session.get(Source, source_id)
        assert source.consecutive_failures == 3 and source.health == "failing"


def test_error_after_insert_rolls_back_and_keeps_first_baseline(session_factory, monkeypatch):
    with session_factory() as session:
        source = make_source(session)
        source_id, url = source.id, source.url
    update = service._update_health

    def fail_on_success(source, status, *args):
        if status == "ok":
            raise RuntimeError("simulated failure after insert")
        return update(source, status, *args)

    monkeypatch.setattr(service, "_update_health", fail_on_success)
    result = service.crawl_sources(
        session_factory, [source_id], FakeFetcher({url: [page(url, LIST_V1)]})
    )[0]
    assert (result.status, result.recorded) == ("internal_error", True)
    with session_factory() as session:
        assert session.scalars(select(Document)).all() == []
        assert session.get(Source, source_id).last_success_at is None
        checks = session.scalars(select(SourceCheck)).all()
        assert (
            len(checks) == 1 and checks[0].status == "internal_error" and not checks[0].is_baseline
        )

    monkeypatch.setattr(service, "_update_health", update)
    result = service.crawl_sources(
        session_factory, [source_id], FakeFetcher({url: [page(url, LIST_V1)]})
    )[0]
    assert result.status == "ok" and result.baseline
    with session_factory() as session:
        docs = session.scalars(select(Document)).all()
        assert len(docs) == 2 and all(doc.is_baseline for doc in docs)


@pytest.mark.parametrize("recorded, expected", [(True, 0), (False, 1)])
def test_crawl_cli_fails_when_failure_record_is_missing(
    session_factory, monkeypatch, recorded, expected
):
    from youju.crawler import fetcher

    with session_factory() as session:
        source_id = make_source(session).id
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr(fetcher, "PoliteFetcher", lambda *a, **kw: nullcontext(object()))
    monkeypatch.setattr(
        service,
        "crawl_sources",
        lambda *a, **kw: [
            service.CheckSummary(source_id, "internal_error", 0, 0, "内部错误", recorded=recorded)
        ],
    )
    assert cli.main(["crawl", "--source-id", str(source_id)]) == expected


@pytest.mark.parametrize("sent, expected", [(True, 0), (False, 1), (None, 1)])
def test_alert_cli_requires_confirmed_submission(monkeypatch, sent, expected, capsys):
    monkeypatch.setattr(alerts, "send_alert", lambda _: sent)
    assert cli.main(["alert", "controlled-test"]) == expected
    if sent is None:
        assert "未配置告警" in capsys.readouterr().out


@pytest.mark.parametrize("sent, expected", [(True, 0), (False, 1), (None, 1)])
def test_daily_report_requires_submission_except_dry_run(monkeypatch, sent, expected, capsys):
    from youju import ai, digest

    monkeypatch.setattr(ai, "get_chat_client", lambda: None)
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: nullcontext(object()))
    monkeypatch.setattr(digest, "build_digest", lambda *a, **kw: object())
    monkeypatch.setattr(digest, "digest_text", lambda *a: "synthetic report")
    monkeypatch.setattr(alerts, "send_alert", lambda _: sent)
    assert cli._cmd_daily_report(SimpleNamespace(dry_run=False)) == expected
    assert ("未配置告警" in capsys.readouterr().out) == (sent is None)
    monkeypatch.setattr(alerts, "send_alert", lambda _: pytest.fail("dry-run must not send"))
    assert cli._cmd_daily_report(SimpleNamespace(dry_run=True)) == 0
    assert "未配置告警" not in capsys.readouterr().out
    monkeypatch.setattr(cli, "_finish_ai", lambda *a: False)
    assert cli._cmd_daily_report(SimpleNamespace(dry_run=True)) == 1
