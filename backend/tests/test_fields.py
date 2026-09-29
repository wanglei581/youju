from types import SimpleNamespace

import pytest

from youju.ai.fields import extract_fields_with_ai
from youju.fields import extract_audience, extract_rules, extract_support


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
        ("不得给予资金支持。", "资金支持"),
        ("取消贷款贴息。", "贷款贴息"),
        ("暂不认定为示范企业。", "资格认定"),
        ("不予入库。", "入库备案"),
        ("不得入库。", "入库备案"),
        ("尚未入库。", "入库备案"),
        ("不纳入省级培育库。", "入库备案"),
        ("暂不提供其他支持。", "其他"),
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


@pytest.mark.parametrize(
    "body",
    [
        "2025年12月31日前纳入本市创新创业载体培育体系（“上海市科技创新创业载体库”入库）的载体。",
        "收等方面的支持措施，1000 字以内）",
        "因提供的审计报告被有关部门认定为虚假、重大失实报告……责任由申报主体自负。",
        "原则上上一年度已入库指南将作为下一年度指南建议遴选范围。",
        "省级科技计划或其他省直有关部门（单位）已经立项支持过的相同或相似的研发内容，不得作为指南建议再次提报。",
        "并按照《商业银行金融资产风险分类办法》认定为不良的贷款。",
        "（四）提交活动立项批复复印件。",
        "的企业，需上传立项文件。",
        "第一阶段完成后将对2个项目执行情况进行评估，根据评估结果确定后续支持方式。",
    ],
)
def test_v5_reviewed_false_positive_sentences_are_excluded(body):
    assert extract_support(body) == []


@pytest.mark.parametrize(
    "body,label",
    [
        ("按照省工信厅公告的步骤进行。", "资格认定"),
        ("由省工业和信息化厅公告为创新型中小企业。", "资格认定"),
        ("通过验收的，正式成为国家级零碳工厂。", "资格认定"),
        ("批复组建市级制造业创新中心并授牌。", "资格认定"),
        ("选择‘企业技术中心的认定与评价’在线办理。", "资格认定"),
        ("对照标准级、卓越级孵化器认定条件。", "资格认定"),
        ("企业配套经费与财政科技经费比例不低于2:1。", "资金支持"),
        ("本批首都科技创新券随时申请随时使用。", "资金支持"),
        ("通过省科技计划项目支持重点培育雏鹰企业开展关键核心技术攻关。", "资金支持"),
        (
            "允许先进制造业企业按照当期可抵扣进项税额加计5%抵减应纳增值税税额。",
            "其他",
        ),
        ("积极帮助项目单位争取国家和我市相关政策支持。", "其他"),
        ("进入资源池的监理机构供各级工信系统参考。", "入库备案"),
        ("申报省级储备制造业中试平台。", "入库备案"),
    ],
)
def test_v5_reviewed_false_negative_sentences_are_included(body, label):
    assert label in values(body)


def test_audience_is_disabled_from_public_rule_extract():
    assert [item.field for item in extract_rules("申报对象：本省企业。企业可申请贷款贴息。")] == [
        "support"
    ]


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
