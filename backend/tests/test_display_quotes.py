"""返回给页面的原文片段走同一个显示层（11/2 F11，D-112）：截止的"原文摘录"原句、支持方式和申报
对象、申报条件的原句，列表行、截止页、日历、站内查看结果里带的原句，网址都换成"（这里隐去了网址）"。

只改返回的那份：库里存的原句、核对台看的、交给模型和核对 AI 原句用的都不变。"""

from datetime import date

from sqlalchemy import select

from tests.test_api_v1 import client, seeded  # noqa: F401 - 夹具
from youju.api import v1
from youju.models import DocumentDeadline, DocumentDetail

URL = "（这里隐去了网址）"
QUOTE = "企业申报截止时间为10月18日，请登录https://kjt.example.gov.cn/apply填报"
SHOWN = f"企业申报截止时间为10月18日，请登录{URL}填报"


def test_helpers_mask_only_the_returned_copy():
    due = {"kind": "企业申报截止", "source": "verified", "quote": QUOTE}
    assert v1.shown_due(due) == {**due, "quote": SHOWN} and due["quote"] == QUOTE
    assert v1.shown_due(None) is None and v1.shown_due({"quote": None}) == {"quote": None}
    assert v1.shown_quote(None) is None and v1.shown_quote(QUOTE) == SHOWN
    fields = {
        "support": ["资金支持"],
        "support_evidence": {"资金支持": "详见 www.example.gov.cn。"},
        "audience": {"text": "科技型企业", "evidence": "申报对象见 http://a.gov.cn/b。"},
    }
    shown = v1._shown_fields(fields)
    assert shown["support_evidence"] == {"资金支持": f"详见 {URL}。"}
    assert shown["audience"] == {"text": "科技型企业", "evidence": f"申报对象见 {URL}。"}
    assert shown["support"] == ["资金支持"]
    # _approved_fields 的结果没被改（AI 解读的"已核对"一栏和版本指纹也用它）
    assert fields["support_evidence"]["资金支持"] == "详见 www.example.gov.cn。"
    assert v1._shown_fields({}) == {}
    conditions = {"items": [{"category": "条件", "quote": "注册地在（https://x.gov.cn）所列园区"}]}
    assert v1._shown_conditions(conditions)["items"][0]["quote"] == f"注册地在{URL}所列园区"
    assert conditions["items"][0]["quote"] == "注册地在（https://x.gov.cn）所列园区"
    assert v1._shown_conditions(None) is None


def _quote_with_url(session, seeded):  # noqa: F811
    row = session.scalar(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id == seeded["apply"], DocumentDeadline.status == "approved"
        )
    )
    row.evidence = QUOTE
    session.commit()
    v1._EXCERPT_CACHE.clear()
    v1._REPORT_CACHE.clear()
    return row


def test_detail_deadline_fields_and_conditions(client, session, seeded, monkeypatch):  # noqa: F811
    _quote_with_url(session, seeded)
    monkeypatch.setattr(
        v1,
        "_approved_fields",
        lambda s, ids: {
            ids[0]: {
                "support": ["资金支持"],
                "support_evidence": {"资金支持": "资金支持见 https://czt.example.gov.cn。"},
            }
        },
    )
    monkeypatch.setattr(
        v1,
        "public_conditions",
        lambda s, doc_id: {"items": [{"category": "条件", "quote": "登记见www.x.gov.cn。"}]},
    )
    detail = client.get(f"/api/v1/documents/{seeded['apply']}").json()
    assert detail["excerpt"]["text"] == SHOWN
    assert SHOWN in [d["quote"] for d in detail["deadlines"] if d["source"] == "verified"]
    assert detail["fields"]["support_evidence"] == {"资金支持": f"资金支持见 {URL}。"}
    assert detail["apply_conditions"]["items"][0]["quote"] == f"登记见{URL}。"
    assert "http" not in str({k: detail[k] for k in ("excerpt", "deadlines", "fields")})


def test_lists_report_calendar_and_preview(client, session, seeded):  # noqa: F811
    row = _quote_with_url(session, seeded)
    report = client.get("/api/v1/reports/deadlines", params={"days": 30, "excerpts": 1}).json()
    item = next(i for i in report["items"] if i["id"] == str(seeded["apply"]))
    assert item["quote"] == SHOWN and item["due"]["quote"] == SHOWN
    # 小程序的截止日历带 excerpts=1（不带的那一版只给日期，没有原句）
    calendar = client.get("/api/v1/calendar", params={"excerpts": 1}).json()
    shown = [i["quote"] for i in calendar["items"] if i["docId"] == str(seeded["apply"])]
    assert SHOWN in shown and not any("http" in q for q in shown)
    search = client.get(
        "/api/v1/search", params={"kind": "申报通知", "time": "不限", "excerpts": 1}
    ).json()
    hit = next(i for i in search["items"] if i["id"] == str(seeded["apply"]))
    assert hit["due"]["quote"] == SHOWN
    home = client.get("/api/v1/home", params={"excerpts": 1}).json()
    assert all("http" not in str(i) for i in home.get("dueSoon", []))
    preview = client.post(
        "/api/v1/exports/preview", json={"ids": [str(seeded["apply"])], "excerpts": 1}
    ).json()
    # 几个截止的原句用换行连在一起
    assert SHOWN in preview["items"][0]["deadline_quote"].split("\n")
    assert "http" not in preview["items"][0]["deadline_quote"]
    # 库里存的原句没动
    session.expire_all()
    assert session.get(DocumentDeadline, row.id).evidence == QUOTE


def test_machine_excerpt_quote_in_list_rows_is_masked(client, session, seeded):  # noqa: F811
    for row in session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.document_id == seeded["apply"])
    ):
        row.approved_date = date(2026, 9, 25)
    detail = session.scalar(
        select(DocumentDetail).where(DocumentDetail.document_id == seeded["apply"])
    )
    quote = "材料报送截止：2026年10月10日，网址http://sb.example.gov.cn。"
    detail.body_text = quote
    session.add(
        DocumentDeadline(
            document_id=seeded["apply"],
            fingerprint="future-excerpt-url",
            kind="材料报送截止",
            candidate_date=date(2026, 10, 10),
            evidence=quote,
            confidence="高",
            status="pending",
        )
    )
    session.commit()
    v1._EXCERPT_CACHE.clear()
    v1._REPORT_CACHE.clear()
    report = client.get("/api/v1/reports/deadlines", params={"days": 30, "excerpts": 1}).json()
    item = next(i for i in report["items"] if i["id"] == str(seeded["apply"]))
    assert item["source"] == "excerpt"
    assert item["quote"] == f"材料报送截止：2026年10月10日，网址{URL}。"
