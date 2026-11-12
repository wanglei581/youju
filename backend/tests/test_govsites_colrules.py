"""地市部门栏目补充规则（govsites/colrules.py 和 inventory._fill_from_page 的接入点）：不联网、不连数据库。"""

from youju.crawler.fetcher import FetchResult
from youju.govsites import colrules
from youju.govsites.divisions import Division, DivisionIndex
from youju.govsites.inventory import ColumnHit, DeptRow, Inventory, PageInfo, find_columns
from youju.govsites.links import Link

JM = "https://www.jiangmen.gov.cn"
SG = "https://www.sg.gov.cn"


def _page(url: str, links: list[tuple[str, str]], via: str = "a", final: str = "") -> PageInfo:
    final = final or url
    return PageInfo(
        url,
        final,
        FetchResult(url, "ok", final, 200, ""),
        links=[Link(t, u, via) for t, u in links],
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


_NAMES = {"科技": "江门市科学技术局", "发改委": "韶关市发展和改革局", "政府办": "江门市人民政府"}


def _row(
    level: str = "市级", dept: str = "科技", site_type: str = "门户频道", nav: str | None = None
) -> DeptRow:
    row = DeptRow("440700", level, "广东", "江门", "", dept, site_type=site_type)
    row.nav_text = row.dept_full_name = _NAMES.get(dept, "") if nav is None else nav
    return row


def _never(*_):
    raise AssertionError("不该再抓、再找")


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
    excluded = frozenset({f"{JM}/bmpd/jmskxjsj/zwdt/tzgg/index.html"})
    assert colrules.deep_columns(page, excluded, ColumnHit) == {}
    root = _page(f"{JM}/", [("通知公告", f"{JM}/a/b/c/d/tzgg/index.html")])
    assert colrules.deep_columns(root, frozenset(), ColumnHit) == {}


def test_deep_columns_leave_group_columns_to_the_standard_check() -> None:
    """大汉分组还原的栏目由通用规则核对栏目名，这里不认。"""
    links = [("通知公告", f"{JM}/bmpd/x/fdzdgknr/a/tzgg/index.html")]
    page = _page(f"{JM}/bmpd/x/", links, via="jpaas分组")
    assert colrules.deep_columns(page, frozenset(), ColumnHit) == {}


def test_dir_page() -> None:
    assert colrules.dir_page(f"{JM}/bmpd/x/")
    assert colrules.dir_page(f"{JM}/bmpd/x/index.html")
    assert colrules.dir_page(f"{JM}/bmpd/x")
    assert not colrules.dir_page(f"{JM}/zwgk/kjj.html")  # 公共目录里的一篇
    assert not colrules.dir_page(f"{JM}/zwgk/index.html?dept=kjj")  # 靠参数区分


def test_shared_directory_dept_pages_are_left_alone() -> None:
    """挂在门户上的部门页是公共目录里的一篇、或靠参数区分时，目录不归这个部门：两条规则都不用。"""
    for url in (f"{JM}/zwgk/kjj.html", f"{JM}/zwgk/index.html?dept=kjj"):
        page = _page(url, [("通知公告", f"{JM}/zwgk/a/b/c/tzgg/index.html")])
        assert colrules.more_columns(_row(), page, {}, frozenset(), _never, _never, ColumnHit) == {}
    # 独立网站整站归这个部门：照常补
    page = _page(f"{JM}/zwgk/kjj.html", [("通知公告", f"{JM}/zwgk/a/b/c/tzgg/index.html")])
    got = colrules.more_columns(
        _row(site_type="独立网站"), page, {}, frozenset(), _never, _never, ColumnHit
    )
    assert {k: h.url for k, h in got.items()} == {"通知公告": f"{JM}/zwgk/a/b/c/tzgg/index.html"}


def test_own_info_page_same_host_and_under_the_dept_dir() -> None:
    page = _page(
        f"{JM}/bmpd/jmskxjsj/index.html",
        [
            ("政务公开", f"{JM}/zwgk/index.html"),  # 门户顶上公共的
            ("信息公开", "https://www.example.gov.cn/bmpd/jmskxjsj/xxgk/"),  # 别的主机，同样路径
            ("信息公开", f"{JM}/bmpd/jmskxjsj/xxgk/index.html"),
        ],
    )
    assert colrules.own_info_page(page, "/bmpd/jmskxjsj/") == f"{JM}/bmpd/jmskxjsj/xxgk/index.html"
    assert colrules.own_info_page(page, "/") is None


def test_more_columns_leaves_complete_and_government_office_rows_alone() -> None:
    page = _page(f"{JM}/bmpd/x/index.html", [("通知公告", f"{JM}/bmpd/x/a/b/tzgg/index.html")])
    full = {k: ColumnHit(k, k, f"{JM}/{i}/", page.url) for i, k in enumerate(colrules.COLUMN_KINDS)}
    assert colrules.more_columns(_row(), page, full, frozenset(), _never, _never, ColumnHit) == full
    gov = colrules.more_columns(
        _row(dept="政府办"), page, {}, frozenset(), _never, _never, ColumnHit
    )
    assert gov == {}
    assert colrules.more_columns(_row(), None, {}, frozenset(), _never, _never, ColumnHit) == {}


def test_info_page_that_is_blocked_or_moves_out_of_the_dept_dir_is_not_used() -> None:
    dept = _page(f"{SG}/bmpdlm/fgw/", [("信息公开", f"{SG}/bmpdlm/fgw/xxgk/")])
    links = [("政策文件", f"{SG}/bmpdlm/fgw/xxgk/zcwj/")]
    moved = _page(f"{SG}/bmpdlm/fgw/xxgk/", links, final=f"{SG}/zwgk/")  # 打开后跳到门户公共页
    blocked = _page(f"{SG}/bmpdlm/fgw/xxgk/", links)
    blocked.block = "HTTP 412 拒绝访问"
    for info in (moved, blocked):
        got = colrules.more_columns(
            _row(dept="发改委"), dept, {}, frozenset(), lambda _u, i=info: i, _never, ColumnHit
        )
        assert got == {}


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


def test_own_info_page_comes_before_the_portal_public_page() -> None:
    """页面上第一个"信息公开"是门户公共的政务公开：部门自己的信息公开页先补，门户那页只补还缺的；
    部门信息公开页上链到部门目录外的栏目不收。"""
    dept = f"{SG}/bmpdlm/fgw/"
    info = f"{SG}/bmpdlm/fgw/xxgk/"
    portal = f"{SG}/zwgk/"
    pages = {
        dept: _html(
            [("政务公开", portal), ("信息公开", info), ("通知公告", f"{SG}/bmpdlm/fgw/tzgg/")]
        ),
        portal: _html([("政策文件", f"{SG}/zwgk/zcwj/"), ("公示公告", f"{SG}/zwgk/gsgg/")]),
        info: _html(
            [("政策文件", f"{SG}/bmpdlm/fgw/xxgk/zcwj/"), ("公示公告", f"{SG}/zwgk/gsgg/")]
        ),
    }
    inv = _inventory(pages)
    page = inv.page(dept)
    row = _row("市级", "发改委")
    inv._fill_from_page(row, page, [page])
    got = {c.kind: c.url for c in row.columns}
    assert got["通知公告"] == f"{SG}/bmpdlm/fgw/tzgg/"
    assert got["政策文件"] == f"{SG}/bmpdlm/fgw/xxgk/zcwj/"  # 不是门户公共页上的 /zwgk/zcwj/
    # 公示公告部门自己没有：照旧由通用规则从门户那页补（部门信息公开页上链出去的不由这里收）
    assert got["公示公告"] == f"{SG}/zwgk/gsgg/"
    assert next(c for c in row.columns if c.kind == "公示公告").source == portal


def test_portal_channel_needs_a_nav_link_named_after_the_dept() -> None:
    """挂在门户上的部门页要是官方导航上写着本部门名称的链接指过来的，才认它的目录。"""
    page = _page(f"{JM}/bmpd/x/", [("通知公告", f"{JM}/bmpd/x/a/b/tzgg/index.html")])
    row = _row(nav="政务服务")
    assert colrules.more_columns(row, page, {}, frozenset(), _never, _never, ColumnHit) == {}


def test_info_page_picks_among_dept_links_only() -> None:
    """部门信息公开页上先筛出部门目录下的链接再择优：目录外的同名链接排在前面也挤不掉目录内的。"""
    dept = _page(f"{SG}/bmpdlm/fgw/", [("信息公开", f"{SG}/bmpdlm/fgw/xxgk/")])
    info = _page(
        f"{SG}/bmpdlm/fgw/xxgk/",
        [("政策文件", f"{SG}/zwgk/zcwj/"), ("政策文件", f"{SG}/bmpdlm/fgw/zcwj/")],
    )
    got = colrules.more_columns(
        _row(dept="发改委"),
        dept,
        {},
        frozenset(),
        lambda _u: info,
        lambda ps, ex: find_columns(ps, ex),
        ColumnHit,
    )
    assert {k: h.url for k, h in got.items()} == {"政策文件": f"{SG}/bmpdlm/fgw/zcwj/"}
