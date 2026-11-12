"""D-130 核对证据与统计的离线测试，不连接数据库。"""

import argparse
import json
from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import pytest

from youju import cli
from youju import deadline_verify as verify
from youju.api import app

BODY = "请各单位在2026年9月30日  下午5点前提交申报材料。"
QUOTE = "请各单位在2026年9月30日 下午5点前提交申报材料。"


class Session:
    def __init__(self, rows, details=None):
        self.rows, self.details = rows, details or {}

    def get(self, model, key):
        return self.rows[0] if self.rows and self.rows[0].id == key else None

    def scalar(self, stmt):
        return self.details[self.rows[0].document_id]

    def scalars(self, stmt):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def rollback(self):
        pass


def row(**kw):
    return SimpleNamespace(
        **(
            dict(
                id=1,
                document_id=1,
                status="pending",
                audit_state=None,
                kind="企业申报截止",
                candidate_date=date(2026, 9, 30),
                candidate_time=time(17),
                reviewed_by=None,
                reviewed_at=None,
                ai_agrees=True,
                verification_corrected=None,
                verification_unclear_at=None,
            )
            | kw
        )
    )


@pytest.fixture
def setup(monkeypatch):
    candidate = row()
    session = Session([candidate], {1: SimpleNamespace(status="ok", body_text=BODY)})
    monkeypatch.setattr(app, "_lock_candidates", lambda session, row: session.rows)
    monkeypatch.setattr(
        app,
        "_apply_review",
        lambda row, locked, status, by, stamp, day, clock: [
            setattr(d, "reviewed_by", by) or setattr(d, "reviewed_at", stamp) for d in locked
        ],
    )
    return session, candidate


def item(**kw):
    return dict(id=1, verdict="confirmed", quote=QUOTE, body_sha256=verify.body_hash(BODY)) | kw


def test_quote_length_and_original(setup):
    session, candidate = setup
    for quote in ("9月30日", "甲" * 301):
        assert "12 至 300" in verify.apply_one(session, item(quote=quote), True)
    assert verify.apply_one(session, item(), True) == "通过"
    assert candidate.verification_quote == BODY
    assert not candidate.verification_corrected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("17:00", True),
        ("17时", True),
        ("17点", True),
        ("下午5点", True),
        ("下午5时30分", False),
        ("17:30", False),
        ("5点", False),
        ("117:00", False),
        ("下午5点半", False),
        ("17:00:30", False),
    ],
)
def test_time_forms(text, expected):
    assert verify._has_time(text, time(17)) is expected


def test_time_required_for_confirmed_and_corrected(setup):
    session, candidate = setup
    text = "请各单位在2026年9月30日前提交申报材料。"
    session.details[1].body_text = text
    args = dict(quote=text, body_sha256=verify.body_hash(text))
    assert verify.apply_one(session, item(**args), True) == "原句里没有时间"
    assert (
        verify.apply_one(
            session, item(verdict="corrected", date="2026-09-30", time="16:00", **args), True
        )
        == "原句里没有时间"
    )
    assert candidate.reviewed_by is None
    assert (
        verify.apply_one(
            session, item(verdict="corrected", date="2026-09-30", time=None, **args), True
        )
        == "通过并修改日期"
    )


def test_context_fields_and_unknown(setup):
    session, _ = setup
    context = dict(
        document_id=1,
        title="标题",
        url="网址",
        kind="类型",
        evidence="依据",
        origin="rule",
        ai_agrees=True,
        sentences=[],
        date="无效",
        time="无效",
    )
    assert verify.apply_one(session, item(**context)) == "通过"
    assert "不支持" in verify.apply_one(session, item(unexpected=1))


def test_bom_file(monkeypatch, tmp_path, capsys, setup):
    session, _ = setup
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: session)
    path = tmp_path / "results.json"
    path.write_text(json.dumps([item()], ensure_ascii=False), encoding="utf-8-sig")
    assert cli._cmd_deadline_verify_apply(argparse.Namespace(file=path, apply=False)) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "通过"


def test_stats_groups_and_since(monkeypatch):
    from youju import dedup

    stamp = datetime(2026, 11, 11, 16, tzinfo=UTC)  # 北京时间 11 月 12 日零点
    rows = [
        row(id=1, status="approved", reviewed_by="Claude", reviewed_at=stamp),
        row(
            id=2,
            document_id=2,
            status="approved",
            reviewed_by="Claude",
            reviewed_at=stamp,
            ai_agrees=False,
        ),
        row(
            id=3,
            document_id=3,
            status="approved",
            reviewed_by="Claude",
            reviewed_at=stamp,
            verification_corrected=True,
        ),
        row(id=4, document_id=4, verification_unclear_at=stamp, ai_agrees=None),
        row(
            id=5,
            document_id=5,
            status="approved",
            reviewed_by="Claude",
            reviewed_at=datetime(2026, 11, 11, 15, 59, tzinfo=UTC),
        ),
    ]
    monkeypatch.setattr(dedup, "canonical_ids", lambda session, ids: {2: 1})
    result = verify.stats(Session(rows), date(2026, 11, 12))
    assert result["total"] == dict(confirmed=1, corrected=1, unclear=1, accuracy=0.5)
    assert result["ai_agrees"] == dict(confirmed=0, corrected=1, unclear=0, accuracy=0.0)
    assert result["other"] == dict(confirmed=1, corrected=0, unclear=1, accuracy=1.0)
    assert verify.stats(Session([]))["total"]["accuracy"] is None


def test_stats_cli(monkeypatch, capsys):
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: Session([]))
    for as_json in (False, True):
        assert cli._cmd_deadline_verify_stats(argparse.Namespace(since=None, json=as_json)) == 0
        output = capsys.readouterr().out
        assert (
            json.loads(output)["total"]["accuracy"] is None if as_json else "准确率 暂无" in output
        )


@pytest.mark.parametrize(
    ("quote", "clock", "ok"),
    [
        ("请于2026年9月30日17:00前提交", time(17), True),
        ("请于2026年9月30日17：00前提交", time(17), True),
        ("请于2026年9月30日 17 时前提交", time(17), True),
        ("请于2026年9月30日17时30分前提交", time(17, 30), True),
        ("请于2026年9月30日17:00:00前提交", time(17), True),
        ("请于2026年9月30日17:00分前提交", time(17), True),
        ("请于2026年9月30日下午 5 时前提交", time(17), True),
        ("请于2026年9月30日下午5时前提交", time(17), True),
        ("请于2026年9月30日前提交", time(17), False),
        ("请于2026年9月30日16:00前提交", time(17), False),
        ("请于2026年9月30日上午5时前提交", time(17), False),
        # 独立 Claude 审阅（11/12）：日期和时刻之间有空白、换行、全角空格的照认；
        # 分钟隔着空格写的不能当整点
        ("请于2026年11月15日 17:00前提交", time(17), True),
        ("请于2026年11月15日\n17时前提交", time(17), True),
        ("请于2026年11月15日\u300017:00前提交", time(17), True),
        ("请于2026-11-15 18:00:00前提交", time(18), True),
        ("请于2026年11月15日17时 30分前提交", time(17), False),
        ("请于2026年11月15日17时 30分前提交", time(17, 30), True),
    ],
)
def test_has_time_follows_the_extraction_formats(quote, clock, ok):
    # agy 审阅（11/12）：和抽取截止时的时间写法一致，空格、"分"、带秒的都认
    assert verify._has_time(quote, clock) is ok
