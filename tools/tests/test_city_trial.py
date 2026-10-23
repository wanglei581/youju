import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path

TEAM = Path(__file__).parents[1] / "team"
sys.path.insert(0, str(TEAM))
SPEC = importlib.util.spec_from_file_location("city_trial", TEAM / "city_trial.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def items(*days):
    return [
        {"title": f"第{n}份", "url": f"https://x.gov.cn/{n}.html", "publish_date": d}
        for n, d in enumerate(days)
    ]


class WindowTests(unittest.TestCase):
    def test_window_is_eight_full_weeks_ending_yesterday(self):
        start, end = MODULE.window(date(2026, 10, 23))
        self.assertEqual(end, date(2026, 10, 22))
        self.assertEqual((end - start).days + 1, 56)

    def test_one_odd_date_at_the_tail_does_not_decide(self):
        # 末尾夹一条置底的旧文，不能当成已经翻过窗口开始
        page = items("2026-10-20", "2026-10-15", "2026-10-09", "2022-01-12")
        self.assertEqual(MODULE.tail_date(page), date(2026, 10, 9))
        self.assertFalse(MODULE.covered(page, date(2026, 8, 28)))

    def test_only_merges_runs_from_the_same_day(self):
        self.assertTrue(MODULE.same_day({}, date(2026, 10, 23)))
        self.assertTrue(MODULE.same_day({"today": "2026-10-23"}, date(2026, 10, 23)))
        self.assertFalse(MODULE.same_day({"today": "2026-10-22"}, date(2026, 10, 23)))

    def test_shared_weekly_scales_each_column_by_its_own_days(self):
        # 一个栏目盖住 7 天触到 1 组，另一个盖住 56 天触到 8 组，其中 1 组两边都有
        parts = [({1}, 7), ({1, 2, 3, 4, 5, 6, 7, 8}, 56)]
        self.assertEqual(MODULE.shared_weekly(parts), round((1 + 1) * 8 / 9, 1))
        self.assertEqual(MODULE.shared_weekly([]), 0.0)
        self.assertEqual(MODULE.shared_weekly([(set(), 56)]), 0.0)

    def test_boilerplate_is_footer_first_or_repeated(self):
        footer = "主办单位：某市科学技术局 网站标识码：2101000018 辽ICP备：15010522号"
        bodies = {
            1: footer,
            2: "一、申报条件" + "具体要求" * 40 + footer,
            3: "同样的公共部分",
            4: "同样的公共部分",
            5: "同样的公共部分",
        }
        self.assertEqual(MODULE.boilerplate_ids(bodies), {1, 3, 4, 5})

    def test_pinned_old_items_do_not_count_as_covered(self):
        # 置顶的旧年报在页首，末尾几条还在窗口里：还要翻页
        page = items("2022-01-12", "2026-10-20", "2026-10-15", "2026-10-09")
        self.assertFalse(MODULE.covered(page, date(2026, 8, 28)))
        self.assertTrue(
            MODULE.covered(page + items("2026-08-20", "2026-08-18"), date(2026, 8, 28))
        )
        self.assertIsNone(MODULE.tail_date(items(None)))

    def test_span_counts_only_days_the_pages_reach(self):
        start, end = date(2026, 8, 28), date(2026, 10, 22)
        pages = [{"items": items("2026-10-20", "2026-10-01", "2026-09-30")}]
        # 末尾三条取中间那个（10-01）
        self.assertEqual(MODULE.span_days(pages, start, end), 22)
        pages[0]["items"] += items("2026-08-01", "2026-07-30")
        self.assertEqual(MODULE.span_days(pages, start, end), 56)
        self.assertEqual(MODULE.span_days([{"items": []}], start, end), 0)
        self.assertEqual(MODULE.per_week(10, 35), 2.0)
        self.assertEqual(MODULE.per_week(3, 0), 0.0)


class PagingTests(unittest.TestCase):
    URL = "https://kjj.example.gov.cn/tzgg/index.html"

    def test_next_link_by_text_or_label_same_site_only(self):
        html = '<a href="index_2.html" class="next"> 下一页 </a>'
        self.assertEqual(
            MODULE.next_page_url(html, self.URL),
            "https://kjj.example.gov.cn/tzgg/index_2.html",
        )
        html = '<a href="index_2.jhtml" aria-label="下一页"></a>'
        self.assertEqual(
            MODULE.next_page_url(html, self.URL),
            "https://kjj.example.gov.cn/tzgg/index_2.jhtml",
        )
        self.assertIsNone(
            MODULE.next_page_url('<a href="javascript:next()">下一页</a>', self.URL)
        )
        self.assertIsNone(
            MODULE.next_page_url(
                '<a href="https://other.gov.cn/2.html">下一页</a>', self.URL
            )
        )

    def test_next_link_must_stay_in_the_column(self):
        first = "https://www.example.gov.cn/gova/tzgg.php?catname=kjj_02_04"
        # "下一页"写成 ?page=2 会丢掉栏目参数，跟过去就是别的栏目
        self.assertIsNone(MODULE.next_page_url('<a href="?page=2">下一页</a>', first))
        self.assertEqual(
            MODULE.next_page_url(
                '<a href="?catname=kjj_02_04&page=2">下一页</a>', first
            ),
            "https://www.example.gov.cn/gova/tzgg.php?catname=kjj_02_04&page=2",
        )
        # 侧栏里别的目录的"下一页"不跟
        self.assertIsNone(
            MODULE.next_page_url('<a href="/other/index_2.html">下一页</a>', self.URL)
        )

    def test_script_pages_follow_the_sites_own_scheme(self):
        zero = 'createPageHTML(3, 0, "index", "html");'
        self.assertEqual(
            MODULE.script_pages(zero, self.URL),
            [
                "https://kjj.example.gov.cn/tzgg/index_1.html",
                "https://kjj.example.gov.cn/tzgg/index_2.html",
            ],
        )
        one = "createPageHTML('page_div',3, 1,'common_list','shtml',40)"
        self.assertEqual(
            MODULE.script_pages(
                one, "https://kjj.example.gov.cn/tzgg/common_list.shtml"
            ),
            [
                "https://kjj.example.gov.cn/tzgg/common_list_2.shtml",
                "https://kjj.example.gov.cn/tzgg/common_list_3.shtml",
            ],
        )
        trs = 'var _nPageCount = 2; var _nCurrIndex = 0; var _sPageName = "index"; var _sPageExt = "html";'
        self.assertEqual(
            MODULE.script_pages(trs, self.URL),
            ["https://kjj.example.gov.cn/tzgg/index_1.html"],
        )
        self.assertEqual(MODULE.script_pages("<html></html>", self.URL), [])


class ManifestTests(unittest.TestCase):
    def test_db_name_must_be_a_trial_database(self):
        self.assertIn("youju_citytrial_1023", MODULE.db_url("youju_citytrial_1023"))
        for name in ("youju", "youju_test", "youju_citytrial-x"):
            with self.assertRaises(SystemExit):
                MODULE.db_url(name)

    def test_row_key(self):
        self.assertEqual(
            MODULE.row_key({"省份": "广东", "城市": "广州", "部门": "科技"}),
            "广东-广州-科技",
        )


if __name__ == "__main__":
    unittest.main()
