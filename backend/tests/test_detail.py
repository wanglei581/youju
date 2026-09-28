from datetime import date

import pytest

from tests.conftest import read_fixture_html
from youju.crawler.detail import (
    MAX_ATTACHMENTS,
    _split_issuer_line,
    parse_detail,
    redact_sensitive,
)


@pytest.mark.parametrize(
    ("fixture", "url", "opening", "issuer", "number", "attachments", "published"),
    [
        (
            "detail_liaoning_344",
            "https://gxt.ln.gov.cn/gxt/tztg/2026092416504833875/index.shtml",
            "辽工信投资〔2026〕130号\n各市工业和信息化局",
            "辽宁省工业和信息化厅",
            "辽工信投资〔2026〕130号",
            2,
            date(2026, 9, 24),
        ),
        (
            "detail_liaoning_343",
            "https://gxt.ln.gov.cn/gxt/tztg/2026091717062662168/index.shtml",
            "各市（不含大连）工业和信息化局",
            "辽宁省工业和信息化厅、辽宁省财政厅、国家税务总局辽宁省税务局",
            None,
            1,
            date(2026, 9, 17),
        ),
        (
            "detail_jiangsu_510",
            "https://kxjst.jiangsu.gov.cn/art/2026/9/23/art_82540_11833785.html",
            "",  # 原站只有“点击正文.pdf”，HTML 无正文。
            None,
            None,
            1,
            date(2026, 9, 23),
        ),
        (
            "detail_fujian_915",
            "https://gxt.fujian.gov.cn/zwgk/gsgg/202609/t20260924_7218179.htm",
            "各设区市（不含厦门）工信局",
            "福建省工业和信息化厅、福建省教育厅、福建省文化和旅游厅、福建省广播电视局、福建省体育局、福建省文物局",
            None,
            2,
            date(2026, 9, 24),
        ),
        (
            "detail_guangdong_1072",
            "https://gdii.gd.gov.cn/zwgk/tzgg1011/content/post_4960472.html",
            "各地级以上市人民政府",
            "广东省工业和信息化厅",
            "粤工信材料函〔2026〕53号",
            0,
            date(2026, 9, 24),
        ),
        (
            "detail_most_1493",
            "https://www.most.gov.cn/tztg/202609/t20260908_197283.html",
            "国际大科学计划—平方公里阵列射电望远镜",
            "科学技术部国际科技合作中心",
            None,
            1,
            date(2026, 9, 8),
        ),
    ],
)
def test_real_detail_pages(fixture, url, opening, issuer, number, attachments, published) -> None:
    result = parse_detail(read_fixture_html(fixture), url)
    assert result.failure_reason is None
    assert result.body_text.startswith(opening) if opening else result.body_text == ""
    assert result.issuer == issuer
    assert result.doc_number == number
    assert result.publish_date == published
    assert len(result.attachments) == attachments
    assert all(a.url.startswith("http") and a.extension for a in result.attachments)
    assert "网站首页" not in result.body_text
    assert "相关新闻" not in result.body_text


def test_attachment_names_and_urls_are_absolute() -> None:
    result = parse_detail(
        read_fixture_html("detail_fujian_915"),
        "https://gxt.fujian.gov.cn/zwgk/gsgg/202609/t20260924_7218179.htm",
    )
    assert [a.extension for a in result.attachments] == ["doc", "doc"]
    assert result.attachments[0].name.startswith("1.视听系统典型案例申报书")
    assert result.attachments[0].url.startswith("https://gxt.fujian.gov.cn/")


def test_attachments_are_capped() -> None:
    links = "".join(f"<p><a href='/files/{i}.xlsx'>附件{i}</a></p>" for i in range(80))
    sample = (
        "<html><body><div id='zoom'><p>关于公布有关名单的通知，全文见附件。</p>"
        f"{links}</div></body></html>"
    )
    result = parse_detail(sample, "https://example.gov.cn/a.html")
    assert len(result.attachments) == MAX_ATTACHMENTS
    assert result.attachments[0].url == "https://example.gov.cn/files/0.xlsx"


def test_doc_number_variants_and_sensitive_data() -> None:
    for number in ("辽工信〔2026〕130号", "辽工信[2026]130号", "辽工信（2026）130号"):
        sample = (
            f"<html><meta name='PubDate' content='2026-09-24'><body><div id='zoom'>"
            f"<p>{number}</p><p>这是公开通知的正文，联系方式：13812345678，"
            "身份证号：110101199001011234，银行账号：6222021234567890123。</p>"
            "<p>辽宁省工业和信息化厅</p><p>2026年9月24日</p></div></body></html>"
        )
        result = parse_detail(sample, "https://example.gov.cn/a.html")
        assert result.doc_number == number
        assert "13812345678" not in result.body_text
        assert "110101199001011234" not in result.body_text
        assert "6222021234567890123" not in result.body_text
    assert redact_sensitive("联系人：张三，邮箱：zhang@example.cn") == (
        "联系人：[姓名已脱敏]，邮箱：[邮箱已脱敏]"
    )
    assert "130503670401001" not in redact_sensitive("旧身份证号：130503670401001")
    assert "6222 0212 3456 7890 123" not in redact_sensitive("银行账号：6222 0212 3456 7890 123")


def test_protection_page_is_rejected() -> None:
    result = parse_detail(
        "<html><title>安全验证</title><body><div id='zoom'>请输入验证码</div></body></html>",
        "https://example.gov.cn/a.html",
    )
    assert result.failure_reason == "防护页或验证码"
    assert not result.body_text


def test_density_fallback_ignores_navigation_and_redacts() -> None:
    sample = (
        "<html><body><div class='top-links'><a href='/a'>网站首页栏目导航公告通知</a>"
        "<a href='/b'>政策服务信息公开相关链接</a></div>"
        "<div class='story'><p>各单位应按规定报送申报材料，主管部门将逐项核对材料。</p>"
        "<p>申报截止2026年10月30日，联系人电话13812345678。</p></div>"
        "<div class='related'>相关新闻推荐报道</div></body></html>"
    )
    result = parse_detail(sample, "https://example.gov.cn/a.html")
    assert result.failure_reason is None
    assert result.body_text.startswith("各单位应按规定")
    assert "网站首页" not in result.body_text
    assert "13812345678" not in result.body_text


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("国家税务总局 辽宁省税务局", "国家税务总局辽宁省税务局"),
        ("国家金融监督管理总局 广东监管局", "国家金融监督管理总局广东监管局"),
        ("中国人民银行 江苏省分行", "中国人民银行江苏省分行"),
        ("国家外汇管理局 上海市分局", "国家外汇管理局上海市分局"),
        ("海关总署 沈阳海关", "海关总署沈阳海关"),
        ("辽宁省财政厅 国家税务总局 辽宁省税务局", "辽宁省财政厅、国家税务总局辽宁省税务局"),
    ],
)
def test_issuer_branch_names_are_not_split(line: str, expected: str) -> None:
    assert _split_issuer_line(line) == expected
    sample = (
        "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。"
        "申报单位应当按照规定提交材料。</p>"
        f"<p>{line}</p><p>2026年9月24日</p></div></body></html>"
    )
    assert parse_detail(sample, "https://example.gov.cn/a.html").issuer == expected
