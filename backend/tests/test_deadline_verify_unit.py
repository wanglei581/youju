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
                origin="rule",
                verification_unclear_hash=None,
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
    # 同一截止同组退回的：离线时就看假会话里这几条
    monkeypatch.setattr(
        verify,
        "_reopened_same_deadline",
        lambda session, row: [
            d for d in session.rows if d.status == "pending" and d.audit_state == "reopened"
        ],
    )
    monkeypatch.setattr(
        app,
        "_apply_review",
        lambda row, locked, status, by, stamp, day, clock: [
            setattr(d, "reviewed_by", by) or setattr(d, "reviewed_at", stamp) for d in locked
        ],
    )
    monkeypatch.setattr(verify, "_ai_items", lambda *args: iter([]))
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


def test_reopened_evidence_and_trace(setup, monkeypatch):
    session, candidate = setup
    candidate.audit_state = "reopened"
    candidate.audit_note = "日期存疑"

    def apply_review(row, locked, status, by, stamp, day, clock):
        row.audit_state = row.audit_note = row.verification_unclear_note = None
        row.reviewed_by, row.reviewed_at = by, stamp

    monkeypatch.setattr(app, "_apply_review", apply_review)
    # 结果不是按带退回原因的清单写的（没有 reopened: true）：不放行
    assert "带退回原因的清单" in verify.apply_one(session, item(), True)
    assert "不一样" in verify.apply_one(session, item(reopened=True, audit_note="日期存疑"), True)
    context = dict(
        reopened=True, audit_note="质检退回原因：日期存疑", audit_notes=["质检退回原因：日期存疑"]
    )
    assert "note" in verify.apply_one(session, item(**context), True)
    context["note"] = "核了原文：申报截止就是这一天"
    assert "不在当前正文" in verify.apply_one(
        session, item(quote="不存在的2026年9月30日截止原句", **context), True
    )
    assert candidate.audit_note == "日期存疑"
    assert verify.apply_one(session, item(**context), True) == "通过"
    assert candidate.audit_state is None and candidate.audit_note is None
    assert (
        candidate.verification_unclear_note
        == "质检退回原因：日期存疑；Claude 处理：核了原文：申报截止就是这一天"
    )


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

    class StatsSession(Session):
        def scalars(self, stmt):
            if "document_deadlines.audit_by LIKE" in str(stmt):
                return []
            return self.rows

    result = verify.stats(StatsSession(rows), date(2026, 11, 12))
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
        # 独立 Claude 审阅第二轮：时刻紧跟在"时间："这类冒号后面照认；"时"后面隔着空格的日期
        # 不当分钟；冒号后面没写分钟的不算
        ("申报日期：11月15日。截止时间：17:00", time(17), True),
        ("截止时间：下午5点", time(17), True),
        ("请于17时 12月1日前提交", time(17), True),
        ("序号20: 提交申报", time(20), False),
    ],
)
def test_has_time_follows_the_extraction_formats(quote, clock, ok):
    # agy 审阅（11/12）：和抽取截止时的时间写法一致，空格、"分"、带秒的都认
    assert verify._has_time(quote, clock) is ok


def test_reopen_reason_with_semicolons_still_matches(setup, monkeypatch):
    # 退回原因本身带"；"（AI 写的备注常这样）：按整段比，照样认得出清单给过
    session, candidate = setup
    candidate.audit_state = "reopened"
    candidate.audit_note = "日期对；但类型应是材料报送截止"
    monkeypatch.setattr(
        app,
        "_apply_review",
        lambda row, locked, status, by, stamp, day, clock: [
            setattr(d, "reviewed_by", by) or setattr(d, "reviewed_at", stamp) for d in locked
        ],
    )
    shown = "质检退回原因：日期对；但类型应是材料报送截止"
    result = item(reopened=True, audit_note=shown, audit_notes=[shown], note="核了原文")
    assert verify.apply_one(session, result, True) == "通过"
    # 短的原因不能被长的冒认
    candidate.status, candidate.reviewed_by = "pending", None
    candidate.audit_note = "日期对"
    assert "等下一份清单" in verify.apply_one(session, result, True)


def test_auto_ai_evidence_current_version():
    from youju.ai.deadlines import RESULT_VERSION

    candidate = row()
    record = SimpleNamespace(
        document_id=1,
        error=None,
        result={
            "version": RESULT_VERSION,
            "items": [dict(kind="企业申报截止", date="2026-09-30", time="17:00", evidence=QUOTE)],
        },
    )
    detail = SimpleNamespace(status="ok", body_text=BODY)

    class EvidenceSession:
        def scalars(self, stmt):
            return [record]

        def scalar(self, stmt):
            return detail

    session = EvidenceSession()
    assert verify._ai_supports(session, candidate, [1])
    record.result["items"][0]["kind"] = "材料报送截止"
    assert not verify._ai_supports(session, candidate, [1])
    record.result["items"][0]["kind"] = "企业申报截止"
    record.result["version"] = RESULT_VERSION - 1
    assert not verify._ai_supports(session, candidate, [1])
    record.result["version"] = RESULT_VERSION
    record.error = "失败"
    assert not verify._ai_supports(session, candidate, [1])
    record.error = None
    record.result["items"][0]["evidence"] = "请各单位在2026年9月30日前提交申报材料。"
    detail.body_text = record.result["items"][0]["evidence"]
    assert not verify._ai_supports(session, candidate, [1])
    detail.body_text = "正文已改变"
    assert not verify._ai_supports(session, candidate, [1])


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"origin": "ai"}, "不是规则候选"),
        ({"ai_agrees": False}, "规则和 AI 未一致"),
        ({"ai_agrees": None}, "规则和 AI 未一致"),
        ({"kind": "材料报送截止"}, "不是企业申报截止"),
        ({"audit_state": "reopened"}, "同一截止有质检退回，须人工核对"),
    ],
)
def test_auto_skips_ineligible(monkeypatch, setup, changes, expected):
    from youju import dedup

    session, candidate = setup
    entry = (
        dict(
            id=1,
            document_id=1,
            kind=candidate.kind,
            date="2026-09-30",
            time="17:00",
            origin="rule",
            ai_agrees=True,
            reopened=False,
            evidence=QUOTE,
            body_sha256=verify.body_hash(BODY),
        )
        | changes
    )
    for key, value in changes.items():
        setattr(candidate, key, value)
    monkeypatch.setattr(verify, "queue", lambda *args: [entry])
    monkeypatch.setattr(dedup, "canonical_ids", lambda *args: {1: 1})
    monkeypatch.setattr(dedup, "groups_for", lambda *args: {1: [1]})
    result = list(verify.auto_verify(session, date(2026, 9, 29), datetime(2026, 9, 29)))
    assert result[0]["result"] == expected
    assert candidate.reviewed_by is None


def test_auto_apply_and_expired(monkeypatch, setup):
    from youju import dedup

    session, candidate = setup
    session.commit = lambda: None
    entry = dict(
        id=1,
        document_id=1,
        kind=candidate.kind,
        date="2026-09-30",
        time="17:00",
        origin="rule",
        ai_agrees=True,
        reopened=False,
        evidence=QUOTE,
        body_sha256=verify.body_hash(BODY),
    )
    monkeypatch.setattr(verify, "queue", lambda *args: [entry])
    monkeypatch.setattr(dedup, "canonical_ids", lambda *args: {1: 1})
    monkeypatch.setattr(dedup, "groups_for", lambda *args: {1: [1]})
    monkeypatch.setattr(verify, "_ai_supports", lambda *args: True)
    assert (
        list(verify.auto_verify(session, date(2026, 9, 30), datetime(2026, 9, 30, 18)))[0]["result"]
        == "截止已过"
    )
    assert (
        list(verify.auto_verify(session, date(2026, 9, 29), datetime(2026, 9, 29)))[0]["result"]
        == "通过"
    )
    assert candidate.reviewed_by is None
    assert (
        list(verify.auto_verify(session, date(2026, 9, 29), datetime(2026, 9, 29), True))[0][
            "result"
        ]
        == "通过"
    )
    assert candidate.reviewed_by == verify.AUTO_REVIEWER
    assert candidate.verification_quote == BODY


def test_auto_tries_next_candidate_until_one_passes(monkeypatch):
    """同一截止有几条规则候选：前一条没通过接着试下一条，有一条通过了后面的不再核（D-135）。"""
    from youju import dedup

    rows = {
        i: SimpleNamespace(id=i, candidate_date=date(2026, 9, 30), candidate_time=None)
        for i in (1, 2, 3)
    }

    class Rows:
        def get(self, model, key):
            return rows.get(key)

        def commit(self):
            pass

        def rollback(self):
            pass

    def entry(i, quote):
        return dict(
            id=i,
            document_id=1,
            kind="企业申报截止",
            date="2026-09-30",
            time=None,
            origin="rule",
            ai_agrees=True,
            reopened=False,
            evidence=quote,
            body_sha256="x",
        )

    monkeypatch.setattr(
        verify, "queue", lambda *args: [entry(1, "不在正文"), entry(2, "在正文"), entry(3, "也在")]
    )
    monkeypatch.setattr(dedup, "canonical_ids", lambda *args: {1: 1})
    monkeypatch.setattr(dedup, "groups_for", lambda *args: {1: [1]})
    monkeypatch.setattr(verify, "_auto_reason", lambda *args: None)
    calls = []

    def fake_apply(session, item, write=False, reviewer="Claude", **kw):
        calls.append((item["id"], reviewer))
        return "quote 不在当前正文" if item["quote"] == "不在正文" else "通过"

    monkeypatch.setattr(verify, "apply_one", fake_apply)
    result = [
        r["result"] for r in verify.auto_verify(Rows(), date(2026, 9, 29), datetime(2026, 9, 29))
    ]
    assert result == ["quote 不在当前正文", "通过", "同一截止已由较早的候选通过"]
    assert calls == [(1, verify.AUTO_REVIEWER), (2, verify.AUTO_REVIEWER)]


def test_auto_cli_gates(monkeypatch, capsys):
    from youju import ai

    monkeypatch.setattr(cli, "session_factory", lambda: pytest.fail("不应打开数据库"))
    monkeypatch.setattr(ai, "get_chat_client", lambda: pytest.fail("开关没开时不看密钥"))
    # D-135：只认 0 和 1，没配当 0；别的值（true、2、带空格的 1）按关处理并说明
    for value, words in (
        ("", "没开"),
        ("0", "没开"),
        ("true", "只认 0 或 1"),
        ("2", "只认 0 或 1"),
    ):
        monkeypatch.setattr(
            cli, "get_settings", lambda v=value: SimpleNamespace(deadline_auto_verify=v)
        )
        assert cli._cmd_deadline_verify_auto(argparse.Namespace(apply=True)) == 0
        assert words in capsys.readouterr().out
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(deadline_auto_verify="1"))
    monkeypatch.setattr(ai, "get_chat_client", lambda: None)
    assert cli._cmd_deadline_verify_auto(argparse.Namespace(apply=True)) == 0
    assert "密钥" in capsys.readouterr().out


def test_auto_switch_only_accepts_zero_and_one():
    assert verify.auto_switch(None) is False
    assert verify.auto_switch("") is False
    assert verify.auto_switch("0") is False
    assert verify.auto_switch("1") is True
    for value in ("true", "True", "yes", "on", "2", " 1", "1 ", "01", "false"):
        assert verify.auto_switch(value) is None, value


def test_auto_stats_groups_and_reopened(monkeypatch):
    from youju import dedup

    stamp = datetime(2026, 11, 12, tzinfo=UTC)
    approved = row(
        status="approved",
        reviewed_by=verify.AUTO_REVIEWER,
        reviewed_at=stamp,
        audit_note="Codex 抽查一致：已核对",
    )
    twin = row(
        id=2,
        document_id=2,
        status="approved",
        reviewed_by=verify.AUTO_REVIEWER,
        reviewed_at=stamp,
        audit_note=None,
    )
    reopened = row(
        id=3,
        document_id=3,
        audit_state="reopened",
        audit_at=stamp,
        audit_note="Codex 抽查不一致：原文不符",
        audit_by=f"Codex{verify.AUTO_REOPEN_TAG}",
    )

    class StatsSession(Session):
        def scalars(self, stmt):
            return (
                [approved, twin, reopened]
                if "document_deadlines.audit_by LIKE" in str(stmt)
                else []
            )

    monkeypatch.setattr(dedup, "canonical_ids", lambda session, ids: {2: 1})
    result = verify.stats(StatsSession([]))
    assert result["total"]["confirmed"] == 0
    assert result["auto"] == {"approved": 1, "spotchecked": 1, "reopened": 1}


def test_auto_reuses_quote_validation(monkeypatch, setup):
    from youju import dedup

    session, candidate = setup
    entry = dict(
        id=1,
        document_id=1,
        kind=candidate.kind,
        date="2026-09-30",
        time="17:00",
        origin="rule",
        ai_agrees=True,
        reopened=False,
        evidence="不存在的原句2026年9月30日17:00",
        body_sha256=verify.body_hash(BODY),
    )
    monkeypatch.setattr(verify, "queue", lambda *args: [entry])
    monkeypatch.setattr(dedup, "canonical_ids", lambda *args: {})
    monkeypatch.setattr(dedup, "groups_for", lambda session, ids: {1: [1]})
    monkeypatch.setattr(verify, "_ai_supports", lambda *args: True)

    def run():
        return list(verify.auto_verify(session, date(2026, 9, 29), datetime(2026, 9, 29)))[0][
            "result"
        ]

    assert run() == "quote 不在当前正文"
    text = "请各单位在2026年9月30日前提交申报材料。"
    session.details[1].body_text = text
    entry.update(evidence=text, body_sha256=verify.body_hash(text))
    assert run() == "原句里没有时间"
    assert candidate.reviewed_by is None


def test_auto_cli_dry_run_ignores_settings_and_key(monkeypatch, capsys):
    from youju import ai

    monkeypatch.setattr(cli, "get_settings", lambda: pytest.fail("演练不检查开关"))
    monkeypatch.setattr(ai, "get_chat_client", lambda: pytest.fail("演练不检查密钥"))
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: Session([]))
    monkeypatch.setattr(
        verify,
        "auto_verify",
        lambda session, today, now, write, clock=None, stop=None: iter(
            [
                {
                    "id": 1,
                    "document_id": 2,
                    "kind": "企业申报截止",
                    "date": "2026-09-30",
                    "time": None,
                    "result": "通过",
                }
            ]
        ),
    )
    assert cli._cmd_deadline_verify_auto(argparse.Namespace(apply=False)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0])["result"] == "通过"
    assert lines[1] == "截止日期自动核对（演练，没写库）：候选 1 条，通过 1 条"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("reopened", "同一截止有质检退回，须人工核对"),
        ("unclear", "同一截止有人标过看不准"),
        ("times", "同一天有几个不同的时刻"),
    ],
)
def test_auto_group_reasons(setup, monkeypatch, case, expected):
    session, candidate = setup
    other = row(id=2, document_id=2, candidate_time=time(17))
    session.rows.append(other)
    session.details[2] = SimpleNamespace(status="ok", body_text=BODY)
    session.scalar = lambda stmt: session.details[2]
    if case == "reopened":
        other.audit_state = "reopened"
    elif case == "unclear":
        other.verification_unclear_hash = verify.body_hash(BODY)
    else:
        other.candidate_time = None
    monkeypatch.setattr(verify, "_ai_supports", lambda *args: True)
    assert verify._auto_reason(session, candidate, [1, 2], datetime(2026, 9, 29)) == expected


def test_precheck_after_lock(setup, monkeypatch):
    session, candidate = setup

    def lock(session, row):
        row.ai_agrees = False
        return [row]

    monkeypatch.setattr(app, "_lock_candidates", lock)
    result = verify.apply_one(
        session,
        item(),
        True,
        precheck=lambda r: verify._auto_reason(session, r, [1], datetime(2026, 9, 29)),
    )
    assert result == "规则和 AI 未一致"
    assert candidate.reviewed_by is None


def test_auto_group_skips_a_kind_already_verified(setup, monkeypatch):
    """同组这一类已经有核对通过（或标了变更）的，不再自动通过：对外每一类只显示一个核对过的。"""
    session, candidate = setup
    for status in ("approved", "changed"):
        session.rows[1:] = [
            row(id=2, document_id=2, status=status, candidate_date=date(2026, 10, 9))
        ]
        monkeypatch.setattr(verify, "_ai_supports", lambda *args: True)
        assert verify._auto_reason(session, candidate, [1, 2], datetime(2026, 9, 29)) == (
            "这一类已有核对结果"
        )


def test_auto_rechecks_the_time_after_locking(monkeypatch):
    """每条判断时重新取时间（clock）：加锁前没过期、加锁后跨过了截止时刻的，不写入。"""
    from youju import dedup

    target = SimpleNamespace(
        id=1,
        document_id=1,
        origin="rule",
        ai_agrees=True,
        kind="企业申报截止",
        candidate_date=date(2026, 9, 30),
        candidate_time=time(17),
        audit_state=None,
        verification_unclear_hash=None,
        status="pending",
    )

    class Rows:
        def get(self, model, key):
            return target

        def scalars(self, stmt):
            return [target]

        def commit(self):
            pass

        def rollback(self):
            pass

    entry = dict(
        id=1,
        document_id=1,
        kind="企业申报截止",
        date="2026-09-30",
        time="17:00",
        origin="rule",
        ai_agrees=True,
        reopened=False,
        evidence=QUOTE,
        body_sha256="x",
    )
    monkeypatch.setattr(verify, "queue", lambda *args: [entry])
    monkeypatch.setattr(dedup, "canonical_ids", lambda *args: {1: 1})
    monkeypatch.setattr(dedup, "groups_for", lambda *args: {1: [1]})
    monkeypatch.setattr(verify, "_ai_supports", lambda *args: True)
    monkeypatch.setattr(verify, "_ai_items", lambda *args: iter([]))
    times = iter([datetime(2026, 9, 30, 16, 59), datetime(2026, 9, 30, 17, 1)])

    def fake_apply(session, item, write=False, reviewer="Claude", **kw):
        return kw["precheck"](target) or "通过"

    monkeypatch.setattr(verify, "apply_one", fake_apply)
    result = list(
        verify.auto_verify(
            Rows(), date(2026, 9, 30), datetime(2026, 9, 30, 16), clock=lambda: next(times)
        )
    )
    assert result[0]["result"] == "截止已过"


def test_auto_locks_the_kind_only_when_writing(monkeypatch):
    """写库时每条先锁同组各文件的全部截止行（_lock_notice）再判断；演练不锁。"""
    from youju import dedup

    target = SimpleNamespace(id=1, document_id=1)
    monkeypatch.setattr(
        verify,
        "queue",
        lambda *args: [
            dict(
                id=1,
                document_id=1,
                kind="企业申报截止",
                date="2026-09-30",
                time=None,
                origin="rule",
                ai_agrees=True,
                reopened=False,
                evidence=QUOTE,
                body_sha256="x",
            )
        ],
    )
    monkeypatch.setattr(dedup, "canonical_ids", lambda *args: {1: 1})
    monkeypatch.setattr(dedup, "groups_for", lambda *args: {1: [1, 2]})
    monkeypatch.setattr(verify, "_auto_reason", lambda *args: "不是企业申报截止")
    locks = []
    monkeypatch.setattr(verify, "_lock_notice", lambda session, members: locks.append(members))
    session = SimpleNamespace(
        get=lambda model, key: target, commit=lambda: None, rollback=lambda: None
    )
    list(verify.auto_verify(session, date(2026, 9, 29), datetime(2026, 9, 29)))
    assert locks == []
    list(verify.auto_verify(session, date(2026, 9, 29), datetime(2026, 9, 29), True))
    assert locks == [[1, 2]]


def test_auto_lock_conflict_rolls_back_one_and_goes_on(monkeypatch):
    """和管理台、抽查撞锁（MySQL 1205 等锁超时、1213 死锁）：这一条回滚，记"加锁冲突，下一轮再试"，
    接着做下一条；别的数据库错误照常抛出。"""
    from sqlalchemy.exc import OperationalError

    from youju import dedup

    def entry(i):
        return dict(
            id=i,
            document_id=i,
            kind="企业申报截止",
            date="2026-09-30",
            time=None,
            origin="rule",
            ai_agrees=True,
            reopened=False,
            evidence=QUOTE,
            body_sha256="x",
        )

    rolled = []
    session = SimpleNamespace(
        get=lambda model, key: SimpleNamespace(id=key, document_id=key),
        commit=lambda: None,
        rollback=lambda: rolled.append(True),
    )
    monkeypatch.setattr(verify, "queue", lambda *args: [entry(1), entry(2)])
    monkeypatch.setattr(dedup, "canonical_ids", lambda *args: {1: 1, 2: 2})
    monkeypatch.setattr(dedup, "groups_for", lambda *args: {1: [1], 2: [2]})
    monkeypatch.setattr(verify, "_auto_reason", lambda *args: "不是企业申报截止")

    def lock(session, members, code=1213):
        if members == [1]:
            raise OperationalError("SELECT", {}, Exception(code, "Deadlock found"))

    monkeypatch.setattr(verify, "_lock_notice", lock)
    result = list(verify.auto_verify(session, date(2026, 9, 29), datetime(2026, 9, 29), True))
    assert [r["result"] for r in result] == ["加锁冲突，下一轮再试", "不是企业申报截止"]
    assert rolled == [True]
    monkeypatch.setattr(
        verify, "_lock_notice", lambda session, members: lock(session, members, 1054)
    )
    with pytest.raises(OperationalError):
        list(verify.auto_verify(session, date(2026, 9, 29), datetime(2026, 9, 29), True))


def test_auto_commit_conflict_does_not_mark_the_deadline_passed(monkeypatch):
    """提交时撞锁回滚的那一条不算"同一截止已通过"：同一截止的下一条候选照样再试。"""
    from sqlalchemy.exc import OperationalError

    from youju import dedup

    def entry(i):
        return dict(
            id=i,
            document_id=1,
            kind="企业申报截止",
            date="2026-09-30",
            time=None,
            origin="rule",
            ai_agrees=True,
            reopened=False,
            evidence=QUOTE,
            body_sha256="x",
        )

    commits = iter([OperationalError("COMMIT", {}, Exception(1213, "Deadlock found")), None])

    def commit():
        error = next(commits)
        if error is not None:
            raise error

    session = SimpleNamespace(
        get=lambda model, key: SimpleNamespace(id=key, document_id=1),
        commit=commit,
        rollback=lambda: None,
    )
    monkeypatch.setattr(verify, "queue", lambda *args: [entry(1), entry(2)])
    monkeypatch.setattr(dedup, "canonical_ids", lambda *args: {1: 1})
    monkeypatch.setattr(dedup, "groups_for", lambda *args: {1: [1]})
    monkeypatch.setattr(verify, "_lock_notice", lambda session, members: None)
    monkeypatch.setattr(verify, "_auto_reason", lambda *args: None)
    monkeypatch.setattr(verify, "apply_one", lambda *args, **kw: "通过")
    result = list(verify.auto_verify(session, date(2026, 9, 29), datetime(2026, 9, 29), True))
    assert [r["result"] for r in result] == ["加锁冲突，下一轮再试", "通过"]


def test_auto_stops_before_the_next_item_when_asked(monkeypatch):
    """stop 说要停（手工跑时每轮任务在等锁），做完手上这一条就不再做后面的。"""
    from youju import dedup

    def entry(i):
        return dict(
            id=i,
            document_id=i,
            kind="企业申报截止",
            date="2026-09-30",
            time=None,
            origin="rule",
            ai_agrees=True,
            reopened=False,
            evidence=QUOTE,
            body_sha256="x",
        )

    session = SimpleNamespace(
        get=lambda model, key: SimpleNamespace(id=key, document_id=key),
        commit=lambda: None,
        rollback=lambda: None,
    )
    monkeypatch.setattr(verify, "queue", lambda *args: [entry(1), entry(2), entry(3)])
    monkeypatch.setattr(dedup, "canonical_ids", lambda *args: {})
    monkeypatch.setattr(dedup, "groups_for", lambda *args: {})
    monkeypatch.setattr(verify, "_auto_reason", lambda *args: "不是企业申报截止")
    answers = iter([False, True])
    result = list(
        verify.auto_verify(
            session, date(2026, 9, 29), datetime(2026, 9, 29), stop=lambda: next(answers)
        )
    )
    assert [r["id"] for r in result] == [1]


def test_manual_apply_stops_when_the_round_is_waiting(monkeypatch, capsys):
    """手工跑拿着每轮任务的锁时，round.sh 来了（留了等锁标记）：不再做后面的，说明先停下。"""
    from contextlib import contextmanager

    from youju import ai
    from youju.ai import pipeline

    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(deadline_auto_verify="1"))
    monkeypatch.setattr(ai, "get_chat_client", lambda: object())
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: Session([]))

    @contextmanager
    def free_slot():
        yield True

    monkeypatch.setattr(pipeline, "round_slot", free_slot)
    monkeypatch.setattr(pipeline, "round_wanted", lambda: True)
    seen = []

    def fake_auto(session, today, now, write, clock=None, stop=None):
        seen.append(stop())
        return iter([])

    monkeypatch.setattr(verify, "auto_verify", fake_auto)
    assert cli._cmd_deadline_verify_auto(argparse.Namespace(apply=True, in_round=False)) == 0
    assert seen == [True]
    assert "自动核对先停下" in capsys.readouterr().out


def test_reopen_auto_preserves_long_reason():
    candidate = row(status="approved", reviewed_by=verify.AUTO_REVIEWER)
    reason = "退" * 200
    verify.reopen_group([candidate], reason, "审" * 80, datetime.now(UTC))
    assert candidate.audit_note == reason
    assert candidate.audit_by.endswith(verify.AUTO_REOPEN_TAG)
    assert len(candidate.audit_by) == 80


def test_auto_cli_apply(monkeypatch, capsys):
    """带 --apply：round.sh 传 --in-round 直接跑；手工跑先拿每轮任务的锁，拿不到（每轮任务在跑或
    在等锁）就跳过，不和抽截止、AI 检查、补详情同时写。"""
    from contextlib import contextmanager

    from youju import ai
    from youju.ai import pipeline

    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(deadline_auto_verify="1"))
    monkeypatch.setattr(ai, "get_chat_client", lambda: object())
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: Session([]))
    writes = []

    def fake_auto(session, today, now, write, clock=None, stop=None):
        writes.append(write)
        return iter(
            [
                {
                    "id": 1,
                    "document_id": 1,
                    "kind": "企业申报截止",
                    "date": "2026-09-30",
                    "time": None,
                    "result": "通过",
                }
            ]
        )

    monkeypatch.setattr(verify, "auto_verify", fake_auto)
    slots = []

    def slot(free):
        @contextmanager
        def fake():
            slots.append(free)
            yield free

        return fake

    monkeypatch.setattr(pipeline, "round_slot", slot(True))
    assert cli._cmd_deadline_verify_auto(argparse.Namespace(apply=True, in_round=False)) == 0
    assert "截止日期自动核对：候选 1 条，通过 1 条" in capsys.readouterr().out
    assert (slots, writes) == ([True], [True])
    monkeypatch.setattr(pipeline, "round_slot", slot(False))
    assert cli._cmd_deadline_verify_auto(argparse.Namespace(apply=True, in_round=False)) == 0
    assert "跳过截止日期自动核对" in capsys.readouterr().out
    assert (slots, writes) == ([True, False], [True])
    # round.sh 里跑：不再拿锁（它已经拿着）
    assert cli._cmd_deadline_verify_auto(argparse.Namespace(apply=True, in_round=True)) == 0
    assert (slots, writes) == ([True, False], [True, True])


def test_precheck_again_right_before_writing(setup, monkeypatch):
    """正文都锁住、证据都核过以后、写入之前再判断一次：等锁期间跨过截止时刻的不写。"""
    session, candidate = setup
    monkeypatch.setattr(app, "_lock_candidates", lambda session, row: [row])
    answers = iter([None, "截止已过"])
    result = verify.apply_one(session, item(), True, precheck=lambda r: next(answers))
    assert result == "截止已过"
    assert candidate.reviewed_by is None and candidate.status == "pending"


def test_auto_group_ai_time_and_stale_unclear(setup, monkeypatch):
    session, candidate = setup
    candidate.verification_unclear_hash = verify.body_hash("旧正文")
    monkeypatch.setattr(verify, "_ai_supports", lambda *args: True)
    monkeypatch.setattr(
        verify,
        "_ai_items",
        lambda *args: iter([(1, {}, date(2026, 9, 30), None)]),
    )
    args = (session, candidate, [1], datetime(2026, 9, 29))
    assert verify._auto_reason(*args) == "同一天有几个不同的时刻"
    monkeypatch.setattr(verify, "_ai_items", lambda *args: iter([]))
    assert verify._auto_reason(*args) is None
