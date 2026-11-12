"""独立抽查的离线行为；SQL 筛选由数据库测试覆盖。"""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace as Obj

from youju import deadline_verify as v
from youju.api import app
from youju.models import Document, DocumentDeadline

NOW = datetime(2026, 11, 12, tzinfo=UTC)
BODY = "请于2026年11月15日17:00前提交材料。"


def row(id, document_id=None):
    return Obj(
        id=id,
        document_id=document_id or id,
        status="approved",
        reviewed_by="Claude",
        reviewed_at=NOW,
        kind="企业申报截止",
        candidate_date=date(2026, 11, 15),
        candidate_time=time(17),
        approved_date=date(2026, 11, 15),
        approved_time=time(17),
        verification_corrected=False,
        audit_state=None,
        audit_note=None,
    )


class Session:
    def __init__(self, rows):
        self.rows = rows
        self.docs = {
            r.document_id: Obj(
                id=r.document_id,
                title="标题",
                url="https://example.org",
                source_id=1,
                publish_date=date(2026, 11, 1),
            )
            for r in rows
        }
        self.details = {r.document_id: Obj(status="ok", body_text=BODY) for r in rows}

    def get(self, model, key):
        if model is DocumentDeadline:
            return next((r for r in self.rows if r.id == key), None)
        return (self.docs if model is Document else self.details).get(key)

    def scalar(self, stmt):
        return self.details[1]


def answer(**kwargs):
    return (
        dict(
            id=1,
            date="2026-11-15",
            time="17:00",
            quote=BODY,
            note="已核对",
            body_sha256=v.body_hash(BODY),
        )
        | kwargs
    )


def test_sample_independent_and_reproducible(monkeypatch):
    rows = [row(1), row(2), row(3)]
    s = Session(rows)
    monkeypatch.setattr(v, "_verified_groups", lambda *args: [[rows[0], rows[1]], [rows[2]]])
    a = v.sample(s, rate=0.5, minimum=0, seed=42, now=NOW)
    assert a == v.sample(s, rate=0.5, minimum=0, seed=42, now=NOW)
    assert len(a) == 1
    # 只给独立核对要用的，不给 Claude 的结论（日期、时间、原句）
    assert set(a[0]) == {
        "id",
        "title",
        "url",
        "kind",
        "publish_date",
        "sentences",
        "truncated",
        "body_sha256",
    }
    assert len(v.sample(s, minimum=5, now=NOW)) == 2


def test_spotcheck_dry_run_and_match(monkeypatch):
    r = row(1)
    s = Session([r])
    monkeypatch.setattr(app, "_lock_group", lambda *args: [r])
    assert v.spotcheck(s, answer(), now=NOW)["result"] == "抽查一致"
    assert r.audit_state is None
    assert v.spotcheck(s, answer(), True, NOW)["result"] == "抽查一致"
    # 只留痕、不改 audit_state：每天的质检只查 audit_state 为空的，不能因为抽查一致就再也不查
    assert (r.audit_state, r.audit_by, r.audit_at) == (None, "Codex", NOW)
    assert r.audit_note.startswith("Codex 抽查一致")
    r.audit_state = "flagged"
    assert "未覆盖" in v.spotcheck(s, answer(), True, NOW)["result"]
    assert r.audit_state == "flagged"


def test_match_marks_the_group_but_leaves_flagged_twins(monkeypatch):
    # 同组同一截止各条都留痕；质检存疑的副本不碰（agy 审阅）
    r, twin, flagged = row(1), row(2), row(3)
    flagged.audit_state, flagged.audit_note, flagged.audit_by = "flagged", "AI 说日期不对", None
    s = Session([r, twin, flagged])
    monkeypatch.setattr(app, "_lock_group", lambda *args: [r, twin, flagged])
    assert v.spotcheck(s, answer(), True, NOW)["result"] == "抽查一致"
    assert (r.audit_by, twin.audit_by) == ("Codex", "Codex")
    assert (flagged.audit_state, flagged.audit_note, flagged.audit_by) == (
        "flagged",
        "AI 说日期不对",
        None,
    )


def test_not_found_ignores_the_quote_and_reopens(monkeypatch):
    # 写"没找到"时 quote 里写的说明不当证据、也不拦：照样整组退回（agy 审阅）
    r = row(1)
    s = Session([r])
    monkeypatch.setattr(app, "_lock_group", lambda *args: [r])
    monkeypatch.setattr(v, "_verified_groups", lambda *args: [[r]])
    a = answer(date=None, time=None, quote="正文里没有写这一类截止")
    assert v.spotcheck(s, a, True, NOW)["result"] == "抽查不一致，退回待核对"
    assert r.status == "pending" and "未找到" in r.audit_note


def test_follow_up_tries_other_copies_when_the_first_body_is_unavailable(monkeypatch):
    # 回查同一类：组里第一条正文不可用时，接着试同组别的副本（agy 审阅）
    r, gone, other = row(1), row(2, document_id=2), row(3, document_id=3)
    s = Session([r, gone, other])
    monkeypatch.setattr(app, "_lock_group", lambda *args: [r])
    monkeypatch.setattr(v, "_verified_groups", lambda *args: [[r], [gone, other]])
    real = v._sample_item
    monkeypatch.setattr(
        v, "_sample_item", lambda session, row: None if row is gone else real(session, row)
    )
    result = v.spotcheck(s, answer(date=None, time=None, quote=None), False, NOW)
    assert [x["id"] for x in result["follow_up"]] == [3]


def test_reopen_group_followup_and_stale(monkeypatch):
    r, twin, other = row(1), row(2), row(3)
    s = Session([r, twin, other])
    monkeypatch.setattr(app, "_lock_group", lambda *args: [r, twin])
    monkeypatch.setattr(v, "_verified_groups", lambda *args: [[r, twin], [other]])
    a = answer(date=None, time=None, quote=None)
    assert "正文" in v.spotcheck(s, a | {"body_sha256": "old"}, True, NOW)["result"]
    assert r.status == twin.status == "approved"
    dry = v.spotcheck(s, a, now=NOW)
    assert [x["id"] for x in dry["follow_up"]] == [3]
    assert r.status == twin.status == "approved"
    assert v.spotcheck(s, a, True, NOW)["result"] == "抽查不一致，退回待核对"
    for x in (r, twin):
        assert x.status == "pending" and x.audit_state == "reopened"
        assert x.audit_by == "Codex" and "未找到" in x.audit_note
        assert x.approved_date is None and x.reviewed_by is None
    assert other.status == "approved"


def test_quote_must_be_in_the_body(monkeypatch):
    r = row(1)
    s = Session([r])
    monkeypatch.setattr(app, "_lock_group", lambda *args: [r])
    # 不在正文里的原句不认，不退回
    wrong = answer(date="2026-11-16", quote="请于2026年11月16日17:00前提交材料。")
    assert "出现在当前正文" in v.spotcheck(s, wrong, True, NOW)["result"]
    assert r.status == "approved"
    # 空白抄得不一样照认（独立 Claude 审阅：和 Claude 写回一样按去空白后比）
    spaced = answer(quote="请于 2026 年 11 月 15 日\u300017:00 前提交材料。")
    assert v.spotcheck(s, spaced, False, NOW)["result"] == "抽查一致"


def test_sample_sentences_find_the_deadline_beyond_the_first_few(monkeypatch):
    # 独立 Claude 审阅：日期隔着空白、全角写的也找得到；"1.5万元""第2.1条"不算日期；
    # 写着截止一类字样的句子排前面，截止句排在第 8 句也给得到；截掉了标 truncated
    filler = "".join(f"第{i}批项目已于2026年{i}月1日公示。" for i in range(1, 8))
    body = (
        "资助额度为1.5万元。按第2.1条执行。"
        + filler
        + "申报时间：2026 年 11 月 15 日\n17:00前提交。"
        + "其他说明见2026年1月2日通知。" * 30
    )
    r = row(1)
    s = Session([r])
    s.details[1].body_text = body
    item = v._sample_item(s, r)
    assert item["sentences"][0].startswith("申报时间")
    assert not any("万元" in x or "第2.1条" in x for x in item["sentences"])
    assert item["truncated"] is True and len(item["sentences"]) <= 20
    assert item["publish_date"] == "2026-11-01"
