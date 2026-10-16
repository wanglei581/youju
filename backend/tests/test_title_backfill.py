"""列表页截断的标题（以"..."或"…"结尾）用详情页标明的完整标题补全（10/16 模拟器验收缺陷 1）。

只认详情页自己标明的标题（ArticleTitle 元数据、h1），且必须以已存标题去掉省略号的部分开头；
对不上、写法不一致、本来没截断的都不改。"""

import pytest
from sqlalchemy import select

from tests.conftest import read_fixture_html
from tests.test_detail_service import (
    HENAN_FULL,
    HENAN_TRUNCATED,
    FakeFetcher,
    add_document,
    detail_page,
)
from youju import cli
from youju.classify import doc_kind
from youju.crawler import detail_service
from youju.crawler.detail import parse_detail
from youju.crawler.detail_service import backfill_truncated_titles
from youju.crawler.fetcher import FetchResult
from youju.crawler.titles import completed_title, is_truncated
from youju.models import Document, DocumentDetail

# 青海科技厅：<title> 只有站名，没有 ArticleTitle，h1 是完整标题；h2 是位置导航和上下篇
QINGHAI_PAGE = (
    "<html><head><title>青海省科学技术厅</title></head><body>"
    "<h2>您所在的位置：首页 > 信息公开目录 > 信息公开详情</h2>"
    "<h1>青海省科学技术厅关于举办2026年青海省系列科普活动的通知</h1>"
    "<div id='zoom'><p>各市州科技局：为深入实施全民科学素质行动，现将有关事项通知如下。</p></div>"
    "<h2>上一条 :青海省科学技术厅关于印发《2026年乡村振兴科技支撑行动实施方案》的通知</h2>"
    "</body></html>"
)
# 湖南科技厅：ArticleTitle 完整；页面上还有一堆别的 h1（领导姓名、平台名）
HUNAN_FULL = (
    "湖南省科学技术厅 湖南省商务厅 湖南省财政厅 国家税务总局湖南省税务局 湖南省发展和改革委员会 "
    "关于组织开展湖南省2026年技术先进型服务企业认定工作的通知"
)
HUNAN_PAGE = (
    f"<html><head><title>{HUNAN_FULL}-湖南省科学技术厅</title>"
    f"<meta name='ArticleTitle' content='{HUNAN_FULL}'></head><body>"
    "<h1>某某某</h1><h1>湖南科技创新管理平台 (湖南科技云)</h1>"
    f"<h1 class='article_title'>{HUNAN_FULL}</h1>"
    "<div id='zoom'><p>各市州科技局、商务局：现将技术先进型服务企业认定工作有关事项通知如下。</p></div>"
    "</body></html>"
)


@pytest.mark.parametrize(
    ("stored", "page", "url", "expected"),
    [
        (
            "青海省科学技术厅关于举办2026年青海省系列...",
            QINGHAI_PAGE,
            "https://kjt.qinghai.gov.cn/content/applyshow/id/3698",
            "青海省科学技术厅关于举办2026年青海省系列科普活动的通知",
        ),
        (
            "湖南省科学技术厅 湖南省商务厅 湖南省财政厅 国家税务总局湖南省税务局 湖南省发...",
            HUNAN_PAGE,
            "https://kjt.hunan.gov.cn/kjt/xxgk/tzgg/tzgg_1/202609/t20260910_34061407.html",
            HUNAN_FULL,
        ),
        # 单字省略号；宁夏列表带进来的装饰符"》"先去掉再比
        (
            "》 " + HENAN_TRUNCATED.removesuffix("...") + "…",
            detail_page(HENAN_FULL),
            "",
            HENAN_FULL,
        ),
        # 多打一个点（西藏经信厅"……申报指南...."）；空白写法不同也认
        (
            HENAN_TRUNCATED.replace(" ", "") + ".",
            detail_page(HENAN_FULL),
            "",
            HENAN_FULL,
        ),
    ],
)
def test_truncated_title_is_completed_from_the_detail_page(stored, page, url, expected) -> None:
    result = parse_detail(page, url or "https://gxt.henan.gov.cn/2026/09-26/1.html")
    assert completed_title(stored, result.page_titles) == expected
    assert not is_truncated(expected)
    # 补全后再判断一次不会再变（幂等）
    assert completed_title(expected, result.page_titles) is None


def test_titles_that_do_not_match_are_left_alone() -> None:
    page_titles = parse_detail(
        detail_page(HENAN_FULL), "https://gxt.henan.gov.cn/2026/09-26/1.html"
    ).page_titles
    assert page_titles == [HENAN_FULL, HENAN_FULL]
    # 没有省略号：哪怕详情页标题更长也不改
    assert completed_title(HENAN_FULL[:-6], page_titles) is None
    # 开头对不上：详情页是另一份文件
    assert (
        completed_title("河南省工业和信息化厅关于公布2026年度专精特新企业...", page_titles) is None
    )
    # 去掉省略号只剩不到 10 个字：太短，可能撞上页面里别的标题
    assert completed_title("河南省工业和信息化...", page_titles) is None
    assert completed_title("河南省工业和信息化厅...", page_titles) == HENAN_FULL
    # 详情页标题本身也是截断的，或和已存标题一样长
    assert completed_title(HENAN_TRUNCATED, [HENAN_FULL[:-10] + "..."]) is None
    assert completed_title(HENAN_TRUNCATED, [HENAN_TRUNCATED[:-3]]) is None
    # 元数据和 h1 写得不一样：不猜
    ambiguous = detail_page(HENAN_FULL, article_title=HENAN_FULL[:-3] + "的补充通知")
    titles = parse_detail(ambiguous, "https://gxt.henan.gov.cn/2026/09-26/1.html").page_titles
    assert completed_title(HENAN_TRUNCATED, titles) is None
    # 长得不像标题（整块内容塞进 h1）
    assert completed_title(HENAN_TRUNCATED, [HENAN_FULL + "。" + "正文" * 100]) is None
    # 没有任何可用标题
    assert completed_title(HENAN_TRUNCATED, []) is None


def test_markup_residue_in_real_article_title_is_not_used() -> None:
    """辽宁工信厅的 ArticleTitle 里有"&ltbr>"残留（真实页面快照），这样的写法不采用。"""
    result = parse_detail(
        read_fixture_html("detail_liaoning_343"),
        "https://gxt.ln.gov.cn/gxt/tztg/2026091717062662168/index.shtml",
    )
    assert "&ltbr>" in result.page_titles[0]
    assert (
        completed_title(
            "辽宁省工业和信息化厅 辽宁省财政厅 国家税务总局 辽宁省...", result.page_titles
        )
        is None
    )


def test_page_titles_come_from_article_title_and_h1_only() -> None:
    result = parse_detail(
        read_fixture_html("detail_fujian_915"),
        "https://gxt.fujian.gov.cn/zwgk/gsgg/202609/t20260924_7218179.htm",
    )
    # <title> 带栏目名和站名，不收
    assert result.page_titles == [
        "福建省工业和信息化厅等六部门关于开展2026年度视听系统典型案例征集工作的通知"
    ]
    # 带个人信息的标题不收（标题入库不脱敏）
    with_mobile = parse_detail(
        "<html><head><meta name='ArticleTitle' content='关于做好2026年度申报工作的通知'></head>"
        "<body><h1>关于做好2026年度申报工作的通知（咨询13812345678）</h1></body></html>",
        "https://example.gov.cn/a.html",
    )
    assert with_mobile.page_titles == ["关于做好2026年度申报工作的通知"]
    # 防护页不给标题
    protected = parse_detail(
        "<html><title>安全验证</title><body><h1>请输入验证码以继续访问本站页面</h1></body></html>",
        "https://example.gov.cn/a.html",
    )
    assert protected.page_titles == []


SHANGHAI_GLUED = (
    "关于第四批上海工业遗产拟认定名单的公示 为贯彻落实《国家工业遗产管理办法》的文件精神和工作要求，"
    "加强上海工业遗产活化利用及管理..."
)


def backfill_fixture(session):
    filled = add_document(session, 201, title=HENAN_TRUNCATED)
    mismatch = add_document(
        session,
        202,
        source_id=filled.source_id,
        title="关于开展2026年度专精特新中小企业认定工作的...",
    )
    unreachable = add_document(
        session, 203, source_id=filled.source_id, title="关于开展2026年度工业设计中心认定工作的..."
    )
    # 上海经信委旧数据：标题后粘了正文，清理后本身就不是截断标题（youju reclassify 会清理）
    glued = add_document(session, 204, source_id=filled.source_id, title=SHANGHAI_GLUED)
    untouched = add_document(session, 205, source_id=filled.source_id)
    other_rights = add_document(session, 206, rights_level=2, title=HENAN_TRUNCATED[:-3] + "…")
    fake = FakeFetcher(
        {
            filled.url: FetchResult(
                filled.url, "ok", http_status=200, text=detail_page(HENAN_FULL)
            ),
            mismatch.url: FetchResult(
                mismatch.url,
                "ok",
                http_status=200,
                text=detail_page("关于公布2026年度专精特新中小企业名单的通知"),
            ),
            unreachable.url: FetchResult(unreachable.url, "network_error", error="timeout"),
        }
    )
    docs = {
        "filled": filled,
        "mismatch": mismatch,
        "unreachable": unreachable,
        "glued": glued,
        "untouched": untouched,
        "other_rights": other_rights,
    }
    return docs, fake


def titles(session) -> dict[int, str]:
    # 命令行用自己的连接写库：先结束本会话的事务，才能读到它提交的结果
    session.commit()
    session.expire_all()
    return dict(session.execute(select(Document.id, Document.title)).all())


def test_backfill_previews_by_default_and_applies_only_when_asked(session) -> None:
    docs, fake = backfill_fixture(session)
    before = titles(session)

    preview = backfill_truncated_titles(session, limit=50, fetcher=fake)
    assert [(r.document_id, r.result) for r in preview] == [
        (docs["filled"].id, "filled"),
        (docs["mismatch"].id, "no_match"),
        (docs["unreachable"].id, "unavailable"),
        (docs["glued"].id, "not_truncated"),
    ]
    assert preview[0].new_title == HENAN_FULL
    assert preview[2].reason == "network_error"
    # 预览不写库；清理后不是截断标题的不去抓
    assert titles(session) == before
    assert docs["glued"].url not in fake.calls

    applied = backfill_truncated_titles(session, apply=True, limit=50, fetcher=fake)
    assert [(r.document_id, r.result) for r in applied] == [
        (r.document_id, r.result) for r in preview
    ]
    after = titles(session)
    assert after == {**before, docs["filled"].id: HENAN_FULL}
    doc = session.get(Document, docs["filled"].id)
    assert doc.rule_kind == doc_kind(HENAN_FULL) == "申报通知"
    # 只改标题：不建详情、不动抓取状态
    assert session.scalars(select(DocumentDetail)).all() == []
    assert doc.status == "discovered"

    # 再跑一遍：补全过的不再选中，其余照旧不改
    again = backfill_truncated_titles(session, apply=True, limit=50, fetcher=fake)
    assert docs["filled"].id not in [r.document_id for r in again]
    assert titles(session) == after


def test_backfill_limits_and_filters(session) -> None:
    docs, fake = backfill_fixture(session)
    with pytest.raises(ValueError, match="limit"):
        backfill_truncated_titles(session, limit=0, fetcher=fake)
    assert [r.document_id for r in backfill_truncated_titles(session, limit=1, fetcher=fake)] == [
        docs["filled"].id
    ]
    after = backfill_truncated_titles(session, limit=1, after_id=docs["filled"].id, fetcher=fake)
    assert [r.document_id for r in after] == [docs["mismatch"].id]
    other = docs["other_rights"].source_id
    assert backfill_truncated_titles(session, limit=50, source_id=other, fetcher=fake) == []


def test_backfill_titles_command(session, monkeypatch, capsys) -> None:
    docs, fake = backfill_fixture(session)

    class Polite:
        def __enter__(self):
            return fake

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(detail_service, "_polite_fetcher", Polite)
    assert cli.main(["backfill-titles"]) == 0
    out = capsys.readouterr().out
    assert f"→ {HENAN_FULL}（类型 其他 → 申报通知）" in out
    assert "预览（未写入）：截断标题 4 份，可补全 1 份（其中按标题算的类型会变 1 份）" in out
    assert "对不上不改 1 份，详情页不可用 1 份，清理后不是截断标题 1 份" in out
    assert "加 --apply 写入" in out
    assert titles(session)[docs["filled"].id] == HENAN_TRUNCATED

    assert cli.main(["backfill-titles", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "已写入：截断标题 4 份，已补全 1 份" in out
    assert "--after-id" not in out
    assert titles(session)[docs["filled"].id] == HENAN_FULL

    # 分批：到了 --limit 就提示从哪一份之后接着处理
    assert cli.main(["backfill-titles", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert (
        f"已到 --limit 2 份，后面可能还有：加 --after-id {docs['unreachable'].id} 接着处理" in out
    )
    assert cli.main(["backfill-titles", "--after-id", str(docs["unreachable"].id)]) == 0
    assert "截断标题 1 份" in capsys.readouterr().out
