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
    assert (
        found[0].value
        == ("一、申报对象：在本省登记的企业。 申报条件还包括正常经营和信用良好。")[:200]
    )
    assert found[0].evidence in body
