from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document as DocxDocument

from youju.crawler.fetcher import FetchResult
from youju.lists.parser import parse_attachment, parse_html_tables, parse_table_rows
from youju.lists.rules import (
    candidate_key,
    classify_title,
    is_person_query,
    normalize_credit_code,
    normalize_name,
    valid_org_name,
)
from youju.lists.service import parse_lists
from youju.models import Document, DocumentAttachment, DocumentDetail, ListEntry, Source

FIXTURES = Path(__file__).parent / "fixtures" / "lists"


@pytest.mark.parametrize(
    ("filename", "count", "first"),
    [
        ("panjin_table.html", 30, "智源（辽宁）碳科技有限公司"),
        ("heilongjiang_30.xlsx", 30, "哈尔滨力强药业有限责任公司"),
        ("heilongjiang_30.xls", 30, "哈尔滨力强药业有限责任公司"),
        ("shanghai_page1.pdf", 27, "中卉泽科技（上海）有限公司"),
    ],
)
def test_real_government_list_fixtures(filename, count, first) -> None:
    path = FIXTURES / filename
    assert path.stat().st_size <= 300 * 1024
    entries = (
        parse_html_tables(path.read_text())
        if path.suffix == ".html"
        else parse_attachment(path.read_bytes(), path.suffix)
    )
    assert len(entries) == count
    assert entries[0].name == first
    assert entries[0].sequence == "1"
    if filename.endswith((".xlsx", ".xls")):
        assert entries[0].region == "黑龙江省/哈尔滨市/依兰县"
    assert all(valid_org_name(entry.name, entry.credit_code) for entry in entries)


def test_docx_and_personal_columns_are_not_saved() -> None:
    doc = DocxDocument()
    table = doc.add_table(rows=3, cols=5)
    values = [
        ["序号", "企业名称", "联系人", "手机号", "所在地区"],
        ["1", "北京星河科技有限公司", "张三", "13800138000", "北京市"],
        ["2", "李四", "王五", "13900139000", "北京市"],
    ]
    for row, values_row in zip(table.rows, values, strict=True):
        for cell, value in zip(row.cells, values_row, strict=True):
            cell.text = value
    stream = BytesIO()
    doc.save(stream)
    entries = parse_attachment(stream.getvalue(), "docx")
    assert [(entry.name, entry.region) for entry in entries] == [("北京星河科技有限公司", "北京市")]
    assert "张三" not in str(entries)
    assert "13800138000" not in str(entries)
    assert parse_table_rows([["姓名", "电话"], ["张三", "13800138000"]]) == []


def test_names_codes_and_title_rules() -> None:
    assert normalize_name("ＡＢＣ（上海） 科技有限 公司") == "ABC(上海)科技有限公司"
    assert candidate_key("北京星河（上海）有限公司") == candidate_key("北京星河有限公司")
    assert is_person_query("张 三")
    assert not is_person_query("北京研究院")
    assert not valid_org_name("李四")
    assert not valid_org_name("老茶厂、铸铁锅等设备和档案资料。")
    assert valid_org_name("洞口县华宇机械制造厂（普通合伙）")
    assert normalize_credit_code("９１３１０１１７５８８６５７２５６ｃ") == "91310117588657256C"
    assert normalize_credit_code("123456789012345679") is None
    assert classify_title("关于2026年第4批拟入库科技型中小企业名单的公示").status == "公示"
    info = classify_title("关于公布2026年第4批专精特新中小企业名单的公告")
    assert (info.category, info.status, info.batch) == ("专精特新中小企业", "正式公布", "第4批")
    assert classify_title("第六批专精特新“小巨人”企业名单公示").category == '专精特新"小巨人"'
    assert classify_title("高新技术企业名单").status == "未确定"
    assert classify_title("关于2026年高新技术企业认定结果的通知").is_list
    assert classify_title("关于开展高新技术企业认定申报工作的通知").is_list is False
    assert classify_title("关于高企认定评审专家培训的预通知").is_list is False
    assert classify_title("采购结果公示").is_list is False
    assert classify_title("取消有关企业专精特新中小企业认定的公告").is_list is False
    assert classify_title("关于公布实验动物行政许可注销名单的通知").is_list is False
    assert classify_title("关于拟对高新技术企业给予奖励的公示").is_list is False


def test_unsupported_and_scanned_file() -> None:
    with pytest.raises(ValueError, match="暂不支持"):
        parse_attachment(b"old doc", "doc")
    with pytest.raises(ValueError, match="不是 PDF"):
        parse_attachment(b"not a pdf", "pdf")


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self.responses[url]


def test_parse_lists_backfills_detail_and_is_idempotent(session) -> None:
    source = Source(
        level="省级",
        province="黑龙江",
        dept="科技",
        dept_full_name="黑龙江省科学技术厅",
        column_name="公示公告",
        url="https://kjt.hlj.gov.cn/notices/",
        fetch_mode="static",
        enabled=True,
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    now = datetime.now(UTC)
    url = "https://kjt.hlj.gov.cn/notice.html"
    attachment = "https://kjt.hlj.gov.cn/list.xlsx"
    document = Document(
        source_id=source.id,
        url=url,
        url_key="kjt.hlj.gov.cn/notice.html",
        title="关于黑龙江省2026年第2批拟入库科技型中小企业名单的公示",
        publish_date=date(2026, 8, 6),
        first_seen_at=now,
        last_seen_at=now,
        is_baseline=False,
    )
    session.add(document)
    session.commit()
    page = (
        "<html><body><div id='zoom'><p>现将拟入库科技型中小企业名单予以公示。"
        "有关单位可查看附件名单。</p><a href='/list.xlsx'>附件：名单.xlsx</a>"
        "</div></body></html>"
    )
    fake = FakeFetcher(
        {
            url: FetchResult(url, "ok", final_url=url, http_status=200, text=page),
            attachment: FetchResult(
                attachment,
                "ok",
                final_url=attachment,
                http_status=200,
                content=(FIXTURES / "heilongjiang_30.xlsx").read_bytes(),
            ),
        }
    )
    result = parse_lists(session, fetcher=fake)
    assert (
        result.documents,
        result.parsed_documents,
        result.names,
        result.attachments_fetched,
    ) == (1, 1, 30, 1)
    assert result.categories == {"科技型中小企业": 30}
    assert result.samples[0].status == "公示"
    assert len(fake.calls) == 3  # 补详情、读 HTML 表格、读附件
    session.add(
        ListEntry(
            document_id=document.id,
            source_url=attachment,
            name="老茶厂、铸铁锅等设备和档案资料。",
            normalized_name="老茶厂、铸铁锅等设备和档案资料。",
            category="其他",
            status="未确定",
        )
    )
    session.commit()
    assert parse_lists(session, fetcher=fake).names == 30
    assert len(fake.calls) == 3
    assert session.query(ListEntry).count() == 30
    assert session.get(DocumentDetail, document.id).attachments[0].url == attachment
    document.title = "关于开展2026年科技型中小企业申报的通知"
    session.commit()
    assert parse_lists(session, fetcher=fake).documents == 0
    assert session.query(ListEntry).count() == 0


def test_attachment_limit(session) -> None:
    source = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科学技术委员会",
        column_name="通知公告",
        url="https://kw.beijing.gov.cn/list/",
        fetch_mode="static",
        enabled=True,
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    now = datetime.now(UTC)
    doc = Document(
        source_id=source.id,
        url="https://kw.beijing.gov.cn/notice.html",
        url_key="kw.beijing.gov.cn/notice.html",
        title="2026年高新技术企业名单公告",
        publish_date=date(2026, 8, 1),
        first_seen_at=now,
        last_seen_at=now,
        is_baseline=False,
    )
    session.add(doc)
    session.flush()
    links = [f"https://kw.beijing.gov.cn/{i}.xlsx" for i in range(3)]
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text="名单附件",
            fetched_at=now,
            status="ok",
            final_url=doc.url,
            attachments=[
                DocumentAttachment(position=i, name=f"名单{i}", url=link, extension="xlsx")
                for i, link in enumerate(links)
            ],
        )
    )
    session.commit()
    fake = FakeFetcher(
        {
            doc.url: FetchResult(
                doc.url, "ok", final_url=doc.url, text="<html><body></body></html>"
            ),
            **{
                link: FetchResult(
                    link,
                    "ok",
                    final_url=link,
                    content=(FIXTURES / "heilongjiang_30.xlsx").read_bytes(),
                )
                for link in links
            },
        }
    )
    report = parse_lists(session, attachment_limit=2, fetcher=fake)
    assert report.attachments_fetched == 2
    assert report.attachments_deferred == 1
    assert report.failures == {}
    assert links[2] not in fake.calls
    fake.responses[links[2]].content = b"damaged spreadsheet"
    retry = parse_lists(session, attachment_limit=1, fetcher=fake)
    assert retry.attachments_fetched == 1
    assert retry.failures["解析失败：BadZipFile"] == 1
    assert retry.parsed_documents == 1


def test_direct_old_doc_is_unsupported_without_fetch(session) -> None:
    source = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科学技术委员会",
        column_name="通知公告",
        url="https://kw.beijing.gov.cn/list/",
        fetch_mode="static",
        enabled=True,
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    now = datetime.now(UTC)
    session.add(
        Document(
            source_id=source.id,
            url="https://kw.beijing.gov.cn/old-list.doc",
            url_key="kw.beijing.gov.cn/old-list.doc",
            title="2026年高新技术企业名单公告",
            publish_date=date(2026, 8, 1),
            first_seen_at=now,
            last_seen_at=now,
            is_baseline=False,
        )
    )
    session.commit()
    fake = FakeFetcher({})
    report = parse_lists(session, fetcher=fake)
    assert report.failures == {"暂不支持旧版或专有格式": 1}
    assert fake.calls == []
