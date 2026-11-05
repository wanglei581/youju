from datetime import date
from types import SimpleNamespace

from youju.dedup import _split_by_content, match_key


def _doc(title: str) -> SimpleNamespace:
    return SimpleNamespace(title=title, publish_date=date(2026, 5, 21))


SOURCE = SimpleNamespace(dept_full_name="青海省科学技术厅")


def test_match_key_uses_title_issuer_and_date() -> None:
    a = match_key(_doc("关于公布 2026年专精特新中小企业名单的通知"), None, SOURCE)
    b = match_key(_doc("关于公布2026年专精特新中小企业名单的通知"), None, SOURCE)
    assert a == b and a is not None
    other = SimpleNamespace(dept_full_name="山东省工业和信息化厅")
    assert match_key(_doc("关于公布2026年专精特新中小企业名单的通知"), None, other) != a


def test_truncated_titles_are_not_merged() -> None:
    """列表页截断的标题，不同文件可能截成一样（如"关于印发《青海省..."），不参与归并。"""
    assert match_key(_doc("中共青海省科学技术厅党组关于印发《青海省..."), None, SOURCE) is None
    assert match_key(_doc("关于印发《青海省科技计划…"), None, SOURCE) is None


def test_same_title_but_different_number_or_body_is_not_merged() -> None:
    body_a = "摘要A"
    body_b = "摘要B"
    groups = _split_by_content(
        [
            (1, ("青科发〔2026〕12号", body_a)),
            (2, ("青科发〔2026〕13号", None)),  # 文号不同：另成一组
            (3, (None, body_b)),  # 正文与第一组不同；与第 2 条没有冲突的信息，并入第 2 组
            (4, (None, None)),  # 取不到文号和正文：跟第一组
            (5, ("青科发〔2026〕12号", None)),  # 同一文号
        ]
    )
    assert groups == [[1, 4, 5], [2, 3]]


def test_signature_compares_whole_body() -> None:
    from youju.dedup import _signature

    head = "各有关单位：现将有关事项通知如下。" * 15
    tail = "联系人及电话见附件。" * 5
    a = SimpleNamespace(doc_number=None, body_text=head + "申报截止时间为10月9日。" + tail)
    b = SimpleNamespace(doc_number=None, body_text=head + "验收材料于11月1日前报送。" + tail)
    same = SimpleNamespace(
        doc_number=None, body_text=(head + "申报截止时间为10月9日。" + tail).replace("。", "。\n")
    )
    assert _signature(a)[1] != _signature(b)[1]  # 中段不同
    assert _signature(a)[1] == _signature(same)[1]  # 只差空白换行


def test_auto_split_can_merge_again_but_manual_undo_sticks(session) -> None:
    from datetime import UTC, datetime

    from youju.dedup import reconcile_documents
    from youju.models import Document, DocumentDetail, DocumentMerge, Source

    src = Source(
        level="省级",
        province="青海",
        dept="科技",
        dept_full_name="青海省科学技术厅",
        column_name="通知",
        url="https://kjt.qinghai.gov.cn/tz/",
        enabled=True,
        parser_config={},
    )
    session.add(src)
    session.flush()
    now = datetime.now(UTC)
    docs = [
        Document(
            source_id=src.id,
            url=f"https://kjt.qinghai.gov.cn/{i}.html",
            url_key=f"kjt.qinghai.gov.cn/{i}.html",
            title="关于组织申报2026年度省重点研发项目的通知",
            publish_date=date(2026, 9, 1),
            first_seen_at=now,
            last_seen_at=now,
        )
        for i in (1, 2)
    ]
    session.add_all(docs)
    session.flush()
    details = [
        DocumentDetail(
            document_id=d.id,
            body_text="",
            doc_number="青科发〔2026〕1号",
            fetched_at=now,
            status="ok",
        )
        for d in docs
    ]
    session.add_all(details)
    session.commit()
    assert reconcile_documents(session) == 1
    session.commit()
    # 文号变得不同：自动拆开（删掉映射）
    details[1].doc_number = "青科发〔2026〕2号"
    session.commit()
    reconcile_documents(session)
    session.commit()
    assert session.query(DocumentMerge).count() == 0
    # 文号恢复一致：还能重新归并
    details[1].doc_number = "青科发〔2026〕1号"
    session.commit()
    reconcile_documents(session)
    session.commit()
    row = session.query(DocumentMerge).one()
    assert row.active
    # 人工撤销（active=False）后，重跑不会覆盖
    row.active = False
    session.commit()
    reconcile_documents(session)
    session.commit()
    assert session.query(DocumentMerge).one().active is False


def test_short_bodies_are_compared_too() -> None:
    from youju.dedup import _signature

    a = SimpleNamespace(
        doc_number=None, body_text="请于10月9日前报送材料，逾期不予受理。联系电话见附件。"
    )
    b = SimpleNamespace(
        doc_number=None, body_text="请于11月1日前报送材料，逾期不予受理。联系电话见附件。"
    )
    empty = SimpleNamespace(doc_number=None, body_text="")
    assert _signature(a)[1] != _signature(b)[1]
    assert _signature(empty)[1] is None


def test_signature_ignores_page_header_time_and_counter() -> None:
    """同一份文件发了两个网址：正文开头网页自带的发布时刻、访问计数不同，仍算同一份（11/4 完整测试）；
    正文里别处的时间、开头以外的数字照样比。"""
    from youju.dedup import _signature

    title = "关于第十五届创新创业大赛获奖单位名单的公示"
    rest = "根据有关通知，现将获奖单位名单予以公示。公示期为5个工作日。" * 10

    def body(head: str, tail: str = rest) -> SimpleNamespace:
        return SimpleNamespace(doc_number=None, body_text=f"{title}\n{head}\n来源：科学技术厅\n{tail}")

    a = body("发布时间：2026-11-03 16:25\n访问量：12次")
    b = body("发布时间：2026-11-03 16:19\n访问量：37次")
    assert _signature(a)[1] == _signature(b)[1]
    # 写成“2026年11月3日 16:25:08”、没有冒号后空格的也一样
    c = body("发布日期：2026年11月3日16:25:08")
    d = body("发布日期：2026年11月3日 09:02:41")
    assert _signature(c)[1] == _signature(d)[1]
    # 发布日期不同：照样不同
    assert _signature(body("发布时间：2026-11-03 16:25"))[1] != _signature(body("发布时间：2026-11-04 16:25"))[1]
    # 没有“发布时间”一类标签的时刻（如截止时间）不动
    e = body("发布时间：2026-11-03 16:25", "申报截止时间：2026-11-20 17:00。" + rest)
    f = body("发布时间：2026-11-03 16:25", "申报截止时间：2026-11-20 18:00。" + rest)
    assert _signature(e)[1] != _signature(f)[1]
    # 开头一段以外的“发布时间”不动（正文里引用别的文件的发布时间）
    g = body("发布时间：2026-11-03 16:25", rest + "原通知发布时间：2026-10-01 09:00。")
    h = body("发布时间：2026-11-03 16:25", rest + "原通知发布时间：2026-10-01 10:00。")
    assert _signature(g)[1] != _signature(h)[1]
