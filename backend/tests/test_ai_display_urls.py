"""AI 结果页显示的原句里带网址：只在显示时换成"（这里隐去了网址）"（D-112，创始人 11/2 同意）。

原句核对（生成时按原文逐字找）、存下的结果、交给模型的文字都不变；"正文变了没有"比的是存下的
段落，也不受影响，旧结果照常显示。"""

from sqlalchemy import select

from tests.test_ai_interpret import (  # noqa: F401 - 夹具
    NOW,
    FakeChat,
    client,
    direct_task,
    item,
    run,
    settings,
)
from youju.ai import content
from youju.api.advisor import _shown_quotes
from youju.models import AdvisorTask, Document, DocumentDetail, Source

URL = "（这里隐去了网址）"
SENTENCE = "请登录火炬中心平台（https://hjrz.chinatorch.org.cn/login）填报。"
BODY = f"申报对象为科技企业。\n{SENTENCE}\n请提交申请书及附件。"


def _doc(session):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="科技厅",
        column_name="政策",
        url="https://official.test/policy/",
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    doc = Document(
        source_id=source.id,
        title="申报通知",
        kind_override="申报通知",
        url="https://official.test/1",
        url_key="official.test/1",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(doc)
    session.flush()
    session.add(DocumentDetail(document_id=doc.id, body_text=BODY, fetched_at=NOW, status="ok"))
    session.commit()
    return doc


def test_result_page_hides_urls_but_stored_result_and_model_text_keep_them(
    client,  # noqa: F811
    session,
    settings,  # noqa: F811
):
    doc = _doc(session)
    task = direct_task(session, doc)
    payload = {"items": [item(SENTENCE, "P002", "方式")], "missing": [], "refusal": None}
    run(session, task, settings, FakeChat(payload))
    assert task.status == "done"
    # 原句核对照原文做：带网址的原句通过了核对，存下的结果带着网址
    assert task.result_json["items"][0]["text"] == SENTENCE
    body = client.get(f"/api/v1/advisor/tasks/{task.id}").json()
    shown = body["result"]["items"][0]
    assert shown["text"] == f"请登录火炬中心平台{URL}填报。"
    assert shown["refs"][0]["quote"] == f"请登录火炬中心平台{URL}填报。"
    assert "http" not in str(body["result"]) and not body.get("error")
    # 存下的结果、交给模型的文字没变
    session.expire_all()
    stored = session.scalar(select(AdvisorTask).where(AdvisorTask.id == task.id))
    assert stored.result_json["items"][0]["refs"][0]["quote"] == SENTENCE
    assert SENTENCE in content.load(session, doc.id).texts.sources[0].text


def test_shown_quotes_returns_a_copy():
    stored = {
        "items": [
            {
                "type": "方式",
                "text": "见 https://x.gov.cn。",
                "refs": [{"pid": "P1", "quote": "见 https://x.gov.cn。"}],
            },
            "不是字典的条目原样",
        ],
        "verified": [{"type": "时间", "quote": "11月20日前登录www.x.gov.cn报送"}, None],
        "rows": [{"text": "不在这个技能的字段里"}],
    }
    out = _shown_quotes(stored, "items")
    assert out["items"][0]["text"] == f"见 {URL}。"
    assert out["items"][0]["refs"][0] == {"pid": "P1", "quote": f"见 {URL}。"}
    assert out["items"][1] == "不是字典的条目原样"
    assert out["verified"][0]["quote"] == f"11月20日前登录{URL}报送" and out["verified"][1] is None
    assert out["rows"] == stored["rows"]
    # 原来的结果一个字没动
    assert stored["items"][0]["text"] == "见 https://x.gov.cn。"
    assert stored["items"][0]["refs"][0]["quote"] == "见 https://x.gov.cn。"
    assert stored["verified"][0]["quote"] == "11月20日前登录www.x.gov.cn报送"
    # 条款对照的条目在 rows 里
    clause = _shown_quotes({"rows": [{"text": "（http://a.gov.cn）", "refs": []}]}, "rows")
    assert clause["rows"][0]["text"] == URL
