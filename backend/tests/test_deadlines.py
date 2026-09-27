from datetime import date, time

import pytest

from youju.extract.deadlines import extract_deadlines

PUBLISHED = date(2026, 9, 10)


@pytest.mark.parametrize(
    ("sentence", "expected_date", "kind", "expected_time"),
    [
        ("企业申报截止时间为2026年9月30日。", date(2026, 9, 30), "企业申报截止", None),
        ("申报截止日期为2026年10月15日。", date(2026, 10, 15), "企业申报截止", None),
        ("请于2026年9月25日前完成申报。", date(2026, 9, 25), "企业申报截止", None),
        ("企业须在9月30日前提交申报。", date(2026, 9, 30), "企业申报截止", None),
        ("截至10月8日，请完成申请。", date(2026, 10, 8), "企业申报截止", None),
        ("申请单位请在2026-10-12之前办理。", date(2026, 10, 12), "企业申报截止", None),
        ("报名截止时间为2026/10/13。", date(2026, 10, 13), "企业申报截止", None),
        ("申报截止于10月20日17:00。", date(2026, 10, 20), "企业申报截止", time(17)),
        ("企业申报截至2026年10月21日18时30分。", date(2026, 10, 21), "企业申报截止", time(18, 30)),
        ("各市主管部门于10月25日前完成推荐。", date(2026, 10, 25), "推荐或审核截止", None),
        ("市级审核截止时间为2026年11月2日。", date(2026, 11, 2), "推荐或审核截止", None),
        ("区县主管部门推荐材料截至11月3日。", date(2026, 11, 3), "推荐或审核截止", None),
        ("纸质材料请于11月4日前报送。", date(2026, 11, 4), "材料报送截止", None),
        ("申报材料报送截止日期为11月5日。", date(2026, 11, 5), "材料报送截止", None),
        ("请在11月6日之前邮寄申请材料。", date(2026, 11, 6), "材料报送截止", None),
        ("受理时间为9月11日至9月30日。", date(2026, 9, 30), "企业申报截止", None),
        ("2026年度项目申报时间为10月1日至10月31日。", date(2026, 10, 31), "企业申报截止", None),
        ("截至2027年1月15日，各企业须完成申报。", date(2027, 1, 15), "企业申报截止", None),
        ("请于1月20日前完成申请。", date(2027, 1, 20), "企业申报截止", None),
        ("申报系统于12月1日止。", date(2026, 12, 1), "企业申报截止", None),
        ("项目申报时间至10月15日下午17:00。", date(2026, 10, 15), "企业申报截止", time(17)),
        ("申报截止于10月15日（周四）17:30。", date(2026, 10, 15), "企业申报截止", time(17, 30)),
    ],
)
def test_real_deadline_phrasings(sentence, expected_date, kind, expected_time):
    candidates = extract_deadlines(sentence, PUBLISHED)
    assert len(candidates) == 1
    assert (candidates[0].due_date, candidates[0].kind, candidates[0].due_time) == (
        expected_date,
        kind,
        expected_time,
    )
    assert len(candidates[0].evidence) <= 120


def test_multiple_deadlines():
    text = "企业申报截至9月30日；各市主管部门推荐截止10月10日；纸质材料11月1日前报送。"
    assert [d.due_date for d in extract_deadlines(text, PUBLISHED)] == [
        date(2026, 9, 30),
        date(2026, 10, 10),
        date(2026, 11, 1),
    ]


def test_multiple_deadlines_separated_by_comma_keep_their_types():
    result = extract_deadlines("企业申报截止9月30日，各市推荐截止10月10日。", PUBLISHED)
    assert [d.kind for d in result] == ["企业申报截止", "推荐或审核截止"]


def test_submission_window_then_review_deadlines():
    sentence = (
        "材料网上提交时间为2026年8月24日9:00至8月31日17:00，"
        "申报单位与依托单位分别于9月1日17:00、9月2日17:00前完成审核工作。"
    )
    result = extract_deadlines(sentence, date(2026, 8, 20))
    assert [d.due_date for d in result] == [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)]


def test_parallel_submission_windows_do_not_extract_start_dates():
    sentence = (
        "项目申报单位网上填报项目申报书的受理时间为：2026年8月7日8:00至9月20日16:00，"
        "其中应急项目为8月7日8:00至8月20日16:00。"
    )
    result = extract_deadlines(sentence, date(2026, 8, 1))
    assert [d.due_date for d in result] == [date(2026, 9, 20), date(2026, 8, 20)]


@pytest.mark.parametrize(
    "sentence",
    [
        "本办法有效期至2028年12月31日，企业按规定申报。",
        "项目实施期限至2027年10月31日，申报时需注明。",
        "建设期限为2026年9月1日至2027年9月1日，企业可以申报。",
        "请在网站上查询2026年10月31日公布的申报名单。",
        "申报联系人电话于2026年10月30日更新。",
        "截至2026年10月30日，已有50家企业完成申报。",
        "企业申报截止日期另行通知。",
        "2026年11月31日前完成申报。",
        "申报项目立项时间为2023年1月1日以后，研发项目应在2026年度申报前已完成验收。",
        "项目自2026年10月1日起实施，申报前应做好准备。",
    ],
)
def test_ambiguous_or_unrelated_not_extracted(sentence):
    assert extract_deadlines(sentence, PUBLISHED) == []


def test_missing_year_needs_publish_date():
    assert extract_deadlines("申报截止10月31日。", None) == []


def test_long_sentence_evidence_keeps_the_date():
    sentence = "申报材料须按照附件说明准备，" + "所需证明文件齐全，" * 30 + "企业申报截止10月31日。"
    result = extract_deadlines(sentence, PUBLISHED)
    assert len(result) == 1
    assert "10月31日" in result[0].evidence
    assert len(result[0].evidence) <= 120
