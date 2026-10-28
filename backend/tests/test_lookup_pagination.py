"""分页核心尚不挂 API：纯参数/SQL 契约，以及独立 MySQL 数据集上的完整遍历。"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event, insert, literal, select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import aliased

from youju.lists.lookup import public_condition
from youju.lists.pagination import (
    LookupIdentity,
    LookupInputError,
    PageKey,
    _identity_statement,
    _masters_statement,
    _representatives_statement,
    _selected_entries,
    _valid_code_condition,
    lookup_identities,
    lookup_page,
    parse_identity,
    parse_query,
)
from youju.lists.rules import _CREDIT_ALPHABET, _CREDIT_WEIGHTS, normalize_credit_code
from youju.models import Document, DocumentMerge, ListEntry, Source

NOW = datetime(2026, 10, 23, tzinfo=UTC)
NAME = "合成星河科技有限公司"
CODE = "91310117588657256C"


def _code(n):
    body = f"91310000{n:09d}"
    checksum = (
        31
        - sum(_CREDIT_ALPHABET.index(c) * w for c, w in zip(body, _CREDIT_WEIGHTS, strict=True))
        % 31
    ) % 31
    return body + _CREDIT_ALPHABET[checksum]


def _sql(statement):
    return str(statement.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize(
    "value",
    [
        "张三",
        "王小明",
        "13812345678",
        "91310117588657256A",
        "",
        "甲",
        "123456",
        "1234567890123456",
        "１２３４５６",
        "١٢٣٤٥٦",
    ],
)
def test_pure_query_rejects_person_sensitive_and_invalid_code(value):
    with pytest.raises(LookupInputError):
        parse_query(value)


def test_pure_identity_normalization_and_validation():
    assert parse_query(CODE.lower()).code == CODE
    assert parse_query("合成星河科技（青岛） 有限公司").name == "合成星河科技(青岛)有限公司"
    assert parse_query("合成省科技创新服务管理局").name == "合成省科技创新服务管理局"
    for key in (f"code:{CODE}", f"unverified:{NAME}"):
        assert parse_identity(key).key == key
    for key in (
        f"code:{CODE.lower()}",
        "code:bad",
        "unverified:张三",
        "name:张三",
        "code:",
        "unverified:合成省科技创新服务管理局",
        "unverified:阿卜杜拉阿里木",
    ):
        with pytest.raises(LookupInputError):
            parse_identity(key)


def test_pure_sql_limits_groups_and_aliases_without_body_loading():
    query = parse_query(NAME)
    entries = _selected_entries(query, LookupIdentity("code", CODE)).subquery()
    statements = [
        _identity_statement(query, 20, None),
        _masters_statement(entries, 20, None),
        _masters_statement(entries, 20, PageKey(date(2026, 10, 1), 100)),
        _masters_statement(entries, 20, PageKey(None, 100)),
        _representatives_statement(entries, query, [1, 2]),
    ]
    sql = [_sql(stmt) for stmt in statements]
    assert all("body_text" not in item and "document_details" not in item for item in sql)
    assert all("LIMIT 21" in item for item in sql[:4])
    assert "GROUP BY" in sql[1] and "publish_date DESC" in sql[1] and "master_id DESC" in sql[1]
    assert "publish_date IS NULL" in sql[2] and "publish_date IS NULL" in sql[3]
    assert "min(CASE WHEN" in sql[4] and "GROUP BY" in sql[4]
    doc, source = aliased(Document, name="master"), aliased(Source, name="master_source")
    condition = _sql(public_condition(doc, source))
    assert "master_source.rights_level" in condition and "master.id" in condition
    assert "sources.rights_level" in _sql(public_condition())


@pytest.mark.parametrize("size", [0, 51, -1, True])
def test_pure_page_size_rejected_before_database(size):
    with pytest.raises(LookupInputError):
        lookup_page(None, NAME, LookupIdentity("code", CODE), size=size)
    with pytest.raises(LookupInputError):
        lookup_identities(None, NAME, size=size)


def _source(session, n=1, rights=1):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="合成科技厅",
        column_name="公示",
        url=f"https://example.gov.cn/source/{n}",
        rights_level=rights,
    )
    session.add(source)
    session.flush()
    return source


def _document(session, source, n, published=date(2026, 10, 23), kind="名单"):
    document = Document(
        id=n,
        source_id=source.id,
        url=f"https://example.gov.cn/{n}",
        url_key=f"example.gov.cn/{n}",
        title=f"合成第{n}批科技企业名单公示",
        publish_date=published,
        kind_override=kind,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(document)
    session.flush()
    return document


def _entry(session, doc, seq, code=CODE, name=NAME):
    row = ListEntry(
        document_id=doc.id,
        source_url=doc.url,
        name=name,
        normalized_name=name,
        credit_code=code,
        sequence=str(seq),
        category="科技型中小企业",
    )
    session.add(row)
    session.flush()
    return row


def test_identity_isolates_codes_unverified_and_traces_renamed_company(session):
    source = _source(session)
    doc = _document(session, source, 1)
    older = _entry(session, doc, 1, name="合成旧名称有限公司")
    exact = _entry(session, doc, 2)
    other = _entry(session, doc, 3, code=_code(2))
    unknown = _entry(session, doc, 4, code=None)
    old_doc = _document(session, source, 2, date(2025, 1, 1))
    _entry(session, old_doc, 1, name="合成旧名称有限公司")
    empty = _entry(session, old_doc, 2, code="")
    session.commit()
    identities = lookup_identities(session, NAME)
    assert {i.key for i in identities.identities} == {
        f"code:{CODE}",
        f"code:{_code(2)}",
        f"unverified:{NAME}",
    }
    selected = lookup_page(session, NAME, LookupIdentity("code", CODE))
    assert [item.master_id for item in selected.matches] == [1, 2]
    assert selected.matches[0].entry_id == exact.id != older.id
    assert selected.matches[1].name == "合成旧名称有限公司"
    assert [
        item.entry_id
        for item in lookup_page(session, NAME, LookupIdentity("code", _code(2))).matches
    ] == [other.id]
    assert [
        item.entry_id
        for item in lookup_page(session, NAME, LookupIdentity("unverified", NAME)).matches
    ] == [unknown.id, empty.id]
    with pytest.raises(LookupInputError, match="与查询不符"):
        lookup_page(session, "合成不存在有限公司", LookupIdentity("code", CODE))
    with pytest.raises(LookupInputError, match="与查询不符"):
        lookup_page(session, CODE, LookupIdentity("code", _code(2)))


def test_identity_choices_over_twenty_and_invalid_legacy_code_progress(session):
    doc = _document(session, _source(session), 1)
    _entry(session, doc, 0, code=None)
    for n in range(1, 34):
        _entry(session, doc, n, code=_code(n))
    _entry(session, doc, 99, code="00000000000000000A")
    _entry(session, doc, 100, code=CODE.lower())
    _entry(session, doc, 101, code="00000000000000000a")
    _entry(session, doc, 102, code="")
    session.commit()
    after, found = None, []
    for _ in range(10):
        page = lookup_identities(session, NAME, size=7, after=after)
        found.extend(i.key for i in page.identities)
        assert len(page.identities) <= 7
        if page.next_after is None:
            break
        assert after is None or page.next_after > after
        after = page.next_after
    else:
        pytest.fail("身份分页未结束")
    assert len(found) == len(set(found)) == 34
    assert set(found) == {f"unverified:{NAME}", *(f"code:{_code(n)}" for n in range(1, 34))}


def test_stored_coded_agency_without_org_suffix_is_queryable(session):
    name = "合成省科技创新服务管理局"
    doc = _document(session, _source(session), 1)
    row = _entry(session, doc, 1, name=name)
    session.commit()
    identities = lookup_identities(session, name)
    assert identities.identities == (LookupIdentity("code", CODE),)
    assert [m.entry_id for m in lookup_page(session, name, identities.identities[0]).matches] == [
        row.id
    ]
    assert lookup_identities(session, "合成省不存在的管理局").identities == ()


@pytest.mark.parametrize("name", ["阿卜杜拉阿里木", "合成省科技创新服务管理局"])
def test_non_org_names_without_valid_code_never_form_unverified_identity(session, name):
    doc = _document(session, _source(session), 1)
    for seq, code in enumerate([None, "", "00000000000000000A"]):
        _entry(session, doc, seq, name=name, code=code)
    session.commit()
    assert parse_query(name).requires_code
    assert lookup_identities(session, name).identities == ()
    with pytest.raises(LookupInputError):
        lookup_page(session, name, LookupIdentity("unverified", name))


def test_sql_code_validation_matches_canonical_normalization(session):
    def with_checksum(body):
        checksum = (
            31
            - sum(_CREDIT_ALPHABET.index(c) * w for c, w in zip(body, _CREDIT_WEIGHTS, strict=True))
            % 31
        ) % 31
        return body + _CREDIT_ALPHABET[checksum]

    samples = [
        *(_code(n) for n in range(1, 100)),
        CODE,
        CODE.lower(),
        "91310117588657256A",
        "00000000000000000A",
        "00000000000000000a",
        "I" * 18,
        "０" * 18,
        "",
        " " + CODE,
        CODE + " ",
        CODE + "\n",
        with_checksum("11010119900101003"),
        with_checksum("AA13812345678AAAA"),
        None,
    ]
    for value in samples:
        expected = value is not None and normalize_credit_code(value) == value and bool(value)
        assert bool(session.scalar(select(_valid_code_condition(literal(value))))) == expected


def test_over_hundred_canonicals_and_two_thousand_members_stay_bounded(session):
    source = _source(session)
    dates = {i: NOW.date() - timedelta(days=i // 3) if i <= 110 else None for i in range(1, 116)}
    rows = []
    for n in [*dates, *range(1000, 3105)]:
        rows.append(
            dict(
                id=n,
                source_id=source.id,
                url=f"https://example.gov.cn/{n}",
                url_key=f"example.gov.cn/{n}",
                title=f"合成第{n}份名单公示",
                kind_override="名单",
                publish_date=dates[n] if n in dates else date(2027, 1, 1),
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
    session.execute(insert(Document), rows)
    session.execute(
        insert(DocumentMerge),
        [
            dict(duplicate_id=n, canonical_id=1, active=True, match_key="test")
            for n in range(1000, 3105)
        ],
    )
    session.execute(
        insert(ListEntry),
        [
            dict(
                document_id=n,
                source_url=f"https://example.gov.cn/{n}",
                name=NAME,
                normalized_name=NAME,
                credit_code=CODE,
                sequence="1",
                category="科技型中小企业",
            )
            for n in [*range(2, 116), *range(1000, 3105)]
        ],
    )
    session.commit()
    returned = []

    def count_rows(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            returned.append(cursor.rowcount)

    event.listen(session.bind, "after_cursor_execute", count_rows)
    try:
        after, found = None, []
        for _ in range(10):
            page = lookup_page(session, CODE, LookupIdentity("code", CODE), size=17, after=after)
            assert len(page.matches) <= 17
            found.extend(page.matches)
            if page.next_after is None:
                break
            after = page.next_after
        else:
            pytest.fail("名单分页未结束")
    finally:
        event.remove(session.bind, "after_cursor_execute", count_rows)
    expected = sorted(
        dates, key=lambda n: (dates[n] is not None, dates[n] or date.min, n), reverse=True
    )
    assert [item.master_id for item in found] == expected
    assert len({item.master_id for item in found}) == 115
    master = next(item for item in found if item.master_id == 1)
    assert master.document_id == 1000 and master.publish_date == dates[1]
    assert returned and all(0 <= n <= 18 for n in returned)


def test_public_rules_manual_kind_and_group_takedown_match_existing_core(session):
    public = _source(session)
    private = _source(session, 2, rights=0)
    master = _document(session, private, 1)
    member = _document(session, public, 2)
    non_list = _document(session, public, 3, kind="其他")
    hidden = _document(session, private, 4)
    for doc in (member, non_list, hidden):
        _entry(session, doc, 1)
    session.add(
        DocumentMerge(duplicate_id=member.id, canonical_id=master.id, active=True, match_key="test")
    )
    session.commit()
    identity = LookupIdentity("code", CODE)
    assert [row.master_id for row in lookup_page(session, CODE, identity).matches] == [1]
    member.taken_down_at = NOW
    session.commit()
    assert lookup_page(session, CODE, identity).matches == ()
    assert lookup_identities(session, CODE).identities == ()
    member.taken_down_at = None
    public.rights_level = 2
    session.commit()
    assert lookup_page(session, CODE, identity).matches == ()
