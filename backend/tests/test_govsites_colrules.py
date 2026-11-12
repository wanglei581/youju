"""地市部门栏目补充规则（govsites/colrules.py 和 inventory._fill_from_page 的接入点）：不联网、不连数据库。"""

from youju.crawler.fetcher import FetchResult
from youju.govsites import colrules
from youju.govsites.divisions import Division, DivisionIndex
from youju.govsites.inventory import ColumnHit, DeptRow, Inventory, PageInfo
from youju.govsites.links import Link

JM = "https://www.jiangmen.gov.cn"


def _page(url: str, links: list[tuple[str, str]]) -> PageInfo:
    return PageInfo(
        url, url, FetchResult(url, "ok", url, 200, ""), links=[Link(t, u, "a") for t, u in links]
    )


def _html(links: list[tuple[str, str]]) -> str:
    body = "".join(f'<a href="{u}">{t}</a>' for t, u in links)
    return f"<html><head><title>部门频道</title></head><body>{body}</body></html>"


class FakeCache:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str) -> FetchResult:
        if url in self.pages:
            return FetchResult(url, "ok", url, 200, self.pages[url])
        return FetchResult(url, "http_error", url, 404, "", "HTTP 404")


def _inventory(pages: dict[str, str]) -> Inventory:
    divisions = [
        Division("440000", "广东省", "省", "省级", "广东", None, None, None),
        Division("440700", "江门市", "地级市", "市级", "广东", "江门", None, "440000"),
    ]
    return Inventory(DivisionIndex(divisions), FakeCache(pages), provinces=["广东"], workers=2)


def _row(level: str = "市级", dept: str = "科技") -> DeptRow:
    return DeptRow("440700", level, "广东", "江门", "", dept, site_type="门户频道")


def test_deep_columns_under_the_dept_dir() -> None:
    page = _page(
        f"{JM}/bmpd/jmskxjsj/index.html",
        [
            ("通知公告", f"{JM}/bmpd/jmskxjsj/zwdt/tzgg/index.html"),  # 5 层：通用规则不收
            ("政策文件", f"{JM}/zwgk/zcwj/index.html"),  # 部门目录外（门户的）
            ("公示公告", f"{JM}/bmpd/jmskxjsj/zwdt/gsgg/202611/t20261101_123.html"),  # 文章
            ("公示公告", "https://www.example.gov.cn/bmpd/jmskxjsj/gsgg/"),  # 别的主机
            ("政策解读", f"{JM}/bmpd/jmskxjsj/a/b/c/d/e/f/g/index.html"),  # 超过 8 层
        ],
    )
    got = colrules.deep_columns(page, frozenset(), ColumnHit)
    assert {k: h.url for k, h in got.items()} == {
        "通知公告": f"{JM}/bmpd/jmskxjsj/zwdt/tzgg/index.html"
    }
    # 门户自己的栏目（exclude）照旧不要；部门页就在网站根上时不放宽
    assert (
        colrules.deep_columns(
            page, frozenset({f"{JM}/bmpd/jmskxjsj/zwdt/tzgg/index.html"}), ColumnHit
        )
        == {}
    )
    root = _page(f"{JM}/", [("通知公告", f"{JM}/a/b/c/d/tzgg/index.html")])
    assert colrules.deep_columns(root, frozenset(), ColumnHit) == {}


def test_channel_root_is_the_parent_named_after_the_dept() -> None:
    page = _page(
        "https://www.sg.gov.cn/bmpdlm/fgw/dt/",
        [
            ("首页", "https://www.sg.gov.cn/"),
            ("韶关市生态环境局", "https://www.sg.gov.cn/bmpdlm/sthjj/"),  # 别的部门
            ("动态信息", "https://www.sg.gov.cn/bmpdlm/fgw/dt/"),  # 就是当前目录
            ("韶关市发展和改革局", "https://www.sg.gov.cn/bmpdlm/fgw/"),
        ],
    )
    assert colrules.channel_root(page, "发改委") == "https://www.sg.gov.cn/bmpdlm/fgw/"
    assert colrules.channel_root(page, "财政") is None


def test_own_info_page_only_under_the_dept_dir() -> None:
    page = _page(
        f"{JM}/bmpd/jmskxjsj/index.html",
        [
            ("政务公开", f"{JM}/zwgk/index.html"),  # 门户顶上公共的
            ("信息公开", f"{JM}/bmpd/jmskxjsj/xxgk/index.html"),
        ],
    )
    assert colrules.own_info_page(page, "/bmpd/jmskxjsj/") == f"{JM}/bmpd/jmskxjsj/xxgk/index.html"
    assert colrules.own_info_page(page, "/") is None


def test_more_columns_leaves_complete_and_government_office_rows_alone() -> None:
    page = _page(f"{JM}/bmpd/x/index.html", [("通知公告", f"{JM}/bmpd/x/a/b/tzgg/index.html")])

    def boom(*_):
        raise AssertionError("不该再抓、再找")

    full = {k: ColumnHit(k, k, f"{JM}/{i}/", page.url) for i, k in enumerate(colrules.COLUMN_KINDS)}
    assert colrules.more_columns(_row(), page, full, frozenset(), boom, boom, ColumnHit) == full
    gov = colrules.more_columns(_row(dept="政府办"), page, {}, frozenset(), boom, boom, ColumnHit)
    assert gov == {}
    assert colrules.more_columns(_row(), None, {}, frozenset(), boom, boom, ColumnHit) == {}


def test_fill_from_page_adds_deep_columns_for_city_rows_only() -> None:
    url = f"{JM}/bmpd/jmskxjsj/index.html"
    links = [
        ("通知公告", f"{JM}/bmpd/jmskxjsj/zwdt/tzgg/index.html"),
        ("政策文件", f"{JM}/bmpd/jmskxjsj/zwgk/zcwj/index.html"),
    ]
    inv = _inventory({url: _html(links)})
    page = inv.page(url)
    city = _row("市级")
    inv._fill_from_page(city, page, [page])
    assert {c.kind: c.url for c in city.columns} == {
        "通知公告": f"{JM}/bmpd/jmskxjsj/zwdt/tzgg/index.html",
        "政策文件": f"{JM}/bmpd/jmskxjsj/zwgk/zcwj/index.html",
    }
    county = _row("区县级")
    inv._fill_from_page(county, page, [page])
    assert county.columns == []  # 区县级照旧，不走补充规则


def test_fill_from_page_follows_channel_root_and_own_info_page() -> None:
    sg = "https://www.sg.gov.cn"
    sub = f"{sg}/bmpdlm/fgw/dt/"
    root = f"{sg}/bmpdlm/fgw/"
    info = f"{sg}/bmpdlm/fgw/xxgk/"
    pages = {
        sub: _html([("韶关市发展和改革局", root), ("动态信息", sub)]),
        root: _html(
            [
                ("政务公开", f"{sg}/zwgk/"),  # 第一个信息公开链接是门户公共的
                ("信息公开", info),
                ("通知公告", f"{sg}/bmpdlm/fgw/tzgg/"),
            ]
        ),
        f"{sg}/zwgk/": _html([("政策文件", f"{sg}/zwgk/zcwj/")]),
        info: _html([("政策文件", f"{sg}/bmpdlm/fgw/xxgk/zcwj/")]),
    }
    inv = _inventory(pages)
    page = inv.page(sub)
    row = _row("市级", "发改委")
    inv._fill_from_page(row, page, [page], exclude=frozenset({f"{sg}/zwgk/zcwj/"}))
    assert {c.kind: c.url for c in row.columns} == {
        "通知公告": f"{sg}/bmpdlm/fgw/tzgg/",
        "政策文件": f"{sg}/bmpdlm/fgw/xxgk/zcwj/",
    }
