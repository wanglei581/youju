"""首发前搬家（D-127）的云托管导出脚本 deploy/selfhost/shared/cloudrun-export.py。

脚本在线上镜像里跑，只能用镜像里已有的函数；这些测试也在线上镜像那一版的代码上跑过
（PYTHONPATH 指到那一版的 backend/src，见手册第六节）。"""

import gzip
import hashlib
import hmac
import importlib.util
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from youju import backup

try:
    from youju import takedown_carry
except ImportError:  # 在线上那一版的代码上跑时没有这个模块（见文件开头），跳过读清单的那几句
    takedown_carry = None
from youju.config import Settings
from youju.models import (
    AppKey,
    Document,
    DocumentMerge,
    DocumentTakedownEvent,
    InvoiceRequest,
    PassReward,
    PayEvent,
    PayOrder,
    Source,
    User,
    WorkbenchPass,
)
from youju.takedown import change_state

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/selfhost/shared/cloudrun-export.py"
_spec = importlib.util.spec_from_file_location("cloudrun_export", SCRIPT)
export_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_script)

OPEN = "http://api.weixin.qq.com"
ENV = "prod-1abc"
BUCKET = "7072-prod-1abc-1250000000"
PREFIX = f"cloud://{ENV}.{BUCKET}/"
NOW = datetime(2026, 11, 12, 8, tzinfo=UTC)


def _source(session):
    src = Source(
        level="省级",
        province="辽宁",
        dept="科技",
        dept_full_name="辽宁省科技厅",
        column_name="通知",
        url="https://example.gov.cn/tz",
    )
    session.add(src)
    session.flush()
    return src


def _doc(session, src, n):
    doc = Document(
        source_id=src.id,
        url=f"https://example.gov.cn/{n}.html",
        url_key=f"example.gov.cn/{n}.html",
        title=f"辽宁省科技项目申报通知{n}",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(doc)
    session.flush()
    return doc


def test_takedown_rows_list_every_member_of_hidden_groups(session):
    src = _source(session)
    master, twin, visible, old, unmerged = (_doc(session, src, n) for n in range(1, 6))
    session.add_all(
        [
            # 下架之后才并进来的副本也不可见（整组按主文件算）
            DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="a"),
            # 停用的归并不算一组
            DocumentMerge(
                duplicate_id=unmerged.id, canonical_id=old.id, active=False, match_key="b"
            ),
        ]
    )
    session.commit()
    change_state(
        session, master.id, action="takedown", note="投诉人某某", operator="x", reason="侵权投诉"
    )
    session.commit()
    old.taken_down_at = NOW - timedelta(days=3)  # 早期的下架没有原因类别
    session.commit()
    twin.taken_down_at = None  # 模拟"下架后才并进来"：副本自己没有下架时间
    session.commit()

    rows = export_script.takedown_rows(session)
    by_key = {row["url_key"]: row for row in rows}
    assert set(by_key) == {master.url_key, twin.url_key, old.url_key}
    assert by_key[twin.url_key]["taken_down_at"] == by_key[master.url_key]["taken_down_at"]
    assert by_key[twin.url_key]["reason"] == "侵权投诉"
    assert by_key[twin.url_key]["origin_source_id"] == src.id
    assert by_key[old.url_key]["reason"] == "其他"
    assert by_key[old.url_key]["taken_down_at"] == (NOW - timedelta(days=3)).isoformat()
    # 只有这四项（备注、操作人、标题不带），本机的 takedown-carry 能直接读
    assert all(
        set(row) == {"url_key", "origin_source_id", "taken_down_at", "reason"} for row in rows
    )
    lines = [export_script._ascii(row) for row in rows]
    assert all(line.isascii() for line in lines)
    if takedown_carry:
        assert {item.url_key for item in takedown_carry.parse(lines)} == set(by_key)
    assert visible.url_key not in by_key and unmerged.url_key not in by_key


def test_summary_has_only_numbers(session):
    users = [
        User(openid="o-secret-1", last_visited_at=NOW - timedelta(days=5)),
        User(openid="o-secret-2", last_visited_at=NOW),
    ]
    session.add_all(users)
    doc = _doc(session, _source(session), 1)
    session.commit()
    for n, (env, status, refund) in enumerate(
        [(0, "paid", None), (0, "paid", "done"), (1, "pending", None)]
    ):
        session.add(
            PayOrder(
                user_id=users[0].id,
                out_trade_no=f"T{n}",
                product_id="workbench_30d",
                price=2900,
                days=30,
                env=env,
                terms_version="v1",
                status=status,
                refund_status=refund,
            )
        )
    session.add(
        DocumentTakedownEvent(
            document_id=doc.id, action="takedown", note="n", operated_by="x", operated_at=NOW
        )
    )
    session.commit()
    numbers = export_script.summary(session, takedown_docs=4)
    assert numbers == {
        "users": 2,
        "last_visit": "2026-11-12",
        "orders": {"env0:paid": 2, "env1:pending": 1},
        "refunds": {"done": 1},
        "invoices": 0,
        "takedown_docs": 4,
        "takedown_events": 1,
    }
    assert "o-secret" not in json.dumps(numbers)


def test_config_values_only_whitelisted_non_defaults():
    settings = Settings(
        _env_file=None,
        ai_model="qwen-plus",
        ai_model_filing="备案号 Beijing-123",
        xpay_price_pass=2900,
        season_boost_start=date(2026, 12, 1),
        pay_openids="o-secret",
        ai_advisor_openids="o-secret",
        wx_appsecret=SecretStr("s" * 32),
        ai_api_key=SecretStr("sk-secret"),
        admin_token="t" * 32,
    )
    values = export_script.config_values(settings)
    assert values == {
        "YOUJU_AI_MODEL": "qwen-plus",
        "YOUJU_AI_MODEL_FILING": "备案号 Beijing-123",
        "YOUJU_XPAY_PRICE_PASS": 2900,
        "YOUJU_SEASON_BOOST_START": "2026-12-01",
    }
    assert export_script._ascii(values).isascii()
    # 白名单里不能有密钥、openid 名单、数据库地址
    fields = Settings.model_fields
    for name in export_script.CONFIG_KEYS:
        assert name in fields, name
        assert "SecretStr" not in str(fields[name].annotation), name
        for word in ("secret", "token", "openid", "appkey", "password", "webhook", "database"):
            assert word not in name, name


class FakeCloud:
    """开放接口和 COS 列文件：backups/ 有两份备份，attachments/ 分两页。"""

    def __init__(self, objects):
        self.objects = dict(objects)
        self.uploads: dict[str, bytes] = {}
        self.link_batches: list[list[str]] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if request.method == "GET" and str(url) == f"{OPEN}/_/cos/getauth":
            return httpx.Response(
                200, json={"TmpSecretId": "id", "TmpSecretKey": "key", "Token": "token"}
            )
        if request.method == "POST" and str(url) == f"{OPEN}/tcb/uploadfile":
            path = json.loads(request.content)["path"]
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "url": f"https://cos.ap-shanghai.myqcloud.com/{BUCKET}/{path}",
                    "token": "upload-token",
                    "authorization": "upload-signature",
                    "file_id": PREFIX + path,
                    "cos_file_id": "cos-file",
                },
            )
        if request.method == "POST" and url.host == "cos.ap-shanghai.myqcloud.com":
            return httpx.Response(204)  # 表单上传（内容由测试里包着的 backup.upload 记下）
        if request.method == "GET" and url.host == f"{BUCKET}.cos.ap-shanghai.myqcloud.com":
            return self.page(request)
        if request.method == "POST" and str(url) == f"{OPEN}/tcb/batchdownloadfile":
            ids = [item["fileid"] for item in json.loads(request.content)["file_list"]]
            assert {item["max_age"] for item in json.loads(request.content)["file_list"]} == {7200}
            self.link_batches.append(ids)
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "file_list": [
                        {
                            "fileid": fid,
                            "status": 0,
                            "download_url": f"https://{BUCKET}.tcb.qcloud.la/"
                            f"{fid.removeprefix(PREFIX)}?sign=s&t=1",
                        }
                        for fid in ids
                    ],
                },
            )
        raise AssertionError(f"没想到的请求：{request.method} {url}")

    def page(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        prefix = query["prefix"][0]
        marker = query.get("marker", [""])[0]
        keys = sorted(k for k in self.objects if k.startswith(prefix) and k > marker)
        page, rest = keys[:2], keys[2:]
        contents = "".join(
            f"<Contents><Key>{k}</Key><LastModified>2026-11-12T00:00:00.000Z</LastModified>"
            f"<Size>{self.objects[k]}</Size></Contents>"
            for k in page
        )
        body = (
            f"<ListBucketResult><Name>{BUCKET}</Name><IsTruncated>{'true' if rest else 'false'}"
            f"</IsTruncated><NextMarker>{page[-1] if rest else ''}</NextMarker>{contents}"
            "</ListBucketResult>"
        )
        return httpx.Response(200, content=body.encode())


def test_export_writes_manifest_with_latest_backup_and_takedowns(session, tmp_path, monkeypatch):
    src = _source(session)
    doc = _doc(session, src, 1)
    session.commit()
    change_state(session, doc.id, action="takedown", note="备注", operator="x", reason="内容有误")
    session.add(User(openid="o-1"))
    session.commit()
    sha = "ab" * 32
    cloud = FakeCloud(
        {
            "backups/youju-20261111.jsonl.gz": 100,
            "backups/youju-20261112.jsonl.gz": 120,
            "backups/prefix-probe-not-a-backup": 1,
            "logs/access-20261110.jsonl.gz": 30,
            f"attachments/{sha}.pdf": 40,
            f"attachments/{'cd' * 32}.docx": 50,
            f"attachments/{'ef' * 32}.xls": 60,
            "advisor/out/1/result.xlsx": 70,
        }
    )
    uploaded = {}
    real_upload = backup.upload

    def upload(client, env, path, local, *, what):
        uploaded[path] = Path(local).read_bytes()
        return real_upload(client, env, path, local, what=what)

    monkeypatch.setattr(backup, "upload", upload)
    settings = Settings(_env_file=None, cloud_env=ENV, ai_model="qwen-plus")
    # conftest 把默认连接指到了测试库
    database_url = os.environ["YOUJU_DATABASE_URL"]
    result = export_script.export(
        ENV, database_url, client=cloud.client, settings=settings, workdir=tmp_path
    )
    manifest = result["manifest"]
    assert [(f["path"], f["size"]) for f in manifest["files"]] == [
        ("backups/youju-20261112.jsonl.gz", 120),
        ("logs/access-20261110.jsonl.gz", 30),
        (f"attachments/{'ab' * 32}.pdf", 40),
        (f"attachments/{'cd' * 32}.docx", 50),
        (f"attachments/{'ef' * 32}.xls", 60),
        ("advisor/out/1/result.xlsx", 70),
    ]
    assert all(f["url"].startswith(f"https://{BUCKET}.tcb.qcloud.la/") for f in manifest["files"])
    assert manifest["revision"] == session.scalar(text("SELECT version_num FROM alembic_version"))
    takedowns = manifest["takedowns"]
    (listing_path,) = [p for p in uploaded if p.startswith("exports/takedowns-")]
    assert takedowns["path"] == listing_path and takedowns["count"] == 1
    assert takedowns["sha256"] == hashlib.sha256(uploaded[listing_path]).hexdigest()
    assert uploaded[listing_path].isascii()
    if takedown_carry:
        listing = takedown_carry.parse(uploaded[listing_path].decode().splitlines())
        assert [item.url_key for item in listing] == [doc.url_key]
    assert manifest["summary"]["users"] == 1 and manifest["summary"]["takedown_docs"] == 1
    assert manifest["config"] == {"YOUJU_AI_MODEL": "qwen-plus"}
    (pay_path,) = [p for p in uploaded if p.startswith("exports/pay-records-")]
    assert manifest["pay_records"]["sha256"] == hashlib.sha256(uploaded[pay_path]).hexdigest()
    assert manifest["pay_records"]["retain_days"] == 3650
    pay_lines = gzip.decompress(uploaded[pay_path]).decode().splitlines()
    assert json.loads(pay_lines[0])["format"] == "youju-pay-records" and len(pay_lines) == 1
    (manifest_path,) = [p for p in uploaded if p.startswith("exports/manifest-")]
    assert result["sha256"] == hashlib.sha256(uploaded[manifest_path]).hexdigest()
    assert json.loads(uploaded[manifest_path]) == manifest
    assert result["url"].startswith(f"https://{BUCKET}.tcb.qcloud.la/exports/manifest-")
    # 列文件时临时换掉的解析函数已经换回来
    assert export_script.storage_list._parse.__name__ == "_parse"


def test_list_with_sizes_restores_parser_on_error():
    cloud = FakeCloud({"logs/a.gz": 1})
    bucket = export_script.storage_list.bucket_of(cloud.client, ENV)
    cloud.page = lambda request: httpx.Response(
        200,
        content=(
            f"<ListBucketResult><Name>{BUCKET}</Name><IsTruncated>false</IsTruncated>"
            "<Contents><Key>logs/a.gz</Key><LastModified>2026-11-12T00:00:00.000Z</LastModified>"
            "</Contents></ListBucketResult>"
        ).encode(),
    )
    with pytest.raises(export_script.ExportError):
        export_script.list_with_sizes(cloud.client, bucket, "logs/")
    assert export_script.storage_list._parse.__name__ == "_parse"


def test_pick_files_needs_a_backup():
    with pytest.raises(export_script.ExportError):
        export_script.pick_files({"backups/": [("backups/prefix-probe", 1)]})


def test_main_stops_before_listing_when_a_command_fails(monkeypatch, capsys):
    calls = []

    def runner(args):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[1] == "backup" else 0)

    monkeypatch.setenv("YOUJU_CLOUD_ENV", ENV)
    monkeypatch.setattr(export_script, "export", lambda *a, **k: pytest.fail("不该接着导出"))
    export_script.get_settings.cache_clear()
    try:
        assert export_script.main([], runner=runner) == 1
    finally:
        export_script.get_settings.cache_clear()
    assert calls == [["youju", "archive-logs"], ["youju", "backup"]]
    assert "EXPORT FAILED" in capsys.readouterr().out


def test_report_prints_copyable_ascii_lines(capsys):
    manifest = {
        "revision": "0046",
        "files": [
            {"path": "backups/youju-20261112.jsonl.gz", "size": 2048},
            {"path": "logs/access-20261110.jsonl.gz", "size": 10},
        ],
        "takedowns": {"count": 2},
        "pay_records": {"counts": {"pay_orders": 2, "invoice_requests": 1}},
        "summary": {"users": 3, "orders": {}},
        "config": {"YOUJU_AI_MODEL_FILING": "备案"},
    }
    export_script.report(
        {
            "manifest": manifest,
            "url": "https://x.tcb.qcloud.la/exports/m.json?sign=s",
            "sha256": "0" * 64,
        }
    )
    lines = capsys.readouterr().out.splitlines()
    copy = [
        line for line in lines if line.startswith(("EXPORT OK", "SUMMARY", "sudo ", "https://"))
    ]
    assert len(copy) == 4 and all(line.isascii() for line in copy)
    assert copy[0] == (
        "EXPORT OK files=backups:1/2048,logs:1/10,attachments:0/0,advisor/out:0/0 total=2058 "
        "revision=0046"
    )
    # 命令里只有哈希；链接单独一行，运行后在提示里粘贴（不进命令行参数和 shell 历史）
    assert copy[2] == f"sudo /opt/youju/bin/youju-shared.sh fetch-export {'0' * 64}"
    assert copy[3] == "https://x.tcb.qcloud.la/exports/m.json?sign=s"
    assert any("pay_orders 2 条" in line for line in lines)


def test_pay_records_are_deidentified_like_account_deletion(session):
    """交易记录：和注销账号时（payments.anonymize、rewards.anonymize）一样断开用户、去掉 openid，
    另外去掉开票联系邮箱和个人抬头（就是姓名）；单位抬头、税号、订单号留着对账。"""
    key = "ab" * 32
    session.add(AppKey(name="access_log", value=key))
    user = User(openid="o-secret-1")
    session.add(user)
    session.flush()
    order, later = (
        PayOrder(
            user_id=user.id,
            out_trade_no=f"T{n}",
            product_id="workbench_30d",
            price=2900,
            days=30,
            env=0,
            terms_version="v1",
            status="paid",
            wx_order_id=f"wx-{n}",
            created_at=NOW + timedelta(days=n),
        )
        for n in (0, 1)
    )
    session.add_all([order, later])
    session.flush()
    session.add_all(
        [
            PayEvent(
                order_id=order.id, kind="notify", summary={"OpenId": "o-secret-1", "Fee": 2900}
            ),
            InvoiceRequest(
                order_id=order.id,
                user_id=user.id,
                title_type="personal",
                title="张三",
                email="someone@example.com",
            ),
            InvoiceRequest(
                order_id=later.id,
                user_id=user.id,
                title_type="company",
                title="某某科技有限公司",
                tax_id="91110000000000000X",
                email="finance@example.com",
            ),
            PassReward(
                user_id=user.id,
                kind="referral",
                requested_days=3,
                granted_days=3,
                ref_type="user_referral",
                ref_id=user.id,
                granted_at=NOW,
            ),
            WorkbenchPass(
                user_id=user.id,
                order_id=order.id,
                starts_at=NOW,
                ends_at=NOW + timedelta(days=30),
                last_task_id=7,
            ),
        ]
    )
    session.commit()
    lines, header = export_script.pay_records(session, 3650)
    assert header["counts"] == {
        "pay_orders": 2,
        "pay_events": 1,
        "invoice_requests": 2,
        "pass_rewards": 1,
        "workbench_passes": 1,
        "pay_reconciliations": 0,
    }
    assert header["earliest_order_at"] == NOW.isoformat() and header["retain_days"] == 3650
    text = "\n".join(lines)
    assert text.isascii()
    for secret in ("o-secret-1", "someone@example.com", "finance@example.com", "user_id"):
        assert secret not in text, secret
    assert "\\u5f20\\u4e09" not in text  # 个人抬头（姓名）去掉
    rows = [json.loads(line) for line in lines[1:]]
    by_table = {}
    for item in rows:
        by_table.setdefault(item["table"], []).append(item["row"])
    expected = hmac.new(bytes.fromhex(key), b"o-secret-1", hashlib.sha256).hexdigest()[:16]
    assert {o["pseudonym"] for o in by_table["pay_orders"]} == {expected}
    assert [o["out_trade_no"] for o in by_table["pay_orders"]] == ["T0", "T1"]
    assert by_table["pay_events"][0]["summary"] == {"Fee": 2900}
    invoices = sorted(by_table["invoice_requests"], key=lambda r: r["title_type"])
    assert (
        invoices[0]["title"] == "某某科技有限公司" and invoices[0]["tax_id"] == "91110000000000000X"
    )
    assert invoices[1]["title"] is None and "email" not in invoices[1]
    assert by_table["pass_rewards"][0]["ref_id"] is None
    assert "last_task_id" not in by_table["workbench_passes"][0]
