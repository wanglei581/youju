"""对象存储的 disk、s3 后端（2.0 自建服务器）：照云托管存储接口的格式做，原来的代码不用改。"""

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from youju import backup, storage, storage_list
from youju.config import get_settings

ENV = "prod-env"


@pytest.fixture
def disk(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUJU_STORAGE_BACKEND", "disk")
    monkeypatch.setenv("YOUJU_STORAGE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("YOUJU_CLOUD_ENV", ENV)
    monkeypatch.setenv("YOUJU_STORAGE_BUCKET", "7072-old-bucket-123")
    get_settings.cache_clear()
    yield tmp_path / "store"
    get_settings.cache_clear()


def test_default_is_cloudbase_and_client_unchanged():
    get_settings.cache_clear()
    assert not storage.emulated()
    with storage.http_client() as client:
        assert client.trust_env is False
        assert not client._mounts


def test_paths_and_file_ids():
    assert storage.valid_path("attachments/" + "a" * 64 + ".pdf")
    for bad in ("", "/abs", "a/../b", "a//b", "a/./b", "a/b/", "中文/x", "a b"):
        assert not storage.valid_path(bad), bad
    fid = f"cloud://{ENV}.any-bucket-name/backups/youju-20261105.jsonl.gz"
    assert storage.path_of(fid) == "backups/youju-20261105.jsonl.gz"
    assert storage.path_of("cloud://x/../etc/passwd") is None
    assert storage.path_of(None) is None


def test_upload_download_delete_through_original_backup_code(disk, tmp_path):
    local = tmp_path / "f.gz"
    local.write_bytes(b"0123456789" * 1000)
    client = storage.http_client()
    fid = backup.upload(client, ENV, "backups/youju-20261105.jsonl.gz", local)
    # 文件 ID 的写法和云托管一样（存储桶名用配的，切换时填原来的）
    assert fid == f"cloud://{ENV}.7072-old-bucket-123/backups/youju-20261105.jsonl.gz"
    assert (disk / "backups/youju-20261105.jsonl.gz").read_bytes() == local.read_bytes()
    # 换下载链接：原来的 _open_api 调用照旧
    data = backup._open_api(
        client,
        "/tcb/batchdownloadfile",
        {"env": ENV, "file_list": [{"fileid": fid, "max_age": 60}]},
    )
    item = data["file_list"][0]
    assert item["status"] == 0 and item["download_url"].startswith("https://storage.youju.invalid/")
    # 分段读（附件副本、批量工具结果就是这样读的）
    part = client.get(item["download_url"], headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == b"0123456789"
    assert part.headers["content-range"] == "bytes 10-19/10000"
    whole = client.get(item["download_url"])
    assert whole.status_code == 200 and len(whole.content) == 10000
    # 没有的文件：status 不是 0（调用方当"没有"处理）
    missing = backup._open_api(
        client,
        "/tcb/batchdownloadfile",
        {"env": ENV, "file_list": [{"fileid": fid.replace("1105", "1104"), "max_age": 60}]},
    )
    assert missing["file_list"][0]["status"] != 0
    # 删除：删掉的、本来就没有的都算成功；文件 ID 不对的不算
    deleted = backup._open_api(
        client,
        "/tcb/batchdeletefile",
        {"env": ENV, "fileid_list": [fid, fid + "x", "cloud://bad"]},
    )
    assert [i["status"] for i in deleted["delete_list"]] == [0, 0, -1]
    assert not (disk / "backups/youju-20261105.jsonl.gz").exists()
    client.close()


def test_prefix_probe_and_listing_for_storage_list(disk, tmp_path):
    local = tmp_path / "x"
    local.write_bytes(b"x")
    storage.upload(ENV, "advisor/out/u1/1.xlsx", local)
    storage.upload(ENV, "advisor/out/u2/2.xlsx", local)
    storage.upload(ENV, "backups/b.gz", local)
    bucket = storage_list.bucket_of(None, ENV)
    assert bucket.prefix == f"cloud://{ENV}.7072-old-bucket-123/"
    objects = storage_list.list_objects(None, bucket, "advisor/out/")
    assert [o.key for o in objects] == ["advisor/out/u1/1.xlsx", "advisor/out/u2/2.xlsx"]
    # 别的客户端不认这个保留域名
    with httpx.Client() as plain, pytest.raises(httpx.HTTPError):
        plain.get("https://storage.youju.invalid/o/backups/b.gz", timeout=2)


def test_disk_transport_rejects_traversal_and_bad_ranges(disk, tmp_path):
    (disk / "x").mkdir(parents=True)
    (disk / "x" / "f").write_bytes(b"abc")
    transport = storage.DiskTransport(storage.DiskBackend(disk))
    client = httpx.Client(mounts={storage.FAKE_ORIGIN: transport})
    base = storage.FAKE_ORIGIN + "/o/"
    assert client.get(base + "x/f").content == b"abc"
    assert client.get(base + "x/%2E%2E/x/f").status_code == 404
    assert client.get(base + "x/f", headers={"Range": "bytes=5-9"}).status_code == 416
    assert client.get(base + "x/f", headers={"Range": "bytes=1-"}).content == b"bc"
    assert client.post(base + "x/f").status_code == 405


def test_backup_round_trip_on_disk(disk, db_engine, session_factory):
    """整库备份存到 disk、再用原来的 download_latest 取回来、校验通过（备份校验任务就是这样）。"""
    report = backup.run_backup(db_engine, ENV, now=datetime(2026, 11, 5, 4, tzinfo=UTC))
    assert report.file_id.endswith("backups/youju-20261105.jsonl.gz")
    dest = disk.parent / "restored.gz"
    with storage.http_client() as client:
        name = backup.download_latest(
            client, ENV, dest, today=datetime(2026, 11, 6).date(), sleep=lambda s: None
        )
    assert name == "backups/youju-20261105.jsonl.gz"
    with dest.open("rb") as fh:
        backup.verify(db_engine, fh)


# ---- 兼容 S3（AWS 签名 V4）----

AWS = storage.S3Config(
    "https://s3.amazonaws.com",
    "us-east-1",
    "examplebucket",
    "AKIAIOSFODNN7EXAMPLE",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
)
WHEN = datetime(2013, 5, 24, tzinfo=UTC)


def test_sigv4_matches_aws_documented_examples():
    """AWS S3 文档"Signature Calculations for the Authorization Header"和"Query String
    Authentication"里的两个例子（GET /test.txt）。"""
    headers = storage.sigv4_headers(
        AWS,
        "GET",
        "examplebucket.s3.amazonaws.com",
        "/test.txt",
        {},
        {"range": "bytes=0-9"},
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        WHEN,
    )
    assert headers["authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    url = storage.presign_url(AWS, "examplebucket.s3.amazonaws.com", "/test.txt", 86400, WHEN)
    assert url.endswith(
        "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )
    assert repr(AWS).find("wJalr") == -1  # 密钥不进日志


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=ak/")
        assert request.url.host == "bucket-1250000000.cos.ap-shanghai.myqcloud.com"
        key = request.url.path.lstrip("/")
        if request.method == "PUT":
            body = request.read()
            assert request.headers["x-amz-content-sha256"] != "UNSIGNED-PAYLOAD"
            self.objects[key] = body
            return httpx.Response(200)
        if request.method == "HEAD":
            return httpx.Response(200 if key in self.objects else 404)
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        if request.method == "GET" and not key:
            prefix = request.url.params.get("prefix", "")
            marker = request.url.params.get("marker", "")
            keys = sorted(k for k in self.objects if k.startswith(prefix) and k > marker)
            page, truncated = keys[:2], len(keys) > 2
            items = "".join(
                f"<Contents><Key>{k}</Key><LastModified>2026-11-05T01:02:03.000Z</LastModified>"
                f"<Size>{len(self.objects[k])}</Size></Contents>"
                for k in page
            )
            xml = (
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>{str(truncated).lower()}</IsTruncated>{items}</ListBucketResult>"
            )
            return httpx.Response(200, content=xml.encode())
        return httpx.Response(400, content=b"<Error><Code>Bad</Code></Error>")


def test_s3_backend_against_fake_cos(tmp_path):
    fake = FakeS3()
    config = storage.S3Config(
        "https://cos.ap-shanghai.myqcloud.com", "ap-shanghai", "bucket-1250000000", "ak", "sk"
    )
    backend = storage.S3Backend(
        config, http=httpx.Client(transport=httpx.MockTransport(fake.handle))
    )
    local = tmp_path / "f"
    local.write_bytes(b"hello")
    for n in range(5):
        backend.put_file(f"backups/f{n}", local, headers={"x-cos-storage-class": "ARCHIVE"})
    assert fake.requests[0].headers["x-cos-storage-class"] == "ARCHIVE"
    assert "x-cos-storage-class" in fake.requests[0].headers["authorization"]
    assert backend.exists("backups/f1") and not backend.exists("backups/none")
    listed = backend.list("backups/")
    assert [o.key for o in listed] == [f"backups/f{n}" for n in range(5)]
    assert listed[0].size == 5 and listed[0].last_modified.year == 2026
    backend.delete("backups/f1")
    assert not backend.exists("backups/f1")
    url = backend.url("backups/f2", 600)
    assert url.startswith("https://bucket-1250000000.cos.ap-shanghai.myqcloud.com/backups/f2?")
    assert "X-Amz-Expires=600" in url and re.search(r"X-Amz-Signature=[0-9a-f]{64}$", url)


def test_s3_errors_carry_code_not_secrets():
    def handle(request):
        return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>")

    config = storage.S3Config("https://minio.local", "us-east-1", "b", "ak", "very-secret", True)
    backend = storage.S3Backend(config, http=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(storage.StorageError) as err:
        backend.exists("x")
    assert "AccessDenied" in str(err.value) and "very-secret" not in str(err.value)


def test_open_api_unknown_endpoint(disk):
    assert storage.open_api("/tcb/other", {"env": ENV})["errcode"] != 0
    assert storage.open_api("/tcb/uploadfile", {"env": ENV, "path": "../x"})["errcode"] != 0
    assert json.dumps(SimpleNamespace.__name__)


def test_storage_export_and_import_keep_paths(disk, tmp_path, monkeypatch):
    """切换时搬文件：按原路径打包、换一个存储解开，文件 ID 只看路径，库里的不用改。"""
    from youju import storage_move

    local = tmp_path / "f"
    for path, body in (
        ("advisor/out/u1/1.xlsx", b"result"),
        ("logs/access-20261101.jsonl.gz", b"log"),
        ("backups/youju-20261105.jsonl.gz", b"not moved"),
    ):
        local.write_bytes(body)
        storage.upload(ENV, path, local)
    archive = tmp_path / "move.tar.gz"
    report = storage_move.export(ENV, archive, ("advisor/out/", "logs/"))
    assert report.files == {"advisor/out/": 1, "logs/": 1}
    monkeypatch.setenv("YOUJU_STORAGE_DIR", str(tmp_path / "new"))
    get_settings.cache_clear()
    moved = storage_move.import_archive(archive)
    assert moved.files == {"advisor/": 1, "logs/": 1}
    assert (tmp_path / "new/advisor/out/u1/1.xlsx").read_bytes() == b"result"
    assert not (tmp_path / "new/backups").exists()
    # 再导一次：已经有、内容一样的跳过
    assert storage_move.import_archive(archive).skipped == 2
    # 已经有、大小一样但内容不一样的：不当成搬好了，用包里的覆盖、记数
    (tmp_path / "new/logs/access-20261101.jsonl.gz").write_bytes(b"LOG")
    again = storage_move.import_archive(archive)
    assert again.replaced == 1 and again.skipped == 1
    assert (tmp_path / "new/logs/access-20261101.jsonl.gz").read_bytes() == b"log"


def test_storage_export_since_days_only_moves_recent(disk, tmp_path):
    """搬家时整库备份只搬近几天的（--since-days）。"""
    import os
    import time

    from youju import storage_move

    local = tmp_path / "f"
    local.write_bytes(b"backup")
    storage.upload(ENV, "backups/youju-20261101.jsonl.gz", local)
    storage.upload(ENV, "backups/youju-20261105.jsonl.gz", local)
    old = disk / "backups/youju-20261101.jsonl.gz"
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))
    archive = tmp_path / "b.tar.gz"
    report = storage_move.export(ENV, archive, ("backups/",), since_days=7)
    assert report.files == {"backups/": 1}
