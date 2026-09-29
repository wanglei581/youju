from types import SimpleNamespace

import pytest

from youju.ai.fields import extract_fields_with_ai
from youju.fields import extract_audience, extract_support


def values(body: str):
    return {item.value: item for item in extract_support(body)}


def test_support_rules_cover_labels_and_keep_each_evidence_sentence():
    body = (
        "本项目提供财政资金补助。\n"
        "符合条件的企业可申请贷款贴息。\n"
        "申报认定为示范企业后获得资格。\n"
        "项目入库备案后纳入管理。\n"
        "项目立项可安排项目资金。"
    )
    found = values(body)
    assert set(found) == {"资金支持", "贷款贴息", "资格认定", "入库备案", "项目立项"}
    for item in found.values():
        assert item.evidence in body
        assert body[item.evidence_offset :].startswith(item.evidence)


def test_support_rules_reject_negation_and_project_or_recognition_boundaries():
    body = (
        "不予支持的项目不得重复申报。"
        "本通知不属于资格认定范围。"
        "项目申报材料应在网上提交。"
        "项目申报并完成立项后可申请项目资金。"
        "认定工作完成后公布名单。"
    )
    found = values(body)
    assert found["资金支持"].evidence.startswith("项目申报并完成立项")
    assert "资格认定" in found
    assert "项目立项" in found
    assert found["项目立项"].evidence.endswith("项目资金。")


def test_audience_is_original_sentence_plus_next_sentence_and_limited():
    body = "一、申报对象：在本省登记的企业。申报条件还包括正常经营和信用良好。"
    found = extract_audience(body)
    assert len(found) == 1
    assert found[0].value == body
    assert found[0].evidence in body


@pytest.mark.parametrize(
    "body,label",
    [
        ("企业应符合资格认定条件。", "资格认定"),
        ("认定条件包括正常经营。", "资格认定"),
        ("认定标准另行规定。", "资格认定"),
        ("企业需满足申报认定条件。", "资格认定"),
        ("项目尚未立项。", "项目立项"),
        ("该项目不予立项。", "项目立项"),
        ("未立项的项目需补充材料。", "项目立项"),
        ("备案材料不齐全。", "入库备案"),
        ("该项目不予备案。", "入库备案"),
        ("请提交备案材料。", "入库备案"),
        ("备案号为123号。", "入库备案"),
    ],
)
def test_support_rejects_conditions_negated_actions_and_filing_materials(body, label):
    assert label not in values(body)


@pytest.mark.parametrize(
    "body,label",
    [
        ("符合认定条件的企业可认定为示范企业。", "资格认定"),
        ("开展申报认定。", "资格认定"),
        ("开展认定工作。", "资格认定"),
        ("组织认定创新企业。", "资格认定"),
        ("企业可申请入库。", "入库备案"),
        ("企业可申请备案登记。", "入库备案"),
        ("企业纳入省级培育库。", "入库备案"),
        ("项目立项后给予支持。", "项目立项"),
    ],
)
def test_support_accepts_positive_actions(body, label):
    assert label in values(body)


@pytest.mark.parametrize("separator", ["", "\n\n  ", "\r\n\t", "   "])
def test_audience_preserves_original_whitespace(separator):
    body = f"开头。\n一、申报对象：本省企业。{separator}申报单位须正常经营。后续说明。"
    candidate = extract_audience(body)[0]
    assert candidate.value == f"一、申报对象：本省企业。{separator}申报单位须正常经营。"
    assert candidate.value in body


def test_audience_rule_limits_original_excerpt_to_200_characters():
    body = "申报对象：" + "本省企业" * 100 + "。"
    candidate = extract_audience(body)[0]
    assert len(candidate.value) == 200
    assert candidate.value in body


@pytest.mark.parametrize(
    "value,valid",
    [
        ("本省企业。", True),
        ("申报对象：本省企业。\n须正常经营。", True),
        ("申报对象：本省企业。 须正常经营。", False),
        ("本省正常经营的企业", False),
        ("本省企业。信用良好。", False),
        ("企" * 200, True),
        ("企" * 201, False),
    ],
)
def test_ai_audience_must_be_bounded_contiguous_original(value, valid):
    body = "申报对象：本省企业。\n须正常经营。信用良好。" + "企" * 201
    client = SimpleNamespace(
        complete_json=lambda *_: {
            "fields": [{"field": "audience", "value": value, "evidence": "本省企业。"}]
        }
    )
    candidates = extract_fields_with_ai(client, "申报通知", body)
    assert bool(candidates) is valid
    if valid:
        assert candidates[0].value == value
