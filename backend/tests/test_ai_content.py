"""取内容（youju/ai/content.py，问一问开发设计第十一节）：这一步只取正文，行为和原来一模一样。

- 逐字核对：新的 interpret.validate 改用 content.locate，和改之前的写法（下面原样抄的
  _legacy_validate）在固定用例和随机用例（解读、对照各 400 份随机正文，每份最多 16 条引用）上
  逐项相同；
- 取内容：段落、正文指纹、用的哪一份正文和原来 prepare 里的算法相同，附件一律记"没读"；
- 两个来源（正文、附件）的句子不会互相算错。
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from youju.ai import content, interpret
from youju.ai.clause import CLAUSE
from youju.api import v1
from youju.models import Document, DocumentAttachment, DocumentDetail, Source

NOW = datetime(2026, 10, 3, 10, tzinfo=UTC)


def _legacy_validate(payload, paragraphs, *, skill=interpret.INTERPRET):
    """改之前的 interpret.validate 原样（main 66f8e7c0），只把模块里的名字加上 interpret. 前缀。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("模型结果结构无效")
    by_pid = {p["pid"]: p for p in paragraphs}
    body = interpret.paragraph_body(paragraphs)
    normalized_body, body_offsets = interpret.normalize(body)
    normalized_paragraphs = {p["pid"]: interpret.normalize(p["text"]) for p in paragraphs}
    sentences = interpret.sentence_spans(body)
    kept = []
    reasons = dict.fromkeys(interpret.DROPPED_REASONS, 0)
    seen = set()
    for item in payload["items"]:
        refs = item.get("refs") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("type"), str)
            or item["type"] not in skill.types
            or not isinstance(refs, list)
            or not refs
        ):
            reasons["bad_shape"] += 1
            continue
        originals, ranges = [], []
        reason = None
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("quote"), str):
                reason = "bad_shape"
                break
            quote = interpret.normalize(ref["quote"].strip())[0]
            if len(quote) < 6:
                reason = "too_short"
                break
            pid = ref.get("pid")
            paragraph = by_pid.get(pid) if isinstance(pid, str) else None
            at = -1
            if paragraph is not None:
                normalized, offsets = normalized_paragraphs[pid]
                at = normalized.find(quote)
            if at >= 0:
                first = paragraph["start"] + offsets[at][0]
                last = paragraph["start"] + offsets[at + len(quote) - 1][1]
            else:
                at = normalized_body.find(quote)
                if at < 0:
                    reason = "not_found"
                    break
                if normalized_body.find(quote, at + 1) >= 0:
                    reason = "ambiguous"
                    break
                first, last = body_offsets[at][0], body_offsets[at + len(quote) - 1][1]
                paragraph = next(p for p in paragraphs if p["start"] <= first < p["end"])
            covered = [(a, b) for a, b in sentences if a < last and b > first]
            if not covered:
                reason = "not_found"
                break
            start, end = covered[0][0], covered[-1][1]
            original = body[start:end]
            if "已脱敏]" in original:
                reason = "redacted"
                break
            originals.append({"pid": paragraph["pid"], "quote": original})
            ranges.append((start, end))
        if reason:
            reasons[reason] += 1
            continue
        identity = (item["type"], *ranges[0])
        if identity in seen:
            reasons["duplicate"] += 1
            continue
        seen.add(identity)
        kept.append({"type": item["type"], "text": originals[0]["quote"], "refs": originals})
        if skill.keys:
            keys = item.get("keys")
            kept[-1]["keys"] = [key for key in skill.keys if isinstance(keys, list) and key in keys]
    present = {item["type"] for item in kept}
    return {
        skill.result_field: kept,
        **({"missing": [t for t in skill.types if t not in present]} if not skill.keys else {}),
        "dropped": sum(reasons.values()),
        "dropped_reasons": reasons,
        "refusal": None if kept else "没有通过原文逐字核对的有效条目，请查看官方原文",
    }


FIXED_BODY = (
    "一、申报对象\n"
    "注册在山东省境内、具有独立法人资格的科技型中小企业。\n"
    "\n"
    "二、支持方式\n"
    "每项省资助经费不超过３００万元，采取“先立项、后补助”方式。联系邮箱：[邮箱已脱敏]，请按时报送。\n"
    "同一项目负责人同一年度只能申报一项省科技计划项目。\n"
    "同一项目负责人同一年度只能申报一项省科技计划项目。\n"
    "三、申报时间\n"
    "网上申报时间为2026年5月10日至5月20日，逾期不\n"
    "予受理。\n" + "（四）其他要求。" * 160 + "\n"
)


def _ref(pid, quote):
    return {"pid": pid, "quote": quote}


FIXED_ITEMS = [
    {"type": "对象", "refs": [_ref("P002", "具有独立法人资格的科技型中小企业")]},
    # 段号给错：退回整份正文找唯一一处
    {"type": "支持", "refs": [_ref("P001", "每项省资助经费不超过300万元")]},
    # 全角、多余空白
    {"type": "支持", "refs": [_ref("P004", "采取 “先立项、 后补助”方式")]},
    # 含脱敏
    {"type": "方式", "refs": [_ref("P004", "联系邮箱：[邮箱已脱敏]")]},
    # 两处一样：段号对得上时用这一段，段号不对时不唯一
    {"type": "条件", "refs": [_ref("P005", "同一项目负责人同一年度只能申报")]},
    {"type": "条件", "refs": [_ref("P999", "同一项目负责人同一年度只能申报")]},
    # 跨行接回一句（PDF 版面折行）
    {"type": "时间", "refs": [_ref("P008", "网上申报时间为2026年5月10日至5月20日")]},
    {"type": "时间", "refs": [_ref("P009", "予受理。")]},
    # 太短、找不到、结构不对、类型不对、重复
    {"type": "时间", "refs": [_ref("P008", "5月")]},
    {"type": "材料", "refs": [_ref("P001", "营业执照复印件一份")]},
    {"type": "材料", "refs": [{"pid": "P001"}]},
    {"type": "不认识", "refs": [_ref("P002", "具有独立法人资格的科技型中小企业")]},
    {"type": "对象", "refs": [_ref("P002", "注册在山东省境内、具有独立法人资格")]},
    {"type": "对象", "refs": []},
    "不是对象",
    # 超长段被硬切以后，引用跨过切口
    {"type": "材料", "refs": [_ref("P010", "（四）其他要求。（四）其他要求。（四）其他要求。")]},
    {"type": "材料", "refs": [_ref(None, "其他要求。（四）其他要求")]},
    {"type": "材料", "refs": [_ref(12, "具有独立法人资格的科技型中小企业")]},
]


@pytest.mark.parametrize("skill", [interpret.INTERPRET, CLAUSE])
def test_validate_same_as_before_on_fixed_cases(skill):
    paragraphs = interpret.split_paragraphs(FIXED_BODY)
    assert len(paragraphs) > 10  # 超长段确实被硬切了
    items = [
        {**item, "type": "条件", "keys": ["企业", "其他"]}
        if skill is CLAUSE and isinstance(item, dict)
        else item
        for item in FIXED_ITEMS
    ]
    payload = {"items": items}
    assert interpret.validate(payload, paragraphs, skill=skill) == _legacy_validate(
        payload, paragraphs, skill=skill
    )


@pytest.mark.parametrize("skill", [interpret.INTERPRET, CLAUSE])
def test_validate_same_as_before_on_random_cases(skill):
    rng = random.Random(20261027)
    clauses = [
        "申报单位应在规定时间内提交材料",
        "注册时间满一年以上",
        "联系电话：[电话已脱敏]",
        "Ｗｏｒｄ版申报书一式三份",
        "每家企业限报1项",
        "逾期不予受理",
        "具体以主管部门解释为准",
        "资助金额不超过50万元",
    ]
    ends = ["。", "；", "！", "", "\n", "  "]
    for _ in range(400):
        paragraphs_text = []
        for _ in range(rng.randint(1, 8)):
            parts = [rng.choice(clauses) + rng.choice(ends) for _ in range(rng.randint(1, 6))]
            if rng.random() < 0.1:
                parts *= 40  # 偶尔来一个超过 1000 字的段
            paragraphs_text.append("".join(parts))
        body = "\n".join(paragraphs_text) + rng.choice(["", "\n", "\n\n"])
        paragraphs = interpret.split_paragraphs(body)
        if not paragraphs:
            continue
        whole = interpret.paragraph_body(paragraphs)
        items = []
        for _ in range(rng.randint(1, 15)):
            a = rng.randrange(len(whole))
            quote = whole[a : a + rng.randint(2, 45)]
            if rng.random() < 0.2:
                quote = quote.replace("，", " ，").replace("1", "１")
            pid = rng.choice(
                [p["pid"] for p in paragraphs] + ["P999", None, 7, paragraphs[0]["pid"]]
            )
            refs = [_ref(pid, quote)]
            if rng.random() < 0.2:
                refs.append(_ref(rng.choice(paragraphs)["pid"], rng.choice(clauses)))
            item = {"type": rng.choice(skill.types + ("别的",)), "refs": refs}
            if skill.keys:
                item["keys"] = rng.sample([*skill.keys, "不认识"], rng.randint(0, 3))
            items.append(item)
        if rng.random() < 0.3:
            items.append(dict(rng.choice(items)))
        payload = {"items": items}
        assert interpret.validate(payload, paragraphs, skill=skill) == _legacy_validate(
            payload, paragraphs, skill=skill
        )


def test_validate_same_as_before_without_paragraphs():
    payload = {"items": [{"type": "对象", "refs": [_ref("P001", "具有独立法人资格的企业")]}]}
    assert interpret.validate(payload, []) == _legacy_validate(payload, [])


def _attachment(text):
    return content.SourceText(
        key="att-0123456789ab",
        version="v",
        text=text,
        attachments=(content.Attachment(1, 1, "申报书", True),),
    )


def test_locate_keeps_body_and_attachment_apart():
    body = "申报对象为科技型中小企业。\n资助金额不超过50万元。"
    att = "申报书一式三份，加盖公章。\n资助金额不超过50万元。"
    texts = content.Texts(
        sources=(
            content.SourceText(key=content.BODY, version="b", text=body),
            _attachment(att),
        ),
        segments=(
            content.Segment("P001", content.BODY, "申报对象为科技型中小企业。", 0, 13),
            content.Segment("P002", content.BODY, "资助金额不超过50万元。", 14, len(body)),
            content.Segment(
                "A0123456789ab-1",
                "att-0123456789ab",
                "申报书一式三份，加盖公章。",
                0,
                13,
                "第 1 段",
            ),
            content.Segment(
                "A0123456789ab-2",
                "att-0123456789ab",
                "资助金额不超过50万元。",
                14,
                len(att),
                "第 2 段",
            ),
        ),
    )
    # 只在附件里有的句子：段号给的是正文的段，不会到附件里去找
    assert content.locate(texts, "P001", "申报书一式三份") == (None, "not_found")
    located, why = content.locate(texts, "A0123456789ab-1", "申报书一式三份")
    assert why is None
    assert (located.source, located.sid, located.quote, located.place) == (
        "att-0123456789ab",
        "A0123456789ab-1",
        "申报书一式三份，加盖公章。",
        "第 1 段",
    )
    # 两个来源都有的句子：按段号所属的来源找，不算"不唯一"，也不会算成另一个来源的
    located, _ = content.locate(texts, "P001", "资助金额不超过50万元")
    assert (located.source, located.sid) == (content.BODY, "P002")
    located, _ = content.locate(texts, "A0123456789ab-1", "资助金额不超过50万元")
    assert (located.source, located.sid, located.place) == (
        "att-0123456789ab",
        "A0123456789ab-2",
        "第 2 段",
    )
    # 段号对不上任何一段：只在正文里找
    located, _ = content.locate(texts, "X9", "资助金额不超过50万元")
    assert located.source == content.BODY
    assert content.locate(texts, None, "加盖公章") == (None, "not_found")


def test_locate_ask_rules():
    """问答的逐字核对（locate_ask，第六节第 3 条"在哪找""每句怎么判"，Codex 第 40 轮）。"""
    masked = "联系人：[姓名已脱敏]，联系电话：[电话已脱敏]。"
    consult = "咨询电话：[电话已脱敏]。"
    body = "\n".join(
        [
            "申报书一式三份。" + masked,
            consult + "申报截止时间为11月20日。",
            "申报单位须提交盖章的申请书。",
            "申请书须加盖公章。承诺书也须加盖公章。",
            "咨询电话另行公布。",
        ]
    )
    texts = content.Texts.from_paragraphs(interpret.split_paragraphs(body))
    second = len("申报书一式三份。" + masked) + 1  # 第 2 段在正文里的起点
    # 含已脱敏的不看长短：只摘了遮挡前不到 6 个字的一小段也算 redacted，带上那一句的位置
    got = content.locate_ask(texts, "P001", "联系电话：")
    assert got.kind == "redacted" and got.masked == ((content.BODY, 8, 8 + len(masked)),)
    # 这一段没有，到同一来源里交给模型的其他段找：两处都有、每处都含遮掉的号码，每句各记一次
    got = content.locate_ask(texts, "P003", "[电话已脱敏]")
    assert got.kind == "redacted"
    assert set(got.masked) == {
        (content.BODY, 8, 8 + len(masked)),
        (content.BODY, second, second + len(consult)),
    }
    # 不止一处、又不全含遮掉的号码：ambiguous（不先看长短）；一处含号码、一处不含也一样
    assert content.locate_ask(texts, "P004", "加盖公章").kind == "ambiguous"
    assert content.locate_ask(texts, "P003", "咨询电话").kind == "ambiguous"
    # 只有一处、不含：少于 6 个字 too_short；够了能显示，出处按实际找到的那一段
    assert content.locate_ask(texts, "P003", "盖章的申请").kind == "too_short"
    got = content.locate_ask(texts, "P003", "申报书一式三份")
    assert got.kind == "shown"
    assert (got.located.sid, got.located.quote) == ("P001", "申报书一式三份。")
    # 段号不是交给模型的某一段：直接 not_found，不到正文里找（解读用的 locate 照旧会找）
    assert content.locate_ask(texts, "P999", "申报书一式三份").kind == "not_found"
    assert content.locate(texts, "P999", "申报书一式三份")[0] is not None
    for sid, quote in (("P001", "   "), ("P001", "原文里没有的一句话"), ("P001", None)):
        assert content.locate_ask(texts, sid, quote).kind == "not_found"


def test_locate_ask_stays_in_its_source_and_the_given_segments():
    """不跨来源（正文段号的句子不到附件里找，反过来也一样）；来源里没交给模型的那截不找；
    这一段里有，就只认这一段，别的段里也有不算不唯一。"""
    body = "正文第一句写申报对象。正文第二句写截止时间。没交给模型的一句话。"
    att = "附件里写评分标准。正文第一句写申报对象。"  # 有一句和正文一模一样
    texts = content.Texts(
        sources=(
            content.SourceText(key="body", version="b", text=body),
            content.SourceText(
                key="att-1",
                version="a",
                text=att,
                attachments=(content.Attachment(5, 1, "指南", True),),
            ),
        ),
        segments=(
            content.Segment("P001", "body", body[:22], 0, 22),
            content.Segment("A1-1", "att-1", att, 0, len(att)),
        ),
    )
    # 两个来源都有的句子：按段号所属的来源找，不算不唯一，也不会算成另一个来源的
    for sid, source in (("A1-1", "att-1"), ("P001", "body")):
        got = content.locate_ask(texts, sid, "正文第一句写申报对象")
        assert (got.kind, got.located.source, got.located.sid) == ("shown", source, sid)
    assert content.locate_ask(texts, "P001", "附件里写评分标准").kind == "not_found"
    assert content.locate_ask(texts, "P001", "没交给模型的一句话").kind == "not_found"
    got = content.locate_ask(texts, "A1-1", "附件里写评分标准")
    assert (got.kind, got.located.source, got.located.sid) == ("shown", "att-1", "A1-1")
    both = content.Texts.from_paragraphs(
        interpret.split_paragraphs(
            "申请书须加盖单位公章后提交。\n承诺书须加盖单位公章后提交。\n逾期不予受理。"
        )
    )
    got = content.locate_ask(both, "P002", "须加盖单位公章后提交")
    assert (got.kind, got.located.sid, got.located.quote) == (
        "shown",
        "P002",
        "承诺书须加盖单位公章后提交。",
    )
    assert content.locate_ask(both, "P003", "须加盖单位公章后提交").kind == "ambiguous"


@pytest.fixture
def doc(session):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="科技厅",
        column_name="政策",
        url="https://official.test/policy/",
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    row = Document(
        source_id=source.id,
        title="申报通知",
        kind_override="申报通知",
        url="https://official.test/content-1",
        url_key="official.test/content-1",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(row)
    session.flush()
    session.add(
        DocumentDetail(document_id=row.id, body_text=FIXED_BODY, fetched_at=NOW, status="ok")
    )
    session.commit()
    return row


def test_load_takes_body_like_before_and_marks_attachments_unread(session, doc):
    raw = v1._reading_body(session, doc.id)
    body = v1.redact_sensitive(raw or "")
    loaded = content.load(session, doc.id)
    # 和原来 prepare 里的算法一样：段落按原规则切、正文指纹是库里原文的摘要、用的是本份正文
    assert loaded.paragraphs() == interpret.split_paragraphs(body)
    assert loaded.source(content.BODY).version == interpret.digest(raw)
    assert loaded.source(content.BODY).text == body
    assert (loaded.doc_id, loaded.body_id, loaded.document_ids) == (doc.id, doc.id, [doc.id])
    assert (loaded.kind, loaded.config, loaded.partial) == ("申报通知", content.CONFIG_BODY, False)
    assert loaded.unread == [] and loaded.attachments_total == 0
    assert loaded.scope_text() == "读了：正文（这份通知没有附件）"
    for i, (name, position) in enumerate([("申报书.docx", 2), ("汇总表.xlsx", 1)], 1):
        session.add(
            DocumentAttachment(
                document_id=doc.id,
                position=position,
                name=name,
                url=f"https://official.test/a{i}",
                extension=name.rsplit(".", 1)[1],
            )
        )
    session.commit()
    ids = dict(session.execute(select(DocumentAttachment.name, DocumentAttachment.id)).all())
    loaded = content.load(session, doc.id)
    # 按详情页附件列表的顺序编"附件 N"，这一步都没读；每个带上附件编号（按编号对，不按顺序对）
    assert loaded.unread == [
        content.Unread("附件 1《汇总表.xlsx》", "off", attachment_id=ids["汇总表.xlsx"]),
        content.Unread("附件 2《申报书.docx》", "off", attachment_id=ids["申报书.docx"]),
    ]
    assert loaded.attachments_total == 2
    assert loaded.scope_text() == "只读了正文，附件没读"
    versions = loaded.versions()
    assert versions["config"] == content.CONFIG_BODY
    assert versions["sources"] == {content.BODY: interpret.digest(raw)}
    # prepare 写进快照的几项都从这里来，逐项相同
    snapshot, _, key = interpret.prepare(session, doc.id, "fake-model")
    assert snapshot["paragraphs"] == interpret.split_paragraphs(body)
    assert snapshot["body_hash"] == interpret.digest(raw)
    assert snapshot["body_length"] == len(body)
    assert (snapshot["doc_id"], snapshot["body_id"], snapshot["document_ids"]) == (
        doc.id,
        doc.id,
        [doc.id],
    )
    assert snapshot["attachments"] == 2 and snapshot["kind"] == "申报通知"
    assert key == interpret.digest(
        [
            "interpret",
            snapshot["body_hash"],
            snapshot["verification_version"],
            interpret.PROMPT_VERSION,
            "fake-model",
        ]
    )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("missing", "unavailable"),
        ("list", "list"),
        ("other", "kind"),
        ("source", "source"),
        ("gone", "link_gone"),
        ("empty", "no_body"),
        ("long", "too_long"),
    ],
)
def test_check_gives_the_same_reasons(session, doc, change, code):
    if change == "missing":
        doc_id = doc.id + 10_000
    else:
        doc_id = doc.id
        if change == "list":
            doc.kind_override = "名单"
        elif change == "other":
            doc.kind_override = "其他"
        elif change == "source":
            doc.source.rights_level = 2
        elif change == "gone":
            doc.link_gone_at = NOW
        else:
            detail = session.get(DocumentDetail, doc.id)
            detail.body_text = "   " if change == "empty" else "字" * (interpret.MAX_BODY + 1)
        session.commit()
    with pytest.raises(interpret.InterpretRefusal) as exc:
        content.check(session, doc_id)
    assert exc.value.code == code
    if change == "gone":
        # 不看原网页撤下了没有的场合（已有的结果、排队的任务、列表预算）照常能做
        content.check(session, doc_id, link=False)
        assert content.load(session, doc_id, link=False).doc_id == doc.id


def test_old_eligible_body_still_gives_the_four_values(session, doc):
    # 旧的调用（interpret.eligible_body）转调 content，照样给（通知、类型、原文、遮过号码的正文）
    found, kind, raw, body = interpret.eligible_body(session, doc.id)
    assert (found.id, kind, raw) == (doc.id, "申报通知", FIXED_BODY)
    assert body == v1.redact_sensitive(FIXED_BODY)


def test_load_prefers_its_own_body_in_a_merged_group(session, doc):
    from youju.models import DocumentMerge

    other = Document(
        source_id=doc.source_id,
        title=doc.title,
        kind_override="申报通知",
        url="https://official.test/content-2",
        url_key="official.test/content-2",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(other)
    session.flush()
    session.add(
        DocumentDetail(document_id=other.id, body_text=FIXED_BODY, fetched_at=NOW, status="ok")
    )
    session.add(
        DocumentMerge(duplicate_id=other.id, canonical_id=doc.id, active=True, match_key="test")
    )
    session.commit()
    loaded = content.load(session, doc.id)
    # 组里两份正文一样：用本份的（原来 prepare 的排法：本份在前，再按编号）
    assert loaded.body_id == doc.id
    assert sorted(loaded.document_ids) == sorted([doc.id, other.id])
