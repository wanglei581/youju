"""名单解析里的 AI 认列（D-023）：规则按表头找不到名称列时才请 AI，抽出的名称照样过机构名称规则。"""

import json
from datetime import UTC, date, datetime

import httpx

from youju.ai.client import ChatClient
from youju.crawler.fetcher import FetchResult
from youju.lists.parser import parse_table_rows
from youju.lists.service import parse_lists
from youju.models import Document, ListEntry, Source

ROWS = [
    ["序", "单位", "所在区"],
    ["1", "烟台示例科技有限公司", "芝罘区"],
    ["2", "威海示例电子股份有限公司", "环翠区"],
    ["3", "潍坊示例研究院", "奎文区"],
]


def test_picker_is_only_used_when_rules_find_nothing() -> None:
    calls = []

    def pick(headers, rows):
        calls.append(headers)
        return 1

    names = [entry.name for entry in parse_table_rows(ROWS, pick)]
    assert names == ["烟台示例科技有限公司", "威海示例电子股份有限公司", "潍坊示例研究院"]
    assert calls == [["序", "单位", "所在区"]]

    # 有"企业名称"表头时规则就能抽，不请 AI
    calls.clear()
    ruled = [["序号", "企业名称"], ["1", "烟台示例科技有限公司"]]
    assert [e.name for e in parse_table_rows(ruled, pick)] == ["烟台示例科技有限公司"]
    assert calls == []


def test_picked_column_still_has_to_pass_org_name_rules() -> None:
    # AI 选错成地区列：地区名过不了机构名称规则，一条也不收
    assert parse_table_rows(ROWS, lambda headers, rows: 2) == []
    assert parse_table_rows(ROWS, lambda headers, rows: None) == []
    assert parse_table_rows(ROWS, None) == []


def fake_client(column):
    def handler(request):
        content = json.dumps({"column": column, "reason": "单位名称"}, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return ChatClient(
        "https://example.test",
        "key",
        "qwen-flash",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses

    def fetch(self, url):
        return self.responses[url]


def add_list_document(session):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知公告",
        url="https://kjt.shandong.gov.cn/tzgg/",
        enabled=True,
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    now = datetime.now(UTC)
    url = "https://kjt.shandong.gov.cn/tzgg/list-1.html"
    doc = Document(
        source_id=source.id,
        url=url,
        url_key="kjt.shandong.gov.cn/tzgg/list-1.html",
        title="关于2026年拟认定高新技术企业名单的公示",
        publish_date=date(2026, 9, 1),
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(doc)
    session.commit()
    rows = "".join(f"<tr>{''.join(f'<td>{c}</td>' for c in row)}</tr>" for row in ROWS)
    page = (
        "<html><body><div id='zoom'><p>现将拟认定企业名单公示如下，公示期为五个工作日。</p>"
        f"<table>{rows}</table></div></body></html>"
    )
    fetcher = FakeFetcher({url: FetchResult(url, "ok", final_url=url, http_status=200, text=page)})
    return doc, fetcher


def test_parse_lists_uses_ai_when_configured(session) -> None:
    doc, fetcher = add_list_document(session)
    report = parse_lists(session, fetcher=fetcher, ai_client=fake_client(1))
    names = sorted(e.name for e in session.query(ListEntry).filter_by(document_id=doc.id))
    assert names == sorted(["烟台示例科技有限公司", "威海示例电子股份有限公司", "潍坊示例研究院"])
    assert (report.ai_tables_tried, report.ai_tables_found) == (1, 1)


def test_parse_lists_without_ai_keeps_rules_only(session) -> None:
    doc, fetcher = add_list_document(session)
    report = parse_lists(session, fetcher=fetcher)
    assert session.query(ListEntry).filter_by(document_id=doc.id).count() == 0
    assert (report.ai_tables_tried, report.ai_tables_found) == (0, 0)
