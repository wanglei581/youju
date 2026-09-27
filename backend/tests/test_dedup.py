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
