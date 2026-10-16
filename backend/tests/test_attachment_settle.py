"""删不掉的附件副本凭对象存储清单结算（youju settle-missing-copies）：清单解析、只读统计、
证据齐全才结算、留档失败不结算、清单可疑时不动。开放接口全部模拟，不联网。"""

import base64
import json
from datetime import timedelta
from hashlib import sha256

import httpx
import pytest

from tests.test_attachments import ENV, NOW, PREFIX
from youju import attachments as copies
from youju.models import AttachmentCopy

BUCKET = "7072-prod-1-1258"
OPEN = "http://api.weixin.qq.com"


def sha(n: int) -> str:
    return sha256(f"content {n}".encode()).hexdigest()


def fid(n: int, *, prefix: str = PREFIX) -> str:
    return f"{prefix}attachments/{sha(n)}.pdf"


def listing_text(present: list[int], *, listed_at=NOW, bucket=BUCKET, **override) -> str:
    """和 scripts/attachment_listing.py 一样的格式。"""
    heads = sorted({bytes.fromhex(sha(n)[:8]) for n in present})
    payload = b"".join(heads)
    head = {
        "v": 1,
        "bucket": bucket,
        "prefix": "attachments/",
        "listed_at": listed_at.isoformat(),
        "count": len(heads),
        "sha256": sha256(payload).hexdigest(),
    }
    head.update(override)
    text = base64.b64encode(payload).decode()
    return "\n".join([json.dumps(head), *(text[i : i + 76] for i in range(0, len(text), 76))])


class Cloud:
    """删除按 statuses 返回（没写的算 0）；上传留档记下内容。"""

    def __init__(
        self, statuses=None, *, missing=(), delete_errcode=0, upload_errcode=0, link_errcode=0
    ):
        self.statuses = statuses or {}
        # 平台拒绝给下载链接的（对象不在）；其余都给 https 链接
        self.missing = set(missing)
        self.delete_errcode, self.upload_errcode = delete_errcode, upload_errcode
        self.link_errcode = link_errcode
        self.requests: list[str] = []
        self.deleted: list[str] = []
        self.linked: list[str] = []
        self.audits: list[dict] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == f"{OPEN}/tcb/batchdownloadfile":
            if self.link_errcode:
                return httpx.Response(200, json={"errcode": self.link_errcode})
            ids = [item["fileid"] for item in json.loads(request.content)["file_list"]]
            self.linked += ids
            items = [
                {"fileid": i, "status": 1, "errmsg": "STORAGE_FILE_NONEXIST"}
                if i in self.missing
                else {"fileid": i, "status": 0, "download_url": "https://b.example.test/x"}
                for i in ids
            ]
            return httpx.Response(200, json={"errcode": 0, "file_list": items})
        if url == f"{OPEN}/tcb/batchdeletefile":
            if self.delete_errcode:
                return httpx.Response(200, json={"errcode": self.delete_errcode})
            ids = json.loads(request.content)["fileid_list"]
            items = []
            for i in ids:
                status, errmsg = self.statuses.get(i, (0, "ok"))
                items.append({"fileid": i, "status": status, "errmsg": errmsg})
                if status == 0:
                    self.deleted.append(i)
            return httpx.Response(200, json={"errcode": 0, "delete_list": items})
        if url == f"{OPEN}/tcb/uploadfile":
            if self.upload_errcode:
                return httpx.Response(200, json={"errcode": self.upload_errcode})
            path = json.loads(request.content)["path"]
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "url": "https://cos.example.test/upload",
                    "token": "t",
                    "authorization": "a",
                    "cos_file_id": "c",
                    "file_id": PREFIX + path,
                },
            )
        if url == "https://cos.example.test/upload":
            body = request.content
            start = body.index(b"{")
            self.audits.append(json.loads(body[start : body.rindex(b"}") + 1]))
            return httpx.Response(204)
        return httpx.Response(404)


def copy_row(session, n: int, *, file_id=None, stale=None, status="ready", fetched=None):
    row = AttachmentCopy(
        url_hash=copies.url_hash(f"https://example.gov.cn/{n}.pdf"),
        url=f"https://example.gov.cn/{n}.pdf",
        declared_type="pdf",
        status=status,
        rules_version=copies.RULES_VERSION,
    )
    if status == "ready":
        row.file_type, row.size, row.sha256 = "pdf", 10, sha(n)
        row.file_id = file_id or fid(n)
        row.fetched_at = fetched or NOW - timedelta(days=1)
    row.stale_file_ids = stale
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def live(monkeypatch):
    """还要提供的附件链接（定时清理的 live 口径）。"""
    urls: dict[str, str] = {}
    monkeypatch.setattr(copies, "candidates", lambda session, now: dict(urls))
    return urls


def setup_rows(session, live):
    # 1 号还在提供、对象在；2 号还在提供、对象不在（会打不开）；3 号到期、对象不在。
    # 1 号的旧副本：11 号不在、12 号在
    one = copy_row(session, 1, stale=[fid(11), fid(12)])
    two = copy_row(session, 2)
    three = copy_row(session, 3)
    live.update({one.url: "pdf", two.url: "pdf"})
    return one, two, three


def test_parse_listing_round_trip_and_prefix_membership():
    listing = copies.parse_listing(listing_text([1, 2, 12]), NOW)
    assert listing.bucket == BUCKET and listing.count == 3
    assert listing.has(fid(1)) and listing.has(fid(12)) and not listing.has(fid(3))
    # 认不出的文件 ID 一律按"可能还在"算，不会被结算
    assert listing.has("cloud://prod-1.x/backups/a.gz")


@pytest.mark.parametrize(
    "text,reason",
    [
        (listing_text([1], listed_at=NOW - timedelta(hours=25)), "24 小时"),
        (listing_text([1], listed_at=NOW + timedelta(hours=1)), "24 小时"),
        (listing_text([1], prefix="backups/"), "前缀"),
        (listing_text([1], bucket="Bad Bucket"), "存储桶"),
        (listing_text([1], sha256="0" * 64), "校验和"),
        (listing_text([1, 2], count=3), "条数"),
        (listing_text([1]).replace("+00:00", ""), "时区"),
        (listing_text([1]).splitlines()[0], "空的"),
        ("", "格式"),
        ("not json\nAAAA", "格式"),
    ],
)
def test_parse_listing_rejects_stale_partial_or_wrong_listings(text, reason):
    with pytest.raises(ValueError, match=reason):
        copies.parse_listing(text, NOW)


def test_parse_listing_rejects_unsorted_payload():
    heads = [bytes.fromhex(sha(2)[:8]), bytes.fromhex(sha(1)[:8])]
    heads.sort(reverse=True)
    payload = b"".join(heads)
    head = {
        "v": 1,
        "bucket": BUCKET,
        "prefix": "attachments/",
        "listed_at": NOW.isoformat(),
        "count": 2,
        "sha256": sha256(payload).hexdigest(),
    }
    text = json.dumps(head) + "\n" + base64.b64encode(payload).decode()
    with pytest.raises(ValueError, match="排好序"):
        copies.parse_listing(text, NOW)


def test_dry_run_only_counts_and_never_calls_the_platform(session, live):
    one, two, three = setup_rows(session, live)
    cloud = Cloud()
    listing = copies.parse_listing(listing_text([1, 12]), NOW)
    report = copies.settle_missing_copies(session, ENV, listing, client=cloud.client, now=NOW)
    assert cloud.requests == []
    assert (report.wanted, report.listed, report.unlisted, report.bucket_mismatch) == (3, 1, 2, 0)
    assert report.live_missing == 1 and report.live_reset == 0 and not report.applied
    session.expire_all()
    assert one.stale_file_ids == [fid(11), fid(12)] and two.file_id == fid(2)
    assert session.get(AttachmentCopy, three.id) is not None


def test_apply_settles_only_status1_not_found_absent_from_listing(session, live):
    one, two, three = setup_rows(session, live)
    cloud = Cloud(
        {
            fid(3): (1, "STORAGE_FILE_NONEXIST"),
            fid(11): (1, "file not exist"),
        },
        missing={fid(2), fid(3), fid(11)},
    )
    listing = copies.parse_listing(listing_text([1, 12]), NOW)
    report = copies.settle_missing_copies(
        session, ENV, listing, client=cloud.client, now=NOW, apply=True
    )
    assert report.error is None
    assert (report.deleted, report.unconfirmed, report.settled, report.live_reset) == (1, 2, 2, 1)
    assert (report.link_issued, report.link_unknown, report.live_link_issued) == (0, 0, 0)
    assert cloud.deleted == [fid(12)]
    # 只对清单里没有的问下载链接，清单里有的、已确认删掉的不问
    assert sorted(cloud.linked) == sorted([fid(2), fid(3), fid(11)])
    assert report.audit and report.audit.startswith("ops/attachment-settlements/")
    [audit] = cloud.audits
    assert {item["file_id"] for item in audit["settled"]} == {fid(3), fid(11)}
    assert audit["listing"]["sha256"] == listing.digest
    assert [item["file_id"] for item in audit["reset_to_pending"]] == [fid(2)]
    session.expire_all()
    assert one.stale_file_ids is None and one.file_id == fid(1)
    assert two.status == "pending" and two.file_id is None and two.next_check_at is None
    assert session.get(AttachmentCopy, three.id) is None


def test_apply_keeps_listed_other_status_and_other_bucket(session, live):
    other = "cloud://prod-1.7072-other-9/"
    one = copy_row(session, 1, stale=[fid(21), fid(22), fid(23, prefix=other)])
    live[one.url] = "pdf"
    cloud = Cloud(
        {
            # 清单里有却说不存在：平台提示和清单矛盾，不结算
            fid(21): (1, "STORAGE_FILE_NONEXIST"),
            # 不是状态 1 + 不存在提示：不结算
            fid(22): (40097, "STORAGE_EXCEED_AUTHORITY"),
            fid(23, prefix=other): (1, "STORAGE_FILE_NONEXIST"),
        }
    )
    listing = copies.parse_listing(listing_text([1, 21]), NOW)
    report = copies.settle_missing_copies(
        session, ENV, listing, client=cloud.client, now=NOW, apply=True
    )
    assert report.bucket_mismatch == 1 and report.listed_unconfirmed == 1
    assert report.other_status == 1 and report.settled == 0 and report.audit is None
    assert cloud.audits == []
    session.expire_all()
    assert set(one.stale_file_ids) == {fid(21), fid(22), fid(23, prefix=other)}


def test_audit_upload_failure_settles_nothing_by_listing(session, live):
    one, two, three = setup_rows(session, live)
    cloud = Cloud(
        {fid(3): (1, "STORAGE_FILE_NONEXIST"), fid(11): (1, "file not exist")},
        missing={fid(2), fid(3), fid(11)},
        upload_errcode=40013,
    )
    listing = copies.parse_listing(listing_text([1, 12]), NOW)
    report = copies.settle_missing_copies(
        session, ENV, listing, client=cloud.client, now=NOW, apply=True, sleep=lambda _: None
    )
    assert report.error and "40013" in report.error and report.settled == 0
    session.expire_all()
    # 照常删掉的 12 号已经结算；按清单的 11 号、3 号和改回待下载都没有发生
    assert one.stale_file_ids == [fid(11)]
    assert two.status == "ready" and two.file_id == fid(2)
    assert session.get(AttachmentCopy, three.id).file_id == fid(3)


def test_link_issued_means_object_still_there_and_vetoes_settlement(session, live):
    # 清单漏列或取清单时正在上传：平台给了下载链接，对象就还在，既不结算也不改回待下载
    one, two, three = setup_rows(session, live)
    cloud = Cloud(
        {fid(3): (1, "STORAGE_FILE_NONEXIST"), fid(11): (1, "file not exist")},
        missing={fid(11)},
    )
    listing = copies.parse_listing(listing_text([1, 12]), NOW)
    report = copies.settle_missing_copies(
        session, ENV, listing, client=cloud.client, now=NOW, apply=True
    )
    assert report.error is None
    assert (report.settled, report.link_issued, report.link_unknown) == (1, 1, 0)
    assert (report.live_missing, report.live_reset, report.live_link_issued) == (1, 0, 1)
    [audit] = cloud.audits
    assert [item["file_id"] for item in audit["settled"]] == [fid(11)]
    assert audit["reset_to_pending"] == []
    session.expire_all()
    assert one.stale_file_ids is None
    assert two.status == "ready" and two.file_id == fid(2)
    assert session.get(AttachmentCopy, three.id).file_id == fid(3)


def test_link_check_failure_settles_nothing_by_listing(session, live):
    # 下载链接接口出错：说不清对象在不在，按清单的一个都不结算、不改回待下载
    one, two, three = setup_rows(session, live)
    cloud = Cloud(
        {fid(3): (1, "STORAGE_FILE_NONEXIST"), fid(11): (1, "file not exist")},
        missing={fid(2), fid(3), fid(11)},
        link_errcode=40013,
    )
    listing = copies.parse_listing(listing_text([1, 12]), NOW)
    report = copies.settle_missing_copies(
        session, ENV, listing, client=cloud.client, now=NOW, apply=True, sleep=lambda _: None
    )
    assert report.error is None and report.settled == 0 and report.live_reset == 0
    assert (report.link_unknown, report.live_link_unknown) == (2, 1)
    assert cloud.audits == []
    session.expire_all()
    assert one.stale_file_ids == [fid(11)]
    assert two.status == "ready" and session.get(AttachmentCopy, three.id).file_id == fid(3)


def test_link_states_are_strict():
    # 只有唯一一条、整数 0、https 链接算"给了链接"；忙码、漏项、重复、非整数都算说不清
    ids = [fid(n) for n in range(6)]

    def handle(request):
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "file_list": [
                    {"fileid": ids[0], "status": 0, "download_url": "https://b.example.test/0"},
                    {"fileid": ids[1], "status": 1},
                    {"fileid": ids[2], "status": -1},
                    {"fileid": ids[3], "status": "1"},
                    {"fileid": ids[4], "status": 0, "download_url": "https://x"},
                    {"fileid": ids[4], "status": 0, "download_url": "https://x"},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        states = copies._link_states(client, ENV, set(ids), lambda _: None)
    assert states == {
        ids[0]: "issued",
        ids[1]: "refused",
        ids[2]: "unknown",
        ids[3]: "unknown",
        ids[4]: "unknown",
        ids[5]: "unknown",
    }


def test_delete_stopping_midway_skips_listing_settlement(session, live):
    one, _two, three = setup_rows(session, live)
    cloud = Cloud(delete_errcode=40013)
    listing = copies.parse_listing(listing_text([1, 12]), NOW)
    report = copies.settle_missing_copies(
        session, ENV, listing, client=cloud.client, now=NOW, apply=True
    )
    assert report.error and "中途出错" in report.error and report.settled == 0
    assert cloud.audits == []
    session.expire_all()
    assert set(one.stale_file_ids) == {fid(11), fid(12)}
    assert session.get(AttachmentCopy, three.id).file_id == fid(3)


def test_listing_without_any_serving_copy_is_refused(session, live):
    setup_rows(session, live)
    cloud = Cloud()
    listing = copies.parse_listing(listing_text([99]), NOW)
    report = copies.settle_missing_copies(
        session, ENV, listing, client=cloud.client, now=NOW, apply=True
    )
    assert report.error and "存储桶" in report.error
    assert cloud.requests == []


def test_copies_stored_after_the_listing_are_not_reset(session, live):
    one = copy_row(session, 1)
    late = copy_row(session, 2, fetched=NOW + timedelta(minutes=5))
    live.update({one.url: "pdf", late.url: "pdf"})
    cloud = Cloud()
    listing = copies.parse_listing(listing_text([1]), NOW)
    report = copies.settle_missing_copies(
        session, ENV, listing, client=cloud.client, now=NOW + timedelta(minutes=6), apply=True
    )
    assert report.live_missing == 0 and report.live_reset == 0
    session.expire_all()
    assert late.status == "ready" and late.file_id == fid(2)


def test_busy_attachment_job_skips(session, live, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def busy(_session):
        yield False

    monkeypatch.setattr(copies, "_exclusive", busy)
    listing = copies.parse_listing(listing_text([1]), NOW)
    report = copies.settle_missing_copies(session, ENV, listing, now=NOW, apply=True)
    assert report.skipped


def test_listing_script_format_matches_parser(monkeypatch):
    """scripts/attachment_listing.py 生成的文本能被 parse_listing 读回。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "scripts" / "attachment_listing.py"
    spec = importlib.util.spec_from_file_location("attachment_listing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pages = [
        {
            "Name": BUCKET,
            "IsTruncated": "true",
            "Contents": [{"Key": f"attachments/{sha(1)}.pdf"}, {"Key": "attachments/x.txt"}],
        },
        {
            "Name": BUCKET,
            "IsTruncated": "false",
            # 大写或其他扩展名也列入（列多了只会多保留）
            "Contents": [
                {"Key": f"attachments/{sha(2)}.doc"},
                {"Key": f"attachments/{sha(3).upper()}.PDF"},
            ],
        },
    ]
    markers = []

    def page(env, marker):
        markers.append(marker)
        return pages[len(markers) - 1]

    monkeypatch.setattr(module, "_page", page)
    bucket, heads, others = module.listing("prod-1")
    assert bucket == BUCKET and others == 1 and markers == [None, "attachments/x.txt"]
    assert heads == sorted({bytes.fromhex(sha(n)[:8]) for n in (1, 2, 3)})
