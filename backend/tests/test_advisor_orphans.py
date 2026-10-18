"""批量工具结果文件巡检（youju advisor-orphans）：有记录的、待删表里的、任务还在排队的、不到 1 天的
都不动；默认只列不删，加 --delete 才删；删除前复查；对象存储出错时什么都不删。

开放接口和 COS 全部用测试替身，不联网；签名另用腾讯云文档里的示例核对。"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx
import pytest
from sqlalchemy import delete, select

from youju import advisor_orphans as orphans
from youju import cli, storage_list
from youju.backup import PROBE_PATH, BackupError
from youju.models import AdvisorFileTrash, AdvisorTask, User

ENV = "test-env"
BUCKET = "7465-test-env-1250000000"
PREFIX = f"cloud://{ENV}.{BUCKET}/"
OPEN = "http://api.weixin.qq.com"
NOW = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=40)
UID = "0123456789abcdef"
OTHER_UID = "fedcba9876543210"
SETTINGS = SimpleNamespace(cloud_env=ENV)
# 失效时间两小时后（取一次够用）；ExpiredTime 按文档是失效时的 Unix 时间戳
AUTH = {
    "TmpSecretId": "AKIDtest",
    "TmpSecretKey": "tmp-secret",
    "Token": "tmp-token",
    "ExpiredTime": int(time.time()) + 7200,
}


def no_sleep(_seconds):
    return None


def key(task_id, uid=UID):
    return f"advisor/out/{uid}/{task_id}.xlsx"


def check_signature(request: httpx.Request) -> None:
    """按收到的请求（实际发出的 host 和查询参数）重算签名，和请求带的一致才算对。"""
    fields = dict(part.split("=", 1) for part in request.headers["authorization"].split("&"))
    assert fields["q-sign-algorithm"] == "sha1" and fields["q-ak"] == AUTH["TmpSecretId"]
    key_time = fields["q-key-time"]
    assert fields["q-sign-time"] == key_time
    start, end = map(int, key_time.split(";"))
    assert start <= time.time() <= end
    params = sorted(request.url.params.multi_items())
    assert fields["q-url-param-list"] == ";".join(name for name, _ in params)
    assert fields["q-header-list"] == "host"
    query = "&".join(f"{name}={quote(value, safe='-_.~')}" for name, value in params)
    http = f"get\n{request.url.path}\n{query}\nhost={request.url.host}\n"
    sign_key = hmac.new(AUTH["TmpSecretKey"].encode(), key_time.encode(), hashlib.sha1).hexdigest()
    to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http.encode()).hexdigest()}\n"
    expected = hmac.new(sign_key.encode(), to_sign.encode(), hashlib.sha1).hexdigest()
    assert fields["q-signature"] == expected
    assert request.headers["x-cos-security-token"] == AUTH["Token"]


class Cloud:
    """开放接口（取临时密钥、申请上传链接、删除）和 COS 列文件的测试替身。
    objects 是对象存储里的文件：路径 → 最后修改时间。"""

    def __init__(
        self,
        objects,
        *,
        page_size=1000,
        fail_delete=(),
        on_list=None,
        auth=None,
        list_statuses=(),
        bucket_name=BUCKET,
        probe_env=ENV,
        region="ap-shanghai",
        upload_url=None,
    ):
        self.objects = dict(objects)
        self.page_size = page_size
        self.fail_delete = set(fail_delete)
        self.on_list = on_list
        self.auth = AUTH if auth is None else auth
        self.list_statuses = list(list_statuses)
        self.bucket_name = bucket_name
        self.probe_env = probe_env
        self.region = region
        self.upload_url = upload_url
        self.requests: list[str] = []
        self.auth_calls = 0
        self.markers: list[str] = []
        self.probe_paths: list[str] = []
        self.deleted: list[str] = []
        self.delete_batches: list[int] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def deletes(self) -> int:
        return len(self.delete_batches)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append(f"{request.method} {url.scheme}://{url.host}{url.path}")
        if request.method == "GET" and str(url) == f"{OPEN}/_/cos/getauth":
            self.auth_calls += 1
            return httpx.Response(200, json=self.auth)
        if request.method == "POST" and str(url) == f"{OPEN}/tcb/uploadfile":
            path = json.loads(request.content)["path"]
            self.probe_paths.append(path)
            bucket = BUCKET if self.probe_env == ENV else f"7465-{self.probe_env}-1"
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "url": self.upload_url
                    or f"https://cos.{self.region}.myqcloud.com/{bucket}/{path}",
                    "token": "upload-token",
                    "authorization": "upload-signature",
                    "file_id": f"cloud://{self.probe_env}.{bucket}/{path}",
                    "cos_file_id": "cos-file",
                },
            )
        if request.method == "GET" and url.host == f"{BUCKET}.cos.{self.region}.myqcloud.com":
            return self.list_page(request)
        if request.method == "POST" and str(url) == f"{OPEN}/tcb/batchdeletefile":
            file_ids = json.loads(request.content)["fileid_list"]
            self.delete_batches.append(len(file_ids))
            items = []
            for file_id in file_ids:
                path = file_id.removeprefix(PREFIX)
                if file_id in self.fail_delete or path not in self.objects:
                    items.append({"fileid": file_id, "status": 1, "errmsg": "delete failed"})
                    continue
                del self.objects[path]
                self.deleted.append(file_id)
                items.append({"fileid": file_id, "status": 0, "errmsg": "ok"})
            return httpx.Response(200, json={"errcode": 0, "delete_list": items})
        raise AssertionError(f"没想到的请求：{request.method} {url}")

    def list_page(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/"
        check_signature(request)
        if self.on_list is not None:
            self.on_list()
        status = self.list_statuses.pop(0) if self.list_statuses else 200
        if status != 200:
            body = (
                "<?xml version='1.0' encoding='utf-8' ?><Error><Code>AccessDenied</Code>"
                f"<Message>Access Denied q-ak={AUTH['TmpSecretId']}</Message></Error>"
            )
            return httpx.Response(status, content=body.encode())
        params = request.url.params
        prefix, marker = params["prefix"], params.get("marker", "")
        assert params["max-keys"] == "1000"
        self.markers.append(marker)
        keys = sorted(name for name in self.objects if name.startswith(prefix) and name > marker)
        page, more = keys[: self.page_size], len(keys) > self.page_size
        contents = "".join(
            f"<Contents><Key>{escape(name)}</Key>"
            f"<LastModified>{self.objects[name]:%Y-%m-%dT%H:%M:%S.000Z}</LastModified>"
            "<ETag>&quot;e&quot;</ETag><Size>10</Size><StorageClass>STANDARD</StorageClass>"
            "</Contents>"
            for name in page
        )
        body = (
            "<?xml version='1.0' encoding='utf-8' ?><ListBucketResult>"
            f"<Name>{self.bucket_name}</Name><Prefix>{escape(prefix)}</Prefix>"
            f"<Marker>{escape(marker)}</Marker><MaxKeys>1000</MaxKeys>"
            f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
            + (f"<NextMarker>{escape(page[-1])}</NextMarker>" if more else "")
            + contents
            + "</ListBucketResult>"
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/xml"}
        )


def add_task(session, user_id, idempotency_key, **values):
    row = AdvisorTask(
        user_id=user_id,
        kind="compare",
        status=values.pop("status", "done"),
        row_count=1,
        idempotency_key=idempotency_key,
        created_at=OLD,
        expires_at=OLD + timedelta(days=30),
        **values,
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def scene(session):
    """对象存储里 7 个文件：有记录、待删表里、任务还在排队、不到 1 天、不是结果文件各 1 个，
    孤儿 2 个（其中 1 个刚过 1 天）。"""
    owner = User(openid="orphan-owner")
    session.add(owner)
    session.commit()
    add_task(session, owner.id, "done", result_file_id=PREFIX + key(900))
    queued = add_task(session, owner.id, "queued", status="queued")
    session.add(AdvisorFileTrash(file_id=PREFIX + key(901)))
    session.commit()
    objects = {
        key(900): OLD,
        key(901): OLD,
        key(queued.id): OLD,
        key(902): NOW - orphans.MIN_AGE + timedelta(minutes=1),
        "advisor/out/readme.txt": OLD,
        key(903): NOW - orphans.MIN_AGE - timedelta(minutes=1),
        key(904, uid=OTHER_UID): OLD,
    }
    return SimpleNamespace(owner=owner, queued=queued, objects=objects)


def scan(session, cloud, **kwargs):
    return orphans.scan(session, SETTINGS, client=cloud.client, now=NOW, sleep=no_sleep, **kwargs)


def test_list_only_counts_and_deletes_nothing(session, scene):
    cloud = Cloud(scene.objects)
    report = scan(session, cloud)
    counts = (
        report.listed,
        report.referenced,
        report.trash,
        report.pending,
        report.recent,
        report.other,
        report.orphans,
    )
    assert counts == (7, 1, 1, 1, 1, 1, 2)
    assert report.prefix == PREFIX + "advisor/out/"
    assert (report.deleted, report.failed, report.unparsed) == (0, 0, 0)
    assert cloud.deletes() == 0 and cloud.objects == scene.objects
    # 存储桶名只靠申请一个专门路径的上传链接得到，不往返回的上传地址传东西
    assert cloud.probe_paths == [PROBE_PATH]
    assert not any("cos.ap-shanghai.myqcloud.com/" + BUCKET in item for item in cloud.requests)
    assert session.scalars(select(AdvisorFileTrash.file_id)).all() == [PREFIX + key(901)]


def test_delete_removes_only_orphans(session, scene):
    cloud = Cloud(scene.objects)
    report = scan(session, cloud, delete=True)
    assert sorted(cloud.deleted) == sorted([PREFIX + key(903), PREFIX + key(904, uid=OTHER_UID)])
    assert (report.orphans, report.deleted, report.failed, report.rechecked) == (2, 2, 0, 0)
    kept = {key(900), key(901), key(902), key(scene.queued.id), "advisor/out/readme.txt"}
    assert set(cloud.objects) == kept
    # 任务表和待删表都没动
    assert session.scalars(select(AdvisorFileTrash.file_id)).all() == [PREFIX + key(901)]
    assert len(session.scalars(select(AdvisorTask.id)).all()) == 2
    # 删完再巡检一次，没有孤儿了
    again = scan(session, cloud)
    assert (again.listed, again.orphans) == (5, 0)


def test_failed_delete_is_recorded_in_trash_and_left_to_cleanup(session, scene):
    cloud = Cloud(scene.objects, fail_delete={PREFIX + key(903)})
    report = scan(session, cloud, delete=True)
    assert (report.deleted, report.failed) == (1, 1)
    assert cloud.deleted == [PREFIX + key(904, uid=OTHER_UID)]
    trash = set(session.scalars(select(AdvisorFileTrash.file_id)))
    assert trash == {PREFIX + key(901), PREFIX + key(903)}
    # 下次巡检算在"待删表里"，不再当孤儿删
    again = scan(session, cloud, delete=True)
    assert (again.trash, again.orphans, again.deleted) == (2, 0, 0)


def test_delete_goes_in_batches_of_50(session):
    cloud = Cloud({key(n): OLD for n in range(1000, 1120)})
    report = scan(session, cloud, delete=True)
    assert (report.orphans, report.deleted) == (120, 120)
    assert cloud.delete_batches == [50, 50, 20] and cloud.objects == {}


def test_reference_written_while_listing_is_not_deleted(session_factory, session, scene):
    """读完任务表之后、删除之前别的进程写进了记录（如任务刚完成）：删除前复查时发现，不删。"""

    def task_completes():
        with session_factory() as other:
            add_task(other, scene.owner.id, "late", result_file_id=PREFIX + key(903))

    cloud = Cloud(scene.objects, on_list=task_completes)
    report = scan(session, cloud, delete=True)
    assert (report.orphans, report.rechecked, report.deleted) == (2, 1, 1)
    assert cloud.deleted == [PREFIX + key(904, uid=OTHER_UID)]
    assert key(903) in cloud.objects


def test_task_deleted_while_listing_keeps_its_file_this_round(session_factory, session, scene):
    """读任务表时还在排队的任务，列文件期间被删掉（用户删除、过期清理）：这一轮按读到的不动，
    下一轮再删。"""

    def task_deleted():
        with session_factory() as other:
            other.execute(delete(AdvisorTask).where(AdvisorTask.id == scene.queued.id))
            other.commit()

    cloud = Cloud(scene.objects, on_list=task_deleted)
    report = scan(session, cloud, delete=True)
    assert (report.pending, report.orphans, report.deleted) == (1, 2, 2)
    assert key(scene.queued.id) in cloud.objects
    later = scan(session, Cloud(cloud.objects), delete=True)
    assert (later.pending, later.orphans, later.deleted) == (0, 1, 1)


def test_listing_follows_next_marker_across_pages(session):
    objects = {key(n): OLD for n in range(1, 6)}
    cloud = Cloud(objects, page_size=2)
    report = scan(session, cloud)
    assert (report.listed, report.orphans) == (5, 5)
    assert cloud.markers == ["", key(2), key(4)]
    # 临时密钥离失效还早：整个翻页只取一次
    assert cloud.auth_calls == 1


def test_expiring_credentials_are_renewed_before_next_page(session):
    cloud = Cloud(
        {key(n): OLD for n in range(1, 6)},
        page_size=2,
        auth={**AUTH, "ExpiredTime": str(int(time.time()) + 100)},
    )
    assert scan(session, cloud).listed == 5
    assert cloud.auth_calls == 3


def test_region_comes_from_upload_link(session):
    cloud = Cloud({key(1): OLD}, region="ap-guangzhou")
    assert scan(session, cloud).orphans == 1
    assert f"GET https://{BUCKET}.cos.ap-guangzhou.myqcloud.com/" in cloud.requests
    # 上传地址认不出地域时，用云托管默认的上海
    fallback = Cloud({key(1): OLD}, upload_url="https://upload.example.test/x")
    assert scan(session, fallback).orphans == 1
    assert f"GET https://{BUCKET}.cos.ap-shanghai.myqcloud.com/" in fallback.requests


def test_busy_cos_is_retried(session):
    cloud = Cloud({key(1): OLD}, list_statuses=[503])
    assert scan(session, cloud).orphans == 1


@pytest.mark.parametrize(
    ("cloud_options", "message"),
    [
        ({"auth": {"errcode": 40001, "errmsg": "invalid"}}, "errcode 40001"),
        ({"auth": {"TmpSecretId": "AKIDtest", "TmpSecretKey": ""}}, "没有临时密钥"),
        ({"probe_env": "other-env"}, "文件 ID 的格式和预期不同"),
        ({"list_statuses": [403]}, "HTTP 403，AccessDenied"),
        ({"bucket_name": "7465-other-1"}, "存储桶和文件 ID 里的不一致"),
    ],
)
def test_storage_problems_stop_before_any_delete(session, scene, cloud_options, message):
    cloud = Cloud(scene.objects, **cloud_options)
    with pytest.raises(BackupError) as caught:
        scan(session, cloud, delete=True)
    assert message in str(caught.value)
    # 报错信息里不带临时密钥
    assert all(str(value) not in str(caught.value) for value in AUTH.values())
    assert cloud.deletes() == 0 and cloud.objects == scene.objects


def test_unrecognized_file_id_in_records_blocks_delete(session, scene):
    add_task(session, scene.owner.id, "odd", result_file_id="not-a-file-id")
    cloud = Cloud(scene.objects)
    report = scan(session, cloud, delete=True)
    assert (report.unparsed, report.orphans, report.deleted, report.blocked) == (1, 2, 0, True)
    assert cloud.deletes() == 0
    assert "疑似没有记录指向的：2 个" in report.text() and "认不出格式" in report.text()
    assert "疑似没有记录指向" in report.alert_text() and "查清前没有删除" in report.alert_text()


def test_signature_matches_cos_doc_examples():
    """腾讯云 COS《请求签名》的两个示例（2026-10-18 取回）：HttpString 的 SHA-1 完全一致；
    文档把签名的最后 4 位换成了 1234，签名只比前 36 位。"""
    download = storage_list.http_string(
        "GET",
        "/exampleobject(腾讯云)",
        {
            "response-content-type": "application/octet-stream",
            "response-cache-control": "max-age=600",
        },
        {
            "Date": "Thu, 16 May 2019 06:55:53 GMT",
            "Host": "examplebucket-1250000000.cos.ap-beijing.myqcloud.com",
        },
    )
    assert hashlib.sha1(download.encode()).hexdigest() == "54ecfe22f59d3514fdc764b87a32d8133ea611e6"
    sign = storage_list.signature(
        "937914bf490e9e8c189836aad2052e4feeb35eaf", "1557989753;1557996953", download
    )
    assert sign[:36] == "01681b8c9d798a678e43b685a9f1bba0f6c01234"[:36]

    upload = storage_list.http_string(
        "PUT",
        "/exampleobject(腾讯云)",
        {},
        {
            "Date": "Thu, 16 May 2019 06:45:51 GMT",
            "Host": "examplebucket-1250000000.cos.ap-beijing.myqcloud.com",
            "Content-Type": "text/plain",
            "Content-Length": "13",
            "Content-MD5": "mQ/fVh815F3k6TAUm8m0eg==",
            "x-cos-acl": "private",
            "x-cos-grant-read": 'uin="100000000011"',
        },
    )
    assert hashlib.sha1(upload.encode()).hexdigest() == "8b2751e77f43a0995d6e9eb9477f4b685cca4172"
    sign = storage_list.signature(
        "eb2519b498b02ac213cb1f3d1a3d27a3b3c9bc5f", "1557989151;1557996351", upload
    )
    assert sign[:36] == "3b8851a11a569213c17ba8fa7dcf2abec6931234"[:36]

    creds = storage_list.Credentials("AKIDexample", "secret", "token")
    auth = storage_list.authorization(
        creds,
        "get",
        "/",
        {"prefix": "advisor/out/", "max-keys": "1000"},
        {"host": f"{BUCKET}.cos.ap-shanghai.myqcloud.com"},
        "1557989753;1557996953",
    )
    assert auth.startswith(
        "q-sign-algorithm=sha1&q-ak=AKIDexample&q-sign-time=1557989753;1557996953"
    )
    assert "&q-header-list=host&q-url-param-list=max-keys;prefix&q-signature=" in auth
    assert "secret" not in repr(creds) and "token" not in repr(creds)


@pytest.fixture
def run_cli(monkeypatch, session_factory):
    """命令行入口：连测试库、用测试替身，返回退出码。"""
    real_scan = orphans.scan
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)

    def run(cloud, *argv, settings=SETTINGS):
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orphans,
            "scan",
            lambda session, settings, **kw: real_scan(
                session, settings, client=cloud.client, now=NOW, sleep=no_sleep, **kw
            ),
        )
        return cli.main(["advisor-orphans", *argv])

    return run


def test_cli_prints_only_counts_and_prefix(run_cli, scene, capsys):
    cloud = Cloud(scene.objects)
    assert run_cli(cloud) == 0
    out = capsys.readouterr().out
    assert f"结果文件巡检（{PREFIX}advisor/out/）：对象存储里共 7 个文件" in out
    assert "没有任何记录指向的：2 个（只列出，没有删除；确认后加 --delete 删除）" in out
    # 不输出用户编号、任务编号和文件名
    assert UID not in out and OTHER_UID not in out and ".xlsx" not in out and "903" not in out
    assert cloud.deletes() == 0

    assert run_cli(cloud, "--delete") == 0
    out = capsys.readouterr().out
    assert "没有任何记录指向的：2 个，删掉 2 个。" in out
    assert UID not in out and OTHER_UID not in out and ".xlsx" not in out
    assert len(cloud.deleted) == 2


def test_cli_alert_and_failures(run_cli, scene, monkeypatch, capsys):
    from youju import alerts

    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda text: sent.append(text) or True)
    cloud = Cloud(scene.objects)
    assert run_cli(cloud, "--alert") == 0
    assert len(sent) == 1 and "2 个批量工具结果文件没有任何记录指向" in sent[0]
    assert "--delete" in sent[0]
    assert UID not in sent[0] and ".xlsx" not in sent[0]
    assert cloud.deletes() == 0

    # 自动删除时也能提醒：写明删掉几个；删不掉的记进待删表，退出码 1
    sent.clear()
    failing = Cloud(scene.objects, fail_delete={PREFIX + key(903)})
    assert run_cli(failing, "--delete", "--alert") == 1
    assert "没删掉 1 个（已记进待删表" in capsys.readouterr().out
    assert len(sent) == 1 and "删掉 1 个，没删掉 1 个（已记进待删表" in sent[0]
    assert UID not in sent[0] and OTHER_UID not in sent[0] and ".xlsx" not in sent[0]

    # 没有孤儿时不发提醒；提醒发不出去时退出码 1
    sent.clear()
    assert run_cli(Cloud({key(1999): NOW}), "--alert") == 0 and sent == []
    monkeypatch.setattr(alerts, "send_alert", lambda text: None)
    assert run_cli(Cloud({key(2000): OLD}), "--alert") == 1
    assert "提醒没有发出去" in capsys.readouterr().out

    # 对象存储出错时退出码 1、写明什么都没删；没配环境不跑
    assert run_cli(Cloud(scene.objects, list_statuses=[403]), "--delete") == 1
    assert "结果文件巡检失败，没有删除任何文件" in capsys.readouterr().out
    assert run_cli(Cloud({}), settings=SimpleNamespace(cloud_env=None)) == 1
