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


def test_evidence_starts_at_clause_boundary() -> None:
    sentence = (
        "材料说明，" + "补充证明文件齐全，" * 25 + "项目纸质申报材料受理截止时间为2026年10月16日。"
    )
    result = extract_deadlines(sentence, PUBLISHED)
    assert len(result) == 1
    assert result[0].evidence.startswith(("补充证明", "项目纸质"))
    assert "10月16日" in result[0].evidence
    assert len(result[0].evidence) <= 120


# PDF 转出的正文按版面换行（10/15 全面测试：证据句只剩"…报送至省工业和信息"、或只剩日期）
PDF_WRAPPED = "\n".join(
    [
        "关于组织申报省级专精特新产业孵化园的通知",
        "各市（州）工业和信息化主管部门，各有关单位：",
        "为加快培育专精特新中小企业，打造一批产业孵化载体，现组织",
        "开展省级专精特新产业孵化园申报工作，有关事项通知如下。",
        "一、申报材料",
        "申报单位按要求编制申报材料，经所在地主管部门审核后，于",
        "10 月 26 日前完成纸质申报,并将材料报送至省工业和信息",
        "化厅中小企业处，逾期不予受理，请各单位认真组织好申报。",
        "（二）申报时间：",
        "2026 年 9 月 20 日-10 月 26 日。",
    ]
)


def test_pdf_line_wraps_are_joined_before_cutting_evidence():
    found = {(c.due_date, c.evidence) for c in extract_deadlines(PDF_WRAPPED, PUBLISHED)}
    evidences = [e for d, e in found if d == date(2026, 10, 26)]
    assert any("报送至省工业和信息化厅中小企业处" in e for e in evidences), evidences
    # 单独一行的"申报时间："接上下一行的日期，证据句写明是什么时间
    assert any(e.startswith("（二）申报时间：2026 年 9 月 20 日-10 月 26 日") for e in evidences), (
        evidences
    )
    # 标题、编号开头的行照旧分开，不和上一行接成一句
    assert not any("通知各市" in e or "如下。一、" in e for e in evidences)


def test_web_text_one_paragraph_per_line_is_unchanged():
    body = "各单位：\n请于2026年10月20日前提交申报材料。\n联系人：张某\n电话：0531-1234567"
    [candidate] = extract_deadlines(body, PUBLISHED)
    assert candidate.evidence == "请于2026年10月20日前提交申报材料"
