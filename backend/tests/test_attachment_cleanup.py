"""附件副本删除的收尾：未确认不算删掉，整体失败马上停，已确认的按批结算。

对象存储只用 httpx.MockTransport，不联网。sleep 由测试注入，或替换 copies._retrying。
"""

import json

import httpx
import pytest
from sqlalchemy import select

from tests.test_attachments import ENV, NOW, PDF, PREFIX, _assert_hints, add_copy, obj
from youju import attachments as copies
from youju.models import AttachmentCopy


def _ids(count: int) -> list[str]:
    return [PREFIX + f"attachments/{i:064x}.pdf" for i in range(count)]


def _ok(pending, status_of=None, errmsg_of=None) -> httpx.Response:
    rows = []
    for file_id in pending:
        item = {"fileid": file_id, "status": 0 if status_of is None else status_of(file_id)}
        if errmsg_of is not None and (errmsg := errmsg_of(file_id)) is not None:
            item["errmsg"] = errmsg
        rows.append(item)
    return httpx.Response(200, json={"errcode": 0, "delete_list": rows})


def test_unconfirmed_head_batch_keeps_later_confirmations() -> None:
    # 防最前面 50 个“文件不存在”被当成已删除，或没发完后面的批次，以及报错带上 ID 和原文。
    ids = _ids(120)
    head = set(ids[:50])
    requests, waits = [], []

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        return _ok(
            pending,
            status_of=lambda file_id: 1 if file_id in head else 0,
            errmsg_of=lambda file_id: "STORAGE_FILE_NONEXIST" if file_id in head else None,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies.delete_copies(client, ENV, ids, sleep=waits.append)
    message = str(error.value)
    assert requests == [ids[:50], ids[50:100], ids[100:]]
    assert [len(batch) for batch in requests] == [50, 50, 20]
    assert error.value.confirmed == set(ids[50:])
    assert waits == []
    assert "未确认：50 个（状态 1）" in message
    _assert_hints(message, not_found_hint=50)
    assert "STORAGE_FILE_NONEXIST" not in message
    for file_id in ids:
        assert file_id not in message


def test_one_failed_id_still_confirms_the_rest() -> None:
    # 防只有第一个 ID 失败时，把同批或后面已经删掉的也当成没确认。
    ids = _ids(120)
    requests, waits = [], []

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        return _ok(pending, status_of=lambda file_id: 1 if file_id == ids[0] else 0)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies.delete_copies(client, ENV, ids, sleep=waits.append)
    assert requests == [ids[:50], ids[50:100], ids[100:]]
    assert [len(batch) for batch in requests] == [50, 50, 20]
    assert error.value.confirmed == set(ids[1:])
    assert len(error.value.confirmed) == 119
    assert waits == []


def test_cross_batch_item_failures_are_one_summary() -> None:
    # 防第 1 批和第 3 批的不同状态被拆开报、状态顺序错，或这种永久失败被标成可重试。
    ids = _ids(120)
    status_by_id = {ids[0]: 1, ids[1]: 1, ids[117]: 40097, ids[118]: 40097, ids[119]: 40097}
    requests, waits = [], []

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        return _ok(pending, status_of=lambda file_id: status_by_id.get(file_id, 0))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies.delete_copies(client, ENV, ids, sleep=waits.append)
    message = str(error.value)
    assert requests == [ids[:50], ids[50:100], ids[100:]]
    assert waits == []
    assert "未确认：5 个（状态 1,40097）" in message
    assert error.value.retryable is False
    assert error.value.confirmed == set(ids) - set(status_by_id)


def test_busy_status_retries_pending_item_then_next_batch() -> None:
    # 防某一项一直返回忙码时重发了整批、等待不是 5 秒和 15 秒，或耗尽后不再发下一批。
    ids = _ids(100)
    requests, waits = [], []

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        return _ok(pending, status_of=lambda file_id: -1 if file_id == ids[0] else 0)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies.delete_copies(client, ENV, ids, sleep=waits.append)
    assert waits == [5, 15]
    assert requests == [ids[:50], [ids[0]], [ids[0]], ids[50:]]
    assert error.value.confirmed == set(ids[1:])
    assert "未确认：1 个（状态 -1）" in str(error.value)
    assert error.value.retryable is True


def test_busy_item_plus_status_one_is_not_retryable() -> None:
    # 防未确认项里只要有忙码，就把混有 status=1 的整次失败也标成可重试。
    ids = _ids(100)
    requests, waits = [], []

    def status_of(file_id: str) -> int:
        if file_id == ids[0]:
            return -1
        if file_id == ids[50]:
            return 1
        return 0

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        return _ok(pending, status_of=status_of)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies.delete_copies(client, ENV, ids, sleep=waits.append)
    assert waits == [5, 15]
    assert requests == [ids[:50], [ids[0]], [ids[0]], ids[50:]]
    assert error.value.confirmed == set(ids) - {ids[0], ids[50]}
    assert "未确认：2 个（状态 -1,1）" in str(error.value)
    assert error.value.retryable is False


def test_item_failure_then_errcode_stops_later_batches() -> None:
    # 防第 1 批逐项失败之后，第 2 批 errcode 仍发第 3 批，或丢掉第 1 批已经确认的 ID。
    ids = _ids(120)
    failed = {ids[0], ids[1]}
    requests, waits = [], []

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        if len(requests) == 2:
            return httpx.Response(
                200, json={"errcode": 40013, "errmsg": "https://secret.example/gone"}
            )
        return _ok(pending, status_of=lambda file_id: 1 if file_id in failed else 0)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies.delete_copies(client, ENV, ids, sleep=waits.append)
    message = str(error.value)
    assert requests == [ids[:50], ids[50:100]]
    assert waits == []
    assert "未确认" in message and "40013" in message
    assert "https://" not in message
    assert error.value.confirmed == set(ids[:50]) - failed


def test_missing_id_stops_before_next_batch() -> None:
    # 防第 2 批少返回一个 ID 时仍发第 3 批，或把没出现在 delete_list 里的 ID 算成已删除。
    ids = _ids(120)
    missing = ids[50]
    requests, waits = [], []

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        listed = pending[1:] if len(requests) == 2 else pending
        return _ok(listed)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies.delete_copies(client, ENV, ids, sleep=waits.append)
    assert requests == [ids[:50], ids[50:100]]
    assert waits == []
    assert error.value.confirmed == set(ids[:100]) - {missing}
    assert missing not in error.value.confirmed
    assert "漏项或无效响应" in str(error.value)
    assert error.value.retryable is False


def test_decoding_error_reports_class_name_only() -> None:
    # 防响应解码失败时继续发下一批、丢掉第 1 批已确认的 ID，或把 URL 写进报错。
    ids = _ids(120)
    requests, waits = [], []

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        if len(requests) == 2:
            raise httpx.DecodingError("x", request=request)
        return _ok(pending)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies.delete_copies(client, ENV, ids, sleep=waits.append)
    message = str(error.value)
    assert requests == [ids[:50], ids[50:100]]
    assert waits == []
    assert error.value.confirmed == set(ids[:50])
    assert "DecodingError" in message
    assert "http://" not in message and "api.weixin.qq.com" not in message
    for file_id in ids[:50]:
        assert file_id not in message


def test_on_confirmed_skips_batch_without_success() -> None:
    # 防某一批一个都没确认时仍调用回调，或回调没拿到另外两批已经确认的 ID。
    ids = _ids(120)
    failed = set(ids[50:100])
    requests, waits, calls = [], [], []

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        return _ok(pending, status_of=lambda file_id: 1 if file_id in failed else 0)

    def on_confirmed(confirmed: set[str]) -> None:
        calls.append(set(confirmed))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError),
    ):
        copies.delete_copies(client, ENV, ids, sleep=waits.append, on_confirmed=on_confirmed)
    assert requests == [ids[:50], ids[50:100], ids[100:]]
    assert calls == [set(ids[:50]), set(ids[100:])]
    assert waits == []


def test_on_confirmed_exception_stops_immediately() -> None:
    # 防结算回调抛出的异常被包成删除错误，或回调失败后仍发送后面的批次。
    ids = _ids(120)
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        return _ok(pending)

    def on_confirmed(_confirmed: set[str]) -> None:
        raise RuntimeError("提交失败")

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(RuntimeError, match="提交失败") as error,
    ):
        copies.delete_copies(client, ENV, ids, sleep=lambda _delay: None, on_confirmed=on_confirmed)
    assert type(error.value) is RuntimeError
    assert requests == [ids[:50]]


def test_salt_changes_which_ids_fit_in_the_round() -> None:
    # 防非空 salt 每轮都删同一批 500 个，或空 salt 打乱调用方传入的顺序。
    ids = _ids(600)
    waits: list[float] = []

    def sent(salt: str) -> list[str]:
        got: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            pending = json.loads(request.content)["fileid_list"]
            got.extend(pending)
            return _ok(pending)

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            deleted = copies.delete_copies(client, ENV, ids, sleep=waits.append, salt=salt)
        assert deleted == set(got)
        return got

    first, second = sent("salt-a"), sent("salt-b")
    assert len(first) == len(second) == 500
    assert set(first) != set(second)
    assert set(first) <= set(ids) and set(second) <= set(ids)
    assert sent("") == ids[:500]
    assert waits == []


def test_cleanup_keeps_unconfirmed_stale_and_drops_expired(session) -> None:
    # 防清理时把未确认的旧副本从记录里清掉、到期记录没删，或 removed 不等于已确认个数。
    stale_ids = _ids(120)
    failed = set(stale_ids[:50])
    url_a = "https://example.gov.cn/cleanup-a.pdf"
    url_b = "https://example.gov.cn/cleanup-b.pdf"
    expired_url = "https://example.gov.cn/cleanup-expired.pdf"
    expired_body = b"expired-only"
    row_a = add_copy(session, url_a, PDF, "pdf")
    row_b = add_copy(session, url_b, PDF + b"b", "pdf")
    row_c = add_copy(session, expired_url, expired_body, "pdf")
    assert row_c.file_id == PREFIX + obj(expired_body, "pdf")
    assert {row_a.file_id, row_b.file_id, row_c.file_id}.isdisjoint(stale_ids)
    expired_file_id = row_c.file_id
    row_a.stale_file_ids = stale_ids[:60]
    row_b.stale_file_ids = stale_ids[60:]
    session.commit()
    report = copies.CacheReport()

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        return _ok(
            pending,
            status_of=lambda file_id: 1 if file_id in failed else 0,
            errmsg_of=lambda file_id: "STORAGE_FILE_NONEXIST" if file_id in failed else None,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies._cleanup(session, client, ENV, {row_a.url_hash, row_b.url_hash}, report)
    session.expire_all()
    rows = {row.url: row for row in session.scalars(select(AttachmentCopy))}
    assert expired_url not in rows
    assert rows[url_a].stale_file_ids == stale_ids[:50]
    assert rows[url_b].stale_file_ids is None
    assert error.value.confirmed == set(stale_ids[50:]) | {expired_file_id}
    assert report.removed == len(error.value.confirmed)


def test_connect_error_keeps_first_batch_commit(session, monkeypatch) -> None:
    # 防第 2 批网络失败重试耗尽后，把第 1 批已经提交的旧 ID 结算回滚掉，或仍发第 3 批。
    stale_ids = _ids(120)
    row = add_copy(session, "https://example.gov.cn/cleanup-connect.pdf", PDF, "pdf")
    current = row.file_id
    row.stale_file_ids = list(stale_ids)
    session.commit()
    requests, waits = [], []
    retry = copies._retrying
    monkeypatch.setattr(copies, "_retrying", lambda action, sleep: retry(action, waits.append))

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        requests.append(pending)
        if len(requests) >= 2:
            raise httpx.ConnectError("connect")
        return _ok(pending)

    report = copies.CacheReport()
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(copies.DeleteCopiesError) as error,
    ):
        copies._cleanup(session, client, ENV, {row.url_hash}, report)
    session.expire_all()
    assert waits == [5, 15]
    assert requests == [stale_ids[:50], *([stale_ids[50:100]] * 3)]
    assert error.value.confirmed == set(stale_ids[:50])
    assert error.value.retryable is True
    assert "网络失败" in str(error.value)
    assert row.stale_file_ids == stale_ids[50:]
    assert row.file_id == current
    assert report.removed == len(error.value.confirmed)


def test_recheck_partial_delete_reports_confirmed_removals(session) -> None:
    # 防手动重查只删掉一部分时 removed 写成 0，或 storage_error 没有删除失败。
    stale_ids = _ids(4)
    failed = {stale_ids[0]}
    confirmed: list[str] = []
    row = add_copy(
        session,
        "https://example.gov.cn/recheck-stale.pdf",
        b"",
        "pdf",
        status="failed",
        failure="network",
    )
    row.stale_file_ids = list(stale_ids)
    session.commit()

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        for file_id in pending:
            if file_id not in failed:
                confirmed.append(file_id)
        return _ok(
            pending,
            status_of=lambda file_id: 1 if file_id in failed else 0,
            errmsg_of=lambda file_id: "STORAGE_FILE_NONEXIST" if file_id in failed else None,
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        report = copies.recheck_copies(session, ENV, client=client, now=NOW)
    assert set(confirmed) == set(stale_ids) - failed
    assert report.removed == len(confirmed)
    assert report.storage_error and report.storage_error.startswith("删除附件副本失败")


def test_cache_run_appends_delete_error_after_upload_error(session, monkeypatch) -> None:
    # 防上传已经失败时，后面的删除失败把 storage_error 里的上传错误盖掉。
    stale_ids = _ids(2)
    failed = {stale_ids[0]}
    row = add_copy(
        session,
        "https://example.gov.cn/cache-old.pdf",
        b"",
        "pdf",
        status="failed",
        failure="network",
    )
    row.stale_file_ids = list(stale_ids)
    session.commit()
    monkeypatch.setattr(
        copies,
        "candidates",
        lambda _session, _now, **_kwargs: {"https://example.gov.cn/cache-new.pdf": "pdf"},
    )

    def broken_upload(*_args, **_kwargs) -> None:
        raise copies.BackupError("上传坏了")

    monkeypatch.setattr(copies, "_check_one", broken_upload)

    def handle(request: httpx.Request) -> httpx.Response:
        pending = json.loads(request.content)["fileid_list"]
        return _ok(pending, status_of=lambda file_id: 1 if file_id in failed else 0)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        report = copies._cache_run(
            session,
            ENV,
            client=client,
            fetcher=object(),
            now=NOW,
            limit=100,
            max_bytes=copies.RUN_BYTES,
            retry_failed=False,
            recheck_limit=0,
            sleep=lambda _delay: None,
        )
    text = report.storage_error or ""
    assert "上传附件副本失败" in text and "删除附件副本失败" in text
    assert "上传坏了" in text
    assert text.index("上传附件副本失败") < text.index("删除附件副本失败")
