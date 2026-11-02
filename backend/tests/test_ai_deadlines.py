import json
from datetime import date, time

import httpx

from youju.ai.client import ChatClient
from youju.ai.deadlines import extract_deadlines_with_ai


def make_client(items):
    content = json.dumps({"deadlines": items}, ensure_ascii=False)
    return ChatClient(
        "https://example.test",
        "key",
        "qwen",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": content}}]}
            )
        ),
    )


def item(date_text, evidence, label="材料报送截止", clock="", confidence="高"):
    return {
        "date": date_text,
        "time": clock,
        "label": label,
        "evidence": evidence,
        "confidence": confidence,
    }


BODY = "请于2026年10月1日前报送材料。"


def test_evidence_and_date_range_and_dedup():
    items = [
        item("2026-10-01", BODY),
        item("2026-10-01", BODY, label="其他", confidence="中"),  # 同一天去重
        item("2028-10-01", "请于2028年10月1日前报送材料。"),  # 不是原文
        item("2026-10-02", "凭空编造。"),
    ]
    result = extract_deadlines_with_ai(make_client(items), "标题", BODY, date(2026, 9, 1))
    assert [(r.kind, r.due_date, r.due_time, r.confidence) for r in result] == [
        ("材料报送截止", date(2026, 10, 1), None, "高")
    ]
    assert result[0].evidence == BODY


def test_whitespace_in_evidence_is_allowed():
    items = [item("2026-10-01", "请 于 2026 年 10 月 1 日 前 报送 材料。", clock="09:30")]
    result = extract_deadlines_with_ai(make_client(items), "标题", BODY, date(2026, 9, 1))
    assert result[0].due_time == time(9, 30)


def test_real_sentence_without_that_date_is_dropped():
    """句子是原文，但里面写的不是这个日期：典型的编造，丢掉。"""
    body = "请于2026年10月1日前报送材料。评审结果另行通知。"
    items = [
        item("2026-10-15", "请于2026年10月1日前报送材料。"),
        item("2026-10-01", "评审结果另行通知。"),
    ]
    assert extract_deadlines_with_ai(make_client(items), "标题", body, date(2026, 9, 1)) == []


def test_year_written_in_evidence_must_match():
    body = "上一年度于2025年10月1日前截止。"
    items = [item("2026-10-01", body)]
    assert extract_deadlines_with_ai(make_client(items), "标题", body, date(2026, 9, 1)) == []


def test_out_of_range_date_is_dropped():
    body = "请于2029年10月1日前报送材料。"
    items = [item("2029-10-01", body)]
    assert extract_deadlines_with_ai(make_client(items), "标题", body, date(2026, 9, 1)) == []


def test_unknown_label_and_confidence_are_normalized():
    items = [item("2026-10-01", BODY, label="随便写的", confidence="极高")]
    result = extract_deadlines_with_ai(make_client(items), "标题", BODY, date(2026, 9, 1))
    assert (result[0].kind, result[0].confidence) == ("其他", "低")


def test_long_evidence_is_cut_around_the_date():
    """和规则抽取一样，证据最多 120 字，并且保留日期那一段。"""
    long_sentence = (
        "为做好相关工作，" + "现将有关事项通知如下，" * 12 + "请于2026年10月1日前报送材料"
    )
    body = long_sentence + "。"
    items = [item("2026-10-01", long_sentence)]
    result = extract_deadlines_with_ai(make_client(items), "标题", body, date(2026, 9, 1))
    assert len(result[0].evidence) <= 120
    assert "2026年10月1日" in result[0].evidence


def test_bad_time_is_dropped():
    items = [item("2026-10-01", BODY, clock="25:00")]
    assert extract_deadlines_with_ai(make_client(items), "标题", BODY, date(2026, 9, 1)) == []


def test_evidence_across_semicolon_and_line_break_is_kept():
    """D-099 期 0：原句按全文核对。以前跨"；"或换行的原句对不上切好的单句，整条丢掉。"""
    body = "申报时间：2026年9月20日至10月15日；材料报送截止\n2026年10月20日。"
    items = [
        item("2026-10-15", "申报时间：2026年9月20日至10月15日；材料报送截止", label="企业申报截止"),
        item("2026-10-20", "材料报送截止\n2026年10月20日"),
    ]
    result = extract_deadlines_with_ai(make_client(items), "标题", body, date(2026, 9, 1))
    assert [(r.due_date, r.kind) for r in result] == [
        (date(2026, 10, 15), "企业申报截止"),
        (date(2026, 10, 20), "材料报送截止"),
    ]
    # 跨行的原句存成一行
    assert result[1].evidence == "材料报送截止 2026年10月20日"


def test_evidence_still_has_to_be_verbatim():
    body = "申报截止时间为2026年10月15日；逾期不予受理。"
    items = [item("2026-10-15", "申报截止时间为2026年10月15日；逾期一律不受理")]
    assert extract_deadlines_with_ai(make_client(items), "标题", body, date(2026, 9, 1)) == []


def test_midnight_24_keeps_the_date_without_clock():
    """写"24 时"的是当天结束：留下这一条、只记日期（和规则一样）。以前过不了格式检查，整条丢掉。"""
    body = "请于2026年10月1日24时前报送材料。"
    items = [item("2026-10-01", body, clock="24:00")]
    result = extract_deadlines_with_ai(make_client(items), "标题", body, date(2026, 9, 1))
    assert [(r.due_date, r.due_time) for r in result] == [(date(2026, 10, 1), None)]


def test_one_digit_hour_is_read():
    items = [item("2026-10-01", BODY, clock="9:00")]
    result = extract_deadlines_with_ai(make_client(items), "标题", BODY, date(2026, 9, 1))
    assert result[0].due_time == time(9)


def test_stored_result_versions():
    from youju.ai.deadlines import RESULT_VERSION, stored_items, stored_version

    old = [{"date": "2026-10-01", "time": None}]
    new = {"version": RESULT_VERSION, "items": old}
    assert stored_items(old) == old and stored_version(old) == 1
    assert stored_items(new) == old and stored_version(new) == RESULT_VERSION
    assert stored_items(None) == [] and stored_version(None) == 0
    assert stored_items({"version": 2}) == [] and stored_version({"items": []}) == 0
