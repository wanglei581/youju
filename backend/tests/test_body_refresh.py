"""一次性重取正文（10/22 湖北科技厅等收录的正文混进网页上的导航、按钮、弹窗等字样）。"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.conftest import read_fixture_html
from youju import cli
from youju.ai import interpret
from youju.crawler import detail_service
from youju.crawler.body_refresh import Impact, refresh_bodies, survey, trimmed_lines
from youju.crawler.detail import parse_detail, redact_sensitive
from youju.crawler.fetcher import FetchResult
from youju.fields import body_fingerprint
from youju.models import (
    AdvisorTask,
    AICheck,
    ConditionList,
    Document,
    DocumentAttachment,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    RepostGroup,
    RepostGroupMember,
    Source,
    User,
)

HUBEI = "kjt.hubei.gov.cn"
HUBEI_URL = "https://kjt.hubei.gov.cn/kjdt/tzgg/202610/t20261020_6035881.shtml"
HUBEI_TITLE = "省科技厅关于组织申报企业自主研发和委托研发项目纳入省级科技计划项目的通知"
FETCHED_AT = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
CLEAN = parse_detail(read_fixture_html("detail_hubei_kjt_70955"), HUBEI_URL).body_text
# 改规则以前收录的正文（10/22 线上 70955 就是这样）：开头是导航、标题、发布时间和发文机关，
# 结尾是附件标题、公众号、侧栏标题和离站提示弹窗
HEAD = ["当前位置：首页>科技动态>通知公告", HUBEI_TITLE, "2026-10-20 10:04 |", "湖北省科学技术厅"]
TAIL = [
    "附件:",
    "欢迎关注湖北科技公众号",
    "相关文档",
    "关联链接",
    "×",
    "您访问的链接即将离开“”门户网站 是否继续？",
]
OLD = "\n".join([*HEAD, CLEAN, *TAIL])


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        response = self.responses[url]
        return response(url) if callable(response) else response


def _page(url: str, html: str) -> FetchResult:
    return FetchResult(url, "ok", final_url=url, http_status=200, text=html)


def _source(session, url="https://kjt.hubei.gov.cn/kjdt/tzgg/", rights_level=1) -> Source:
    source = Source(
        level="省级",
        province="湖北",
        dept="科技",
        dept_full_name="湖北省科学技术厅",
        column_name="通知公告",
        url=url,
        fetch_mode="static",
        enabled=True,
        parser_config={},
        rights_level=rights_level,
    )
    session.add(source)
    session.flush()
    return source


def _document(session, source, url, body, *, status="ok", final_url=None) -> Document:
    now = datetime.now(UTC)
    doc = Document(
        source_id=source.id,
        url=url,
        url_key=url.split("://", 1)[1],
        title=HUBEI_TITLE,
        publish_date=date(2026, 10, 20),
        first_seen_at=now,
        last_seen_at=now,
        is_baseline=False,
        status="fetched",
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text=body,
            issuer="旧发文机关",
            publish_date=date(2026, 10, 20),
            fetched_at=FETCHED_AT,
            http_status=200,
            status=status,
            final_url=final_url,
            attachments=[
                DocumentAttachment(position=0, name="旧附件", url=f"{url}.doc", extension="doc")
            ],
        )
    )
    session.flush()
    return doc


def _stored(session, document_id) -> DocumentDetail:
    session.expire_all()
    return session.get(DocumentDetail, document_id)


def _reviewed_results(session, doc: Document) -> None:
    """正文换了以后要重新处理的已有结果，各一份；另加不算的（没核对的、别的正文版本的）。"""
    digest = body_fingerprint(OLD)
    stamp = datetime.now(UTC)
    session.add_all(
        [
            DocumentField(
                document_id=doc.id,
                field="support",
                value="资金支持",
                evidence="项目经费",
                evidence_offset=0,
                body_hash=digest,
                status="approved",
                reviewed_by="核对员",
                reviewed_at=stamp,
            ),
            DocumentField(
                document_id=doc.id,
                field="support",
                value="项目立项",
                evidence="纳入省级科技计划",
                evidence_offset=0,
                body_hash=digest,
                status="pending",
            ),
            DocumentField(
                document_id=doc.id,
                field="support",
                value="资格认定",
                evidence="旧版正文",
                evidence_offset=0,
                body_hash="0" * 64,
                status="approved",
                reviewed_by="核对员",
                reviewed_at=stamp,
            ),
            ConditionList(
                document_id=doc.id,
                body_hash=digest,
                status="confirmed",
                model="m",
                prompt_version="v1",
                extracted_at=stamp,
                confirmed_by="核对员",
                confirmed_at=stamp,
            ),
            DocumentDeadline(
                document_id=doc.id,
                fingerprint="a" * 64,
                kind="企业申报截止",
                candidate_date=date(2026, 11, 20),
                evidence="申报单位网上申报截止时间为2026年11月20日17时",
                confidence="high",
                status="approved",
                approved_date=date(2026, 11, 20),
                reviewed_by="核对员",
                reviewed_at=stamp,
            ),
            # 原句在去掉的网页字样里（不会有这样的核对，用来数"没有了"）
            DocumentDeadline(
                document_id=doc.id,
                fingerprint="b" * 64,
                kind="材料报送截止",
                candidate_date=date(2026, 10, 20),
                evidence="2026-10-20 10:04 |",
                confidence="low",
                status="approved",
                approved_date=date(2026, 10, 20),
                reviewed_by="核对员",
                reviewed_at=stamp,
            ),
            # 原句本来就不在旧正文里（如截断过）：和这次换正文无关，不算
            DocumentDeadline(
                document_id=doc.id,
                fingerprint="c" * 64,
                kind="推荐或审核截止",
                candidate_date=date(2026, 11, 23),
                evidence="推荐单位网上审核推荐截止时间为2026年11月23日17时…",
                confidence="high",
                status="approved",
                approved_date=date(2026, 11, 23),
                reviewed_by="核对员",
                reviewed_at=stamp,
            ),
        ]
    )
    shown = {
        "body_hash": interpret.digest(OLD),
        "paragraphs": interpret.split_paragraphs(redact_sensitive(OLD)),
    }
    earlier = {
        "body_hash": interpret.digest("早先"),
        "paragraphs": interpret.split_paragraphs("早先"),
    }
    redacted_before = {**shown, "paragraphs": interpret.split_paragraphs("脱敏规则改过以前的段落")}
    group = RepostGroup(strength="strong", topic="企业自主研发项目纳入省级科技计划")
    user = User(openid="o-refetch")
    session.add_all([group, user])
    session.flush()
    session.add(
        RepostGroupMember(
            document_id=doc.id, group_id=group.id, confirmed_by="核对员", confirmed_at=stamp
        )
    )
    session.add_all(
        [
            AdvisorTask(
                user_id=user.id,
                kind=kind,
                status=status,
                input_json={"doc_id": doc.id},
                snapshot={"doc_id": doc.id, "document_ids": [doc.id, doc.id + 1], **body},
                result_json={"items": []} if status == "done" else None,
                row_count=0,
                idempotency_key=f"task-{number}",
                expires_at=stamp + timedelta(days=days),
            )
            # 只数还在显示的：完成的、没过期的，读的是这份正文（body_id；旧任务没记的按它自己的文件），
            # 读取端判断正文没变（原文指纹、脱敏后的段落都对得上；没记这两项的旧任务也算）。读的是同组
            # 另一份的正文、正文早就变过、脱敏规则改过段落对不上（这两种已显示"通知正文已变化"）、
            # 过期的、没完成的不算
            for number, (kind, status, days, body) in enumerate(
                (
                    ("interpret", "done", 30, {}),
                    ("clause", "done", 30, {"body_id": doc.id}),
                    ("interpret", "done", 30, {"body_id": doc.id, **shown}),
                    ("clause", "done", 30, {"body_id": doc.id, **earlier}),
                    ("interpret", "done", 30, {"body_id": doc.id, **redacted_before}),
                    ("interpret", "done", 30, {"body_id": doc.id + 1}),
                    ("interpret", "done", -1, {}),
                    ("clause", "failed", 30, {}),
                )
            )
        ]
    )


def test_trimmed_lines() -> None:
    assert trimmed_lines("a\nb\nc\nd", "b\nc") == (["a"], ["d"])
    assert trimmed_lines("a\nb\nc", "a\nb") == ([], ["c"])
    assert trimmed_lines("a\nb", "a\nb") is None  # 一行没少
    assert trimmed_lines("a\nb\nc", "a\nc") is None  # 少的是中间的行
    assert trimmed_lines("a\nb\nc", "b\nx") is None
    assert trimmed_lines("a\nb", "") is None


def test_refresh_previews_then_replaces_only_the_body(session) -> None:
    doc = _document(session, _source(session), HUBEI_URL, OLD)
    _reviewed_results(session, doc)
    session.commit()
    fake = FakeFetcher({HUBEI_URL: _page(HUBEI_URL, read_fixture_html("detail_hubei_kjt_70955"))})

    preview = refresh_bodies(session, hosts=[HUBEI], fetcher=fake)
    assert [(r.document_id, r.result, r.head, r.tail, r.written) for r in preview] == [
        (doc.id, "trimmed", HEAD, TAIL, False)
    ]
    assert preview[0].impact == Impact(
        approved_fields=1, confirmed_conditions=1, confirmed_reposts=1, ai_results=3, lost_quotes=1
    )
    detail = _stored(session, doc.id)
    assert (detail.body_text, detail.fetched_at) == (OLD, FETCHED_AT)  # 预览不写库

    before = datetime.now(UTC)
    applied = refresh_bodies(session, hosts=[HUBEI], apply=True, fetcher=fake)
    assert [(r.result, r.written, r.reason) for r in applied] == [("trimmed", True, None)]
    detail = _stored(session, doc.id)
    assert detail.body_text == CLEAN
    # 抓取时间记写入那一刻（"能不能 AI 解读"每 10 分钟按它重算最近 30 分钟的）
    assert detail.fetched_at.replace(tzinfo=UTC) >= before - timedelta(seconds=1)
    # 只换正文和抓取时间：发文机关、日期、附件、抓取状态都不动
    assert (detail.issuer, detail.publish_date, detail.status) == (
        "旧发文机关",
        date(2026, 10, 20),
        "ok",
    )
    assert [a.name for a in detail.attachments] == ["旧附件"]

    again = refresh_bodies(session, hosts=[HUBEI], apply=True, fetcher=fake)
    assert [(r.result, r.written) for r in again] == [("unchanged", False)]


def test_other_changes_are_written_only_when_asked(session) -> None:
    """不只是去掉开头结尾的（青海工信厅以前收录的只有导航、标题和上一篇下一篇）：预览列出前后开头，
    加 include_changed 才换；换的时候删掉按旧正文做的后台 AI 复查（类型、截止），下一轮重查。"""
    url = "https://gxt.qinghai.gov.cn/2100879587404161025.html"
    old = "当前位置: 首页\n- 新闻资讯\n- 通知公告\n上一篇：\n没有更多了\n下一篇：\n没有更多了"
    doc = _document(session, _source(session, url="https://gxt.qinghai.gov.cn/"), url, old)
    stamp = datetime.now(UTC)
    session.add_all(
        AICheck(document_id=doc.id, task=task, model="m", result=[], checked_at=stamp)
        for task in ("kind", "deadlines", "conditions")
    )
    session.commit()
    fake = FakeFetcher({url: _page(url, read_fixture_html("detail_qinghai_gxt"))})

    applied = refresh_bodies(session, hosts=["gxt.qinghai.gov.cn"], apply=True, fetcher=fake)
    assert [(r.result, r.written, r.old_start) for r in applied] == [("changed", False, old[:60])]
    assert applied[0].new_start.startswith("根据《青海省智能工厂认定管理办法》")
    assert applied[0].impact == Impact(ai_checks=2)
    assert _stored(session, doc.id).body_text == old

    applied = refresh_bodies(
        session, hosts=["gxt.qinghai.gov.cn"], apply=True, include_changed=True, fetcher=fake
    )
    assert [(r.result, r.written) for r in applied] == [("changed", True)]
    assert _stored(session, doc.id).body_text.startswith("根据《青海省智能工厂认定管理办法》")
    # 类型、截止两项复查删掉（下一轮重查）；申报条件按正文指纹自己会重抽，记录不动
    assert session.scalars(select(AICheck.task).where(AICheck.document_id == doc.id)).all() == [
        "conditions"
    ]


def test_trimmed_only_when_removed_lines_are_still_on_the_page(session) -> None:
    """只是少了开头结尾几行、但少的那几行原网页上已经没有了（原网页改过、删了内容）：不算"只去掉网页
    上的字"，归其他变化，要人看过加 include_changed 才换。"""
    edited = OLD + "\n八、其他\n本通知自印发之日起施行。"
    doc = _document(session, _source(session), HUBEI_URL, edited)
    session.commit()
    fake = FakeFetcher({HUBEI_URL: _page(HUBEI_URL, read_fixture_html("detail_hubei_kjt_70955"))})

    applied = refresh_bodies(session, hosts=[HUBEI], apply=True, fetcher=fake)
    assert [(r.result, r.written, r.reason) for r in applied] == [
        ("changed", False, "去掉的开头结尾有的已不在原网页上（原网页可能改过），要人看")
    ]
    assert applied[0].tail[-2:] == ["八、其他", "本通知自印发之日起施行。"]
    assert _stored(session, doc.id).body_text == edited
    # 删掉的条款还留在页面的脚本里，也不算还在网页上
    leftover = "<script>var old = '八、其他 本通知自印发之日起施行。';</script>"
    page = read_fixture_html("detail_hubei_kjt_70955").replace("</body>", f"{leftover}</body>")
    fake = FakeFetcher({HUBEI_URL: _page(HUBEI_URL, page)})
    assert [r.result for r in refresh_bodies(session, hosts=[HUBEI], fetcher=fake)] == ["changed"]


@pytest.mark.parametrize(
    ("fixture", "url", "extra"),
    [
        # 四川科技厅：正文区里的二维码块（qr_container）
        (
            "detail_sichuan_kjt",
            "https://kjt.sc.gov.cn/kjt/gstz/2026/9/18/ae78e2444766482e91538646341f90c9.shtml",
            ["扫一扫在手机打开当前页"],
        ),
        # 上海经信委：正文区末尾的按钮块（_SITE_JUNK）
        (
            "detail_shanghai_sheitc",
            "https://sheitc.sh.gov.cn/gg/20260914/b3cb3b63a51b423c8d303b1725299e78.html",
            ["[返回]", "【市民留言】"],
        ),
        # 山东工信厅：文章头里行内样式隐藏的"信息来源"（正文提取不分隐藏不隐藏，以前收进了正文）
        (
            "detail_shandong_gxt",
            "http://gxt.shandong.gov.cn/art/2026/9/28/art_15201_10366576.html",
            ["信息来源：山东省工业和信息化厅"],
        ),
    ],
)
def test_page_chrome_counts_as_page_text(session, fixture, url, extra) -> None:
    """以前收进了正文的网页上的字照常换：正文区里按同样规则不算正文的块（二维码、按钮），
    正文区外隐藏的页头字。"""
    page = read_fixture_html(fixture)
    clean = parse_detail(page, url).body_text
    host = url.split("/")[2]
    doc = _document(
        session, _source(session, url=f"https://{host}/list/"), url, "\n".join([clean, *extra])
    )
    session.commit()
    fake = FakeFetcher({url: _page(url, page)})
    applied = refresh_bodies(session, hosts=[host], apply=True, fetcher=fake)
    assert [(r.result, r.tail, r.written) for r in applied] == [("trimmed", extra, True)]
    assert _stored(session, doc.id).body_text == clean


def test_failed_or_empty_refetch_keeps_the_body(session) -> None:
    source = _source(session)
    gone_url = "https://kjt.hubei.gov.cn/kjdt/tzgg/1.shtml"
    empty_url = "https://kjt.hubei.gov.cn/kjdt/tzgg/2.shtml"
    gone = _document(session, source, gone_url, OLD)
    empty = _document(session, source, empty_url, OLD)
    session.commit()
    # 页面上只剩两个附件链接、没有正文：解析成功但正文为空
    files_only = (
        "<html><body><div id='zoom'><p><a href='/a.doc'>附件1.doc</a></p>"
        "<p><a href='/b.xls'>附件2.xls</a></p></div></body></html>"
    )
    fake = FakeFetcher(
        {
            gone_url: FetchResult(gone_url, "http_error", http_status=404, error="HTTP 404"),
            empty_url: _page(empty_url, files_only),
        }
    )
    results = refresh_bodies(session, hosts=[HUBEI], apply=True, include_changed=True, fetcher=fake)
    assert [(r.document_id, r.result, r.reason, r.written) for r in results] == [
        (gone.id, "failed", "HTTP 404", False),
        (empty.id, "empty", "新正文为空，不换", False),
    ]
    for doc in (gone, empty):
        detail = _stored(session, doc.id)
        assert (detail.body_text, detail.fetched_at, detail.failure_reason) == (
            OLD,
            FETCHED_AT,
            None,
        )


def test_scope_by_exact_host_or_source(session) -> None:
    """按跳转后的最终网址比主机名，要完全一样；只查一级来源、详情抓成功的；分批用 limit、after_id。"""
    source = _source(session)
    other = _source(session, url="https://jxt.hubei.gov.cn/tzgg/")
    level2 = _source(session, url="https://kjt.hubei.gov.cn/kjdt/zcjd/", rights_level=2)
    first = _document(session, source, "https://kjt.hubei.gov.cn/a/1.shtml", OLD)
    moved = _document(
        session,
        source,
        "https://www.hubei.gov.cn/r/2.shtml",
        OLD,
        final_url="https://kjt.hubei.gov.cn:443/a/2.shtml",
    )
    _document(session, source, "https://xkjt.hubei.gov.cn/a/3.shtml", OLD)
    _document(session, source, "https://kjt.hubei.gov.cn/a/4.shtml", "", status="parse_empty")
    _document(session, level2, "https://kjt.hubei.gov.cn/a/5.shtml", OLD)
    jxt = _document(session, other, "https://jxt.hubei.gov.cn/a/6.shtml", OLD)
    session.commit()
    page = read_fixture_html("detail_hubei_kjt_70955")
    fake = FakeFetcher(
        {
            first.url: _page(first.url, page),
            moved.url: _page("https://kjt.hubei.gov.cn:443/a/2.shtml", page),
            jxt.url: _page(jxt.url, page),
        }
    )
    with pytest.raises(ValueError):
        refresh_bodies(session, fetcher=fake)
    picked = refresh_bodies(session, hosts=["KJT.hubei.gov.cn"], fetcher=fake)
    assert [r.document_id for r in picked] == [first.id, moved.id]
    assert [
        r.document_id for r in refresh_bodies(session, source_ids=[other.id], fetcher=fake)
    ] == [jxt.id]
    batch = refresh_bodies(session, hosts=[HUBEI], limit=1, after_id=first.id, fetcher=fake)
    assert [r.document_id for r in batch] == [moved.id]


def test_body_changed_meanwhile_is_not_overwritten(session, session_factory) -> None:
    """重新打开页面的这段时间里库里的正文被别的任务改了：不写，原因写明。"""
    doc = _document(session, _source(session), HUBEI_URL, OLD)
    session.commit()
    page = _page(HUBEI_URL, read_fixture_html("detail_hubei_kjt_70955"))

    def fetch_and_meanwhile_update(url):
        with session_factory() as other:
            other.get(DocumentDetail, doc.id).body_text = "别的任务刚写进去的正文，内容不一样。"
            other.commit()
        return page

    fake = FakeFetcher({HUBEI_URL: fetch_and_meanwhile_update})
    results = refresh_bodies(session, hosts=[HUBEI], apply=True, fetcher=fake)
    assert [(r.result, r.written, r.reason) for r in results] == [
        ("trimmed", False, "库里的详情在这期间变了，未写入")
    ]
    assert _stored(session, doc.id).body_text == "别的任务刚写进去的正文，内容不一样。"


def test_refetch_bodies_command(session, monkeypatch, capsys) -> None:
    doc = _document(session, _source(session), HUBEI_URL, OLD)
    session.commit()
    fake = FakeFetcher({HUBEI_URL: _page(HUBEI_URL, read_fixture_html("detail_hubei_kjt_70955"))})

    class Polite:
        def __enter__(self):
            return fake

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(detail_service, "_polite_fetcher", Polite)
    assert cli.main(["refetch-bodies"]) == 2
    assert "要用 --host 或 --source-id" in capsys.readouterr().err

    assert cli.main(["refetch-bodies", "--host", HUBEI]) == 0
    out = capsys.readouterr().out
    assert f"  #{doc.id} {HUBEI_TITLE}" in out
    # 每行最多显示 30 个字，最多 3 行
    assert (
        f"      去掉开头 4 行：当前位置：首页>科技动态>通知公告 | {HUBEI_TITLE[:30]} | "
        "2026-10-20 10:04 | …"
    ) in out
    assert "      去掉结尾 6 行：附件: | 欢迎关注湖北科技公众号 | 相关文档 …" in out
    assert (
        "预览（未写入）：检查 1 份，只去掉网页上的字 1 份，其他变化 0 份，没变 0 份，"
        "新正文为空 0 份，没抓成功 0 份；写入 0 份"
    ) in out
    assert (
        "加 --apply 写入（换正文和抓取时间，后台 AI 复查下一轮重查）；其他变化的要再加 --include-changed"
        in out
    )
    assert (
        f"refetch_bodies mode=preview checked=1 trimmed=1 changed=0 unchanged=0 empty=0 "
        f"failed=0 written=0 last_id={doc.id} approved_fields=0 confirmed_conditions=0 "
        "confirmed_reposts=0 ai_results=0 lost_quotes=0 ai_checks=0"
    ) in out

    assert cli.main(["refetch-bodies", "--host", HUBEI, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "已写入：检查 1 份，只去掉网页上的字 1 份" in out
    assert "      已写入" in out
    assert "refetch_bodies mode=apply checked=1 trimmed=1" in out and "written=1" in out
    assert _stored(session, doc.id).body_text == CLEAN

    assert cli.main(["refetch-bodies", "--host", HUBEI, "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "没变 1 份" in out
    assert f"已到 --limit 1 上限，可能还有没检查的，用 --after-id {doc.id} 接着跑" in out


def test_survey_counts_bodies_with_page_chrome(session, capsys) -> None:
    """只读、不联网：每个一级来源抓成功的正文里带网页字样的份数，多的在前。"""
    hubei = _source(session)
    clean = _source(session, url="https://jxt.hubei.gov.cn/tzgg/")
    level2 = _source(session, url="https://kjt.hubei.gov.cn/kjdt/zcjd/", rights_level=2)
    _document(session, clean, "https://jxt.hubei.gov.cn/a/1.shtml", CLEAN)
    _document(session, hubei, "https://kjt.hubei.gov.cn/a/2.shtml", OLD)
    _document(session, hubei, "https://kjt.hubei.gov.cn/a/3.shtml", CLEAN)
    _document(session, hubei, "https://kjt.hubei.gov.cn/a/4.shtml", OLD, status="parse_empty")
    _document(session, level2, "https://kjt.hubei.gov.cn/a/5.shtml", OLD)
    session.commit()
    assert [(r.source.id, r.ok, r.hits) for r in survey(session)] == [
        (hubei.id, 2, 1),
        (clean.id, 1, 0),
    ]

    assert cli.main(["refetch-bodies", "--survey"]) == 0
    out = capsys.readouterr().out
    assert (
        f"  #{hubei.id} 湖北 湖北省科学技术厅 通知公告（kjt.hubei.gov.cn）：抓成功 2 份，"
        "正文带网页字样 1 份"
    ) in out
    assert f"refetch_bodies survey source={hubei.id} host=kjt.hubei.gov.cn ok=2 hits=1" in out
    assert f"source={clean.id}" not in out
    assert "共 2 个来源，1 个有带网页字样的正文，合计 1 份" in out
